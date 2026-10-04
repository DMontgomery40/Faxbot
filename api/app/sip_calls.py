"""Per-call records for faxes Faxbot's own engine places or answers over the SIP trunk.

Outbound rows start when a call is submitted (``ambiguous`` until Asterisk
reports otherwise) and are completed from AMI events: OriginateResponse for
calls that never answer, the dialplan's FaxResult for answered calls. Inbound
rows come from the call details the inbound dialplan sends with a received
fax. Recording is evidence only: it never changes delivery state, and a
recording failure never reaches the caller of the event.

Readers (the routing ledger, the console) use ``for_attempt``,
``connected_seconds_for``/``observed_seconds`` and the cursor-paginated ``page``.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import logging
import re
from uuid import uuid4

import sqlalchemy as sa


_ID = re.compile(r'[A-Za-z0-9_-]{1,40}', re.ASCII)
_NUMBER = re.compile(r'\+?[0-9]{3,20}', re.ASCII)
_CALL = re.compile(r'[A-Za-z0-9_.:-]{1,100}', re.ASCII)
# AMI OriginateResponse Reason values (Asterisk include/asterisk/frame.h and
# pbx_dial_reason in main/pbx.c): 0 failure, 1 hangup, 3 rang without answer,
# 4 answered, 5 busy, 8 congestion.
_REASONS = {'0': 'failed', '1': 'failed', '3': 'no_answer', '4': 'answered', '5': 'busy', '8': 'congestion'}
_REASON_TEXT = {'0': 'call failed', '1': 'call ended before answer', '3': 'no answer', '5': 'busy',
                '8': 'network congestion'}
COLUMNS = ('id', 'direction', 'job_id', 'attempt_id', 'trunk_preset', 'did', 'caller', 'called', 'started_at',
           'answered_at', 'ended_at', 'disposition', 'connected_seconds', 't38', 'pages', 'fax_status',
           'remote_station_id', 'error_cause', 'fax_preference')

NO_FAX_DATA = 'The call connected but no fax data came back from the carrier.'
NO_SOUND = 'The call connected but no sound came back from the carrier.'
NOT_A_FAX = 'The call connected but the other end did not answer as a fax machine.'
# Verdicts for an answered call that delivered no fax. The no-data ones mean the
# network path failed; the other two mean the network carried the call.
NO_DATA_VERDICTS = frozenset({'no_media_back', 'no_t38_data_back', 'no_fax_data_back'})
VERDICTS = NO_DATA_VERDICTS | {'no_fax_answer', 'remote_fax_failed'}
# Fax engine endings that mean the far end never sent one fax message: spandsp's
# T0 and T1 timers run only until the first message arrives, res_fax's TIMEOUT
# means nothing moved in either direction, and a hang-up or an unanswered
# repeated command counts only with no page and no remote station either. In
# the loopback proof, against a carrier that never followed Faxbot's packets, a
# sent fax ended "The call dropped prematurely" (the carrier hung up first) and
# a received one ended either that way or "Disconnected after permitted
# retries" (Faxbot's own DIS went unanswered three times), depending on timing.
_NO_MESSAGE_ERRORS = frozenset({
    'timed out waiting for initial communication', 'timed out waiting for the first message',
    'timeout', 'hangup', 'channel_hangup', 'the call dropped prematurely', 'remote channel hungup',
    'disconnected after permitted retries'})
_DISPOSITION_TEXT = {
    'busy': 'The number was busy.',
    'no_answer': 'Nobody answered the call.',
    'congestion': 'The carrier network was too busy to connect the call.',
    'failed': 'The call did not connect.',
    'ambiguous': 'Faxbot does not know yet how this call ended.',
}
_UNFINISHED = 'The call connected but the fax did not finish.'


class SipCallRecordError(RuntimeError):
    """Sanitized storage failure; never includes SQL or values."""


def _decoded(encoded, plain=''):
    """Base64 text from the dialplan, falling back to the plain field; printable characters only."""
    text = str(plain or '')
    if encoded:
        try:
            text = base64.b64decode(str(encoded), validate=True).decode('utf-8', 'replace')
        except (binascii.Error, ValueError):
            pass
    return ''.join(character for character in text if character.isprintable()).strip()


def _count(value):
    """An audio packet count, or None when Asterisk could not count (after a T.38 switch)."""
    text = str(value or '').strip()
    return int(text) if text.isdigit() and len(text) <= 9 else None


def _reason(event):
    """The fax engine's own words for how the call ended."""
    return _decoded(event.get('Error64'), event.get('Error')) or _decoded(event.get('Status64'))


