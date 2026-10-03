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


class SipCallRecordError(RuntimeError):
    """Sanitized storage failure; never includes SQL or values."""


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
        error = re.sub(r'[^A-Za-z0-9 _.-]', '', str(event.get('Error') or ''))[:40]
        cause = str(event.get('Cause') or '').strip()
        if status == 'SUCCESS':
            error_cause = None
        else:
            error_cause = (error or 'fax failed') + (f' (cause {cause})' if cause.isdigit() else '')

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

        def apply(connection, table):
            existing = self._find(connection, table, 'inbound', call_id)
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
        return result

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


def attach(ami_client, engine):
    """Record calls from this AMI client into ``engine``; safe to call on every start."""
    global _current
    _current = SipCallRecords(engine)
    ami_client.on_submission(_on_submission)
    ami_client.on_originate_response(_on_originate_response)
    ami_client.on_fax_result(_on_fax_result)
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