def verdict(event):
    """Why a connected fax call delivered nothing, from a FaxResult or FaxInboundCall event.

    None for a call that was not answered or whose fax went through. Packet
    counts are trusted only when the call ended in audio: after a T.38 switch
    Asterisk no longer has the audio counters, so the T.38 verdict comes from
    the mode, the pages and the fax engine's reason.
    """
    if not str(event.get('Answered') or '').strip():
        return None
    if str(event.get('Status') or '').strip().upper() == 'SUCCESS':
        return None
    if (_pages(event.get('Pages')) or 0) > 0 or _station(event.get('Station64')):
        return 'remote_fax_failed'
    mode = str(event.get('Mode') or '').strip().lower()
    received = _count(event.get('RtpRx')) if mode != 't38' else None
    reasons = {_decoded(event.get('Error64'), event.get('Error')).lower(), _decoded(event.get('Status64')).lower()}
    if received == 0:
        return 'no_media_back'
    if received is not None:
        # Sound came back, so the network path works; the far end sent no fax signal.
        return 'no_fax_answer' if reasons & _NO_MESSAGE_ERRORS else 'remote_fax_failed'
    if reasons & _NO_MESSAGE_ERRORS:
        return 'no_t38_data_back' if mode == 't38' else 'no_fax_data_back'
    return 'remote_fax_failed'


def _error_cause(event):
    """What error_cause stores for a failed call: the verdict, the engine's words, the hang-up cause."""
    error = re.sub(r'[^A-Za-z0-9 _.,-]', '', _reason(event))
    cause = str(event.get('Cause') or '').strip()
    suffix = f' (cause {cause})' if cause.isdigit() and len(cause) <= 3 else ''
    found = verdict(event)
    prefix = found + ': ' if found else ''
    room = 64 - len(prefix) - len(suffix)
    return prefix + (error or 'fax failed')[:room].rstrip() + suffix


def _sentence(found, reason=''):
    if found == 'no_media_back':
        return NO_SOUND
    if found in NO_DATA_VERDICTS:
        return NO_FAX_DATA
    if found == 'no_fax_answer':
        return NOT_A_FAX
    if found == 'remote_fax_failed':
        reason = reason.strip().rstrip('.')
        return (f'The other fax machine answered but the fax failed: {reason}.' if reason
                else 'The other fax machine answered but the fax failed.')
    return _UNFINISHED


def _pages_text(pages):
    return '1 page' if pages == 1 else f'{pages} pages'


def result_summary(event):
    """One plain sentence for a finished outbound fax call, or None when the fax went through."""
    if str(event.get('Status') or '').strip().upper() == 'SUCCESS':
        return None
    return _sentence(verdict(event), _reason(event)[:60])


def _no_pages(caller, found):
    """A received call that left no fax image, named by its caller."""
    who = f'A fax call from {caller}' if caller else 'A fax call'
    if found in NO_DATA_VERDICTS:
        return f'{who} came in, but no fax data arrived from the carrier.'
    return f'{who} came in, but no pages arrived.'


def inbound_summary(event):
    """One plain sentence for a received call that left no fax image."""
    if not str(event.get('Answered') or '').strip():
        return 'The caller hung up before Faxbot answered.'
    return _no_pages(_number(event.get('Caller')), verdict(event))


def originate_summary(event):
    """One plain sentence for a call that never connected, or None when it did."""
    if str(event.get('Response') or '').strip().lower() != 'failure':
        return None
    disposition = _REASONS.get(str(event.get('Reason') or '').strip(), 'failed')
    return _DISPOSITION_TEXT['failed' if disposition == 'answered' else disposition]


def stored_verdict(record):
    """The verdict a stored call record carries: sent, received, a failure verdict, or None."""
    if record['disposition'] == 'answered' and record['fax_status'] == 'SUCCESS':
        return 'sent' if record['direction'] == 'outbound' else 'received'
    code = (record['error_cause'] or '').split(':', 1)[0]
    return code if code in VERDICTS else None


def call_summary(record):
    """One plain sentence for a stored call record (public field names)."""
    disposition = record['disposition']
    if disposition == 'failed' and record['direction'] == 'inbound':
        return 'The caller hung up before Faxbot answered.'
    if disposition != 'answered':
        return _DISPOSITION_TEXT[disposition]
    found = stored_verdict(record)
    if record['direction'] == 'inbound' and record['job_id'] is None and found != 'received':
        return _no_pages(record['caller'], found)
    if found == 'sent':
        return f'Sent: {_pages_text(record["pages"] or 0)} confirmed by the receiving machine.'
    if found == 'received':
        return f'Received: {_pages_text(record["pages"] or 0)}.'
    if found:
        reason = (record['error_cause'] or '').split(':', 1)[1]
        return _sentence(found, re.sub(r' \(cause [0-9]+\)$', '', reason.strip()))
    if record['ended_at'] is None:
        return 'The call connected and the fax is still in progress.'
    return _UNFINISHED


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _epoch(value):
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not 946684800 <= seconds <= 4102444800:  # 2000-01-01 .. 2100-01-01
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)


def _number(value):
    text = str(value or '').strip()
    return text if _NUMBER.fullmatch(text) else None


def _identity(value):
    text = str(value or '').strip()
    return text if _ID.fullmatch(text) else None


def _station(encoded):
    if not encoded:
        return None
    try:
        text = base64.b64decode(str(encoded), validate=True).decode('utf-8', 'replace')
    except (binascii.Error, ValueError):
        return None
    text = ''.join(character for character in text if character.isprintable()).strip()
    return text[:40] or None


def _pages(value):
    try:
        pages = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return pages if 0 <= pages <= 100000 else None


def _t38(mode):
    mode = str(mode or '').strip().lower()
    return 'yes' if mode == 't38' else 'no' if mode == 'audio' else 'unknown'


def _seconds(answered, ended):
    if answered is None or ended is None or ended < answered:
        return None
    return int((ended - answered).total_seconds())


def _iso(value):
    return value.replace(microsecond=0).isoformat() + 'Z' if isinstance(value, datetime) else None


def _encode_cursor(started_at, identity):
    raw = f'{started_at.isoformat()}|{identity}'.encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _decode_cursor(cursor):
    try:
        padded = cursor + '=' * (-len(cursor) % 4)
        started, identity = base64.urlsafe_b64decode(padded.encode()).decode().split('|', 1)
        return datetime.fromisoformat(started), identity
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise ValueError('Invalid cursor') from None


class SipCallRecords:
    """Read and write ``sip_call_records`` through one installation engine."""

    def __init__(self, engine):
        self.engine = engine
        self._table = None

    @property
    def table(self):
        if self._table is None:
            metadata = sa.MetaData()
            try:
                self._table = sa.Table('sip_call_records', metadata, autoload_with=self.engine)
            except sa.exc.SQLAlchemyError:
                raise SipCallRecordError('Call records are unavailable.') from None
        return self._table

    # Writes ------------------------------------------------------------------

    def _write(self, operation):
        try:
            with self.engine.begin() as connection:
                return operation(connection, self.table)
        except sa.exc.SQLAlchemyError:
            raise SipCallRecordError('Call record could not be saved.') from None

    @staticmethod
    def _find(connection, table, direction, call_id):
        return connection.execute(sa.select(table).where(
            table.c.direction == direction, table.c.call_id == call_id)).mappings().first()

    def _outbound_row(self, connection, table, job_id, attempt_id, now, **values):
        row = self._find(connection, table, 'outbound', attempt_id)
        if row is not None:
            return row
        record = {'id': uuid4().hex, 'direction': 'outbound', 'call_id': attempt_id, 'job_id': job_id,
                  'attempt_id': attempt_id, 'started_at': now, 'disposition': 'ambiguous', 't38': 'unknown',
                  'fax_preference': 0, 'created_at': now, 'updated_at': now, **values}
        connection.execute(table.insert().values(**record))
        return self._find(connection, table, 'outbound', attempt_id)

    def record_submission(self, event, *, now=None):
        """A call is about to be placed; its outcome is unknown until Asterisk says otherwise."""
        job_id, attempt_id = _identity(event.get('JobID')), _identity(event.get('AttemptID'))
        if job_id is None or attempt_id is None:
            return None
        now = now or utcnow()
        caller = _number(event.get('CallerID'))
        preset = str(event.get('Preset') or '')[:32] or None
        values = {'trunk_preset': preset, 'did': caller, 'caller': caller, 'called': _number(event.get('Called')),
                  'fax_preference': 1 if event.get('FaxPreference') == 'yes' else 0}
        return self._write(lambda connection, table: self._outbound_row(
            connection, table, job_id, attempt_id, now, **values)['id'])

    def record_originate_response(self, event, *, now=None):
        parts = str(event.get('ActionID') or '').split(':')
        if len(parts) != 3 or parts[0] != 'faxbot':
            return None
        job_id, attempt_id = _identity(parts[1]), _identity(parts[2])
        if job_id is None or attempt_id is None:
            return None
        now = now or utcnow()
        response = str(event.get('Response') or '').lower()
        reason = str(event.get('Reason') or '').strip()

        def apply(connection, table):
            row = self._outbound_row(connection, table, job_id, attempt_id, now)
            if row['ended_at'] is not None:
                return row['id']
            if response == 'success':
                changes = {'disposition': 'answered', 'answered_at': row['answered_at'] or now}
            elif response == 'failure':
                changes = {'disposition': _REASONS.get(reason, 'failed'), 'ended_at': now, 'connected_seconds': 0,
                           'error_cause': _REASON_TEXT.get(reason, 'call failed')}
                if changes['disposition'] == 'answered':
                    changes = {'disposition': 'failed', 'ended_at': now, 'error_cause': 'call failed'}
            else:
                return row['id']
            connection.execute(table.update().where(table.c.id == row['id']).values(updated_at=now, **changes))
            return row['id']
        return self._write(apply)

    def record_fax_result(self, event, *, now=None):
        job_id, attempt_id = _identity(event.get('JobID')), _identity(event.get('AttemptID'))
        if job_id is None or attempt_id is None:
            return None
        now = now or utcnow()
        answered, ended = _epoch(event.get('Answered')), _epoch(event.get('Ended')) or now
        status = re.sub(r'[^A-Z_]', '', str(event.get('Status') or '').upper())[:16] or None
        error_cause = None if status == 'SUCCESS' else _error_cause(event)

        def apply(connection, table):
            row = self._outbound_row(connection, table, job_id, attempt_id, now)
            answered_at = answered or row['answered_at']
            changes = {'disposition': 'answered', 'answered_at': answered_at, 'ended_at': ended,
                       'connected_seconds': _seconds(answered_at, ended), 't38': _t38(event.get('Mode')),
                       'pages': _pages(event.get('Pages')), 'fax_status': status,
                       'remote_station_id': _station(event.get('Station64')), 'error_cause': error_cause}
            connection.execute(table.update().where(table.c.id == row['id']).values(updated_at=now, **changes))
            return row['id']
        return self._write(apply)

    def record_inbound(self, call, *, call_id, inbound_fax_id=None, preset=None, fax_status=None, now=None):
        """One received call; repeated reports of the same Asterisk call are ignored."""
        if not isinstance(call, dict):
            return None
        call_id = str(call_id or '').strip()
        if not _CALL.fullmatch(call_id):
            return None
        now = now or utcnow()
        started, answered, ended = (_epoch(call.get(name)) for name in ('started_at', 'answered_at', 'ended_at'))
        did, caller = _number(call.get('did')), _number(call.get('caller'))
        t38 = call.get('t38')
        status = re.sub(r'[^A-Z_]', '', str(fax_status or '').upper())[:16] or None
        record = {
            'id': uuid4().hex, 'direction': 'inbound', 'call_id': call_id, 'job_id': _identity(inbound_fax_id),
            'attempt_id': None, 'trunk_preset': str(preset or '')[:32] or None, 'did': did, 'caller': caller,
            'called': did, 'started_at': started or answered or now, 'answered_at': answered, 'ended_at': ended,
            'disposition': 'answered', 'connected_seconds': _seconds(answered, ended),
            't38': 'yes' if t38 is True else 'no' if t38 is False else 'unknown',
            'pages': _pages(call.get('pages')), 'fax_status': status,
            'remote_station_id': _station(call.get('remote_station_id_b64')), 'error_cause': None,
            'fax_preference': 0, 'created_at': now, 'updated_at': now}

        return self._insert_inbound(record)

    def record_inbound_event(self, event, *, preset=None, now=None):
        """A received call that left no fax image (the FaxInboundCall manager event)."""
        call_id = str(event.get('UniqueID') or '').strip()
        if not _CALL.fullmatch(call_id):
            return None
        now = now or utcnow()
        started, answered = _epoch(event.get('Started')), _epoch(event.get('Answered'))
        ended = _epoch(event.get('Ended')) or now
        did, caller = _number(event.get('DID')), _number(event.get('Caller'))
        status = re.sub(r'[^A-Z_]', '', str(event.get('Status') or '').upper())[:16] or None
        record = {
            'id': uuid4().hex, 'direction': 'inbound', 'call_id': call_id, 'job_id': None, 'attempt_id': None,
            'trunk_preset': str(preset or '')[:32] or None, 'did': did, 'caller': caller, 'called': did,
            'started_at': started or answered or now, 'answered_at': answered, 'ended_at': ended,
            'disposition': 'answered' if answered else 'failed', 'connected_seconds': _seconds(answered, ended),
            't38': _t38(event.get('Mode')), 'pages': _pages(event.get('Pages')), 'fax_status': status,
            'remote_station_id': _station(event.get('Station64')),
            'error_cause': _error_cause(event) if answered else 'caller hung up before answer',
            'fax_preference': 0, 'created_at': now, 'updated_at': now}
        return self._insert_inbound(record)

    def _insert_inbound(self, record):
        def apply(connection, table):
            existing = self._find(connection, table, 'inbound', record['call_id'])
            if existing is not None:
                return existing['id']
            connection.execute(table.insert().values(**record))
            return record['id']
        return self._write(apply)

    # Reads -------------------------------------------------------------------

    @staticmethod
    def _public(row):
        result = {name: row[name] for name in COLUMNS}
        for name in ('started_at', 'answered_at', 'ended_at'):
            result[name] = _iso(result[name])
        result['fax_preference'] = bool(result['fax_preference'])
        result['verdict'] = stored_verdict(result)
        result['summary'] = call_summary(result)
        return result

    def latest(self):
        """The newest call record, or None when there are none yet."""
        items = self.page(limit=1)['items']
        return items[0] if items else None

    def for_attempt(self, attempt_id):
        """Call records for one outbound attempt (normally one), oldest first."""
        if _identity(attempt_id) is None:
            return []
        table = self.table
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(sa.select(table).where(table.c.attempt_id == attempt_id)
                                          .order_by(table.c.started_at, table.c.id)).mappings().all()
        except sa.exc.SQLAlchemyError:
            raise SipCallRecordError('Call records are unavailable.') from None
        return [self._public(row) for row in rows]

    def connected_seconds_for(self, attempt_id):
        """Measured connected seconds for a finished attempt, or None when unknown."""
        for record in reversed(self.for_attempt(attempt_id)):
            if record['ended_at'] is not None and record['connected_seconds'] is not None:
                return record['connected_seconds']
        return None

    def observed_seconds(self, target):
        """Adapter for a cost recorder target carrying the attempt id as ``attempt_id`` or ``id``."""
        return self.connected_seconds_for(getattr(target, 'attempt_id', None) or getattr(target, 'id', None))

    def page(self, *, cursor=None, limit=50, direction=None):
        """Newest calls first; pass the returned ``next_cursor`` to continue."""
        if direction not in (None, 'outbound', 'inbound'):
            raise ValueError('Invalid direction')
        limit = max(1, min(int(limit), 200))
        table = self.table
        query = sa.select(table)
        if direction:
            query = query.where(table.c.direction == direction)
        if cursor:
            started, identity = _decode_cursor(cursor)
            query = query.where(sa.or_(table.c.started_at < started,
                                       sa.and_(table.c.started_at == started, table.c.id < identity)))
        query = query.order_by(table.c.started_at.desc(), table.c.id.desc()).limit(limit + 1)
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(query).mappings().all()
        except sa.exc.SQLAlchemyError:
            raise SipCallRecordError('Call records are unavailable.') from None
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1]['started_at'], rows[-1]['id']) if more and rows else None
        return {'items': [self._public(row) for row in rows], 'next_cursor': next_cursor}


# AMI wiring ---------------------------------------------------------------------

_current: SipCallRecords | None = None


def _safely(method_name):
    def handler(event):
        records = _current
        if records is None:
            return
        try:
            getattr(records, method_name)(event)
        except Exception:
            logging.getLogger(__name__).warning('A SIP call record could not be saved.')
    handler.__name__ = 'record_' + method_name
    return handler


_on_submission = _safely('record_submission')
_on_originate_response = _safely('record_originate_response')
_on_fax_result = _safely('record_fax_result')


def _active_preset():
    try:
        from .config import configuration_values
        return configuration_values().sip_trunk_preset or None
    except Exception:
        return None


def _on_inbound_call(event):
    records = _current
    if records is None:
        return
    try:
        records.record_inbound_event(event, preset=_active_preset())
    except Exception:
        logging.getLogger(__name__).warning('A SIP call record could not be saved.')


def attach(ami_client, engine):
    """Record calls from this AMI client into ``engine``; safe to call on every start."""
    global _current
    _current = SipCallRecords(engine)
    ami_client.on_submission(_on_submission)
    ami_client.on_originate_response(_on_originate_response)
    ami_client.on_fax_result(_on_fax_result)
    ami_client.on_inbound_call(_on_inbound_call)
    return _current


def detach():
    global _current
    _current = None


def record_inbound_call(engine, call, *, call_id, inbound_fax_id, preset=None, fax_status=None):
    """Record a received call's details; the fax itself is already stored, so failures only log."""
    try:
        return SipCallRecords(engine).record_inbound(call, call_id=call_id, inbound_fax_id=inbound_fax_id,
                                                     preset=preset, fax_status=fax_status)
    except Exception:
        logging.getLogger(__name__).warning('A SIP call record could not be saved.')
        return None
