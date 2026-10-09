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
# A SIP Call-ID: visible ASCII without spaces (RFC 3261 word characters and @).
_SIP_CALL_ID = re.compile(r'[!-~]{1,100}', re.ASCII)
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
# An engine call on which the engine never heard the other fax machine: the engine's own words, never the
# network's (its T.38 gateway may be the cause). Operator words call the SSL Fax engine "the fax engine".
NO_FAX_SIGNAL = 'no_fax_signal'
NO_SIGNAL = "The call connected but the fax engine heard no fax machine on the line."
NO_SOUND = 'The call connected but no sound came back from the carrier.'
NOT_A_FAX = 'The call connected but the other end did not answer as a fax machine.'
# A person or a voice line answered: sound came back, no fax message ever did, and the far end hung up first
# (research N9, 2026-10-08). The call was answered by a person, never by a fax machine on another route, so the
# fax takes no other route by itself (``outbound_store.NO_FALLBACK_CATEGORIES``) and a person checks the number.
PERSON_ANSWERED = 'person_answered'
PERSON = 'A person answered, not a fax machine; Faxbot did not call again.'
# Verdicts for an answered call that delivered no fax. The no-data ones mean the
# network path failed; the other two mean the network carried the call.
NO_DATA_VERDICTS = frozenset({'no_media_back', 'no_t38_data_back', 'no_fax_data_back'})
# A received fax whose image Asterisk stored but could not hand to Faxbot.
NOT_HANDED_OVER = 'not_handed_over'
VERDICTS = NO_DATA_VERDICTS | {'no_fax_answer', 'remote_fax_failed', NOT_HANDED_OVER, NO_FAX_SIGNAL, PERSON_ANSWERED}
# What the Asterisk notify script prints when a hand-over fails, and the plain
# reason after "A fax was received but could not be handed to Faxbot: ".
HANDOVER_REASONS = {
    'no_secret': 'the fax engine has no inbound secret yet; select Apply and connect',
    'refused': "Faxbot refused the fax engine's inbound secret; select Apply and connect",
    'not_receiving': 'receiving faxes is turned off in Faxbot',
    'unreadable': 'Faxbot could not read the received image',
    'unreachable': 'Faxbot could not be reached',
    'failed': 'Faxbot answered with an error',
}


def handover_sentence(reason):
    """One sentence for a received fax that Asterisk could not hand to Faxbot."""
    text = HANDOVER_REASONS.get(str(reason or '').strip(), HANDOVER_REASONS['failed'])
    return f'A fax was received but could not be handed to Faxbot: {text}.'


def _handover(event):
    """The failed hand-over reason in a FaxInboundCall event, or None."""
    reason = str(event.get('Handover') or '').strip().lower()
    if not reason or reason == 'ok':
        return None
    return reason if reason in HANDOVER_REASONS else 'failed'
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
# Of those, the endings in which the far end hung up first (spandsp's T30_ERR_CALLDROPPED, res_fax's HANGUP). With
# sound back and no fax message, that is a person or a voice line; a timeout with the line still open may be a
# silent route or a media problem, so it stays ``no_fax_answer`` and another route may still send the fax.
_HUNG_UP = frozenset({'hangup', 'channel_hangup', 'the call dropped prematurely', 'remote channel hungup'})
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
        # Sound came back, so the network path works; the far end sent no fax signal. When it also hung up first,
        # a person or a voice line answered.
        if reasons & _NO_MESSAGE_ERRORS:
            return PERSON_ANSWERED if reasons & _HUNG_UP else 'no_fax_answer'
        return 'remote_fax_failed'
    if reasons & _NO_MESSAGE_ERRORS:
        return 'no_t38_data_back' if mode == 't38' else 'no_fax_data_back'
    return 'remote_fax_failed'


# HylaFAX+ 7.0.11 status codes (faxd/ClassModem.c++, faxd/Class1Send.c++, faxd/Class1Recv.c++) for an
# answered engine call on which the other side never sent one fax message: E002 "No carrier detected" (no
# fax answer once the call connected), E126 "No receiver protocol (T.30 T1 timeout)" and, receiving,
# E102 "No sender protocol (T.30 T1 timeout)".
_ENGINE_NO_MESSAGE = re.compile(r'\bE(?:002|102|126)\b|No carrier detected|T\.30 T1 timeout', re.IGNORECASE)
_ENGINE_HUNG_UP = re.compile(r'\bE002\b|No carrier detected', re.IGNORECASE)
# What the trunk heard on an audio engine call, kept until the engine's result arrives.
_HEARD, _SILENT = 'trunk heard sound', 'trunk heard no sound'
# The same, after the engine's words when its result came before the call ended.
_HEARD_MARK = re.compile(r' ~h([01])$')


def engine_verdict(record, heard=None):
    """Why an answered engine call delivered nothing. The engine's result decides (its pages, the other
    machine's ID, its own reason); the trunk adds only what the engine cannot know: whether sound came
    back on an audio call (``heard``). A call on which the engine never heard the other fax machine is
    ``no_fax_signal``, never a network verdict: on a T.38 call the engine's own gateway may be the cause.
    None for a fax that went through or a call that was not answered."""
    if record['disposition'] != 'answered' or record['fax_status'] != 'FAILED':
        return None
    if (record['pages'] or 0) > 0 or record['remote_station_id']:
        return 'remote_fax_failed'
    audio = record['t38'] != 'yes'
    if audio and heard is False:
        return 'no_media_back'
    if not _ENGINE_NO_MESSAGE.search(record['error_cause'] or ''):
        return 'remote_fax_failed'
    if audio and heard and record['direction'] == 'outbound':
        # E002 "No carrier detected": the line dropped before any fax carrier, after sound came back, as when a
        # person answers and hangs up; a T.30 T1 timeout (E126) kept the line open and stays no_fax_answer.
        return PERSON_ANSWERED if _ENGINE_HUNG_UP.search(record['error_cause'] or '') else 'no_fax_answer'
    return NO_FAX_SIGNAL


def freeswitch_verdict(status, pages, station, text, audio_in):
    """``person_answered`` for a FreeSWITCH send (mod_spandsp's channel variables) that a person or a voice line
    answered: failed, no page and no remote station, spandsp's "The call dropped prematurely" (the far end hung up
    before any fax message), and sound came back (the hook's ``rtp_audio_in_packet_count`` above zero). Without
    that count Faxbot cannot tell a person from a silent line, so the fax may still take another route; None then,
    and for a timeout or any other ending."""
    if status != 'failed' or (type(pages) is int and pages > 0) or (station or '').strip():
        return None
    if ' '.join(str(text or '').split()).lower().rstrip('.') not in _HUNG_UP:
        return None
    return PERSON_ANSWERED if type(audio_in) is int and audio_in > 0 else None


def category_for(found):
    """The attempt's error category for a call verdict: ``person_answered`` (never another route), else None."""
    return PERSON_ANSWERED if found == PERSON_ANSWERED else None


def verdict_sentence(found):
    """The Jobs sentence for a failure verdict, or None."""
    if found in VERDICTS and found != NOT_HANDED_OVER:
        return 'The other fax machine answered but the fax did not finish.' if found == 'remote_fax_failed' \
            else _sentence(found)
    return None


def _kept_heard(kept):
    """What the trunk heard on an engine call, as kept in the row until the engine's result arrived."""
    return True if kept == _HEARD else False if kept == _SILENT else None


def _log_gateway(event):
    """The T.38 gateway's own outcome for an engine call, in the server log (no numbers, no secrets)."""
    status = re.sub(r'[^A-Z_]', '', str(event.get('GwStatus') or ''))[:20]
    if not status:
        return
    error = re.sub(r'[^A-Z0-9_]', '', str(event.get('GwError') or ''))[:30]
    pages = re.sub(r'[^0-9]', '', str(event.get('GwPages') or ''))[:4]
    words = re.sub(r'[^A-Za-z0-9 .,_-]', '', _decoded(event.get('Gw64')))[:80]
    logging.getLogger(__name__).info('SSL Fax engine call: T.38 gateway %s %s, %s pages; %s', status, error or '-',
                                     pages or '0', words or '-')


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
    if found == NO_FAX_SIGNAL:
        return NO_SIGNAL
    if found == 'no_media_back':
        return NO_SOUND
    if found in NO_DATA_VERDICTS:
        return NO_FAX_DATA
    if found == 'no_fax_answer':
        return NOT_A_FAX
    if found == PERSON_ANSWERED:
        return PERSON
    if found == 'remote_fax_failed':
        reason = reason.strip().rstrip('.')
        return (f'The other fax machine answered but the fax failed: {reason}.' if reason
                else 'The other fax machine answered but the fax failed.')
    return _UNFINISHED


def _pages_text(pages):
    return '1 page' if pages == 1 else f'{pages} pages'


def result_summary(event):
    """One plain sentence for a finished outbound fax call, or None when the fax went through.

    Jobs show at most 80 characters, so the fax engine's own reason stays in
    the call record (Recent calls) and Jobs get a fixed sentence.
    """
    if str(event.get('Status') or '').strip().upper() == 'SUCCESS':
        return None
    found = verdict(event)
    if found == 'remote_fax_failed':
        return 'The other fax machine answered but the fax did not finish.'
    return _sentence(found)


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
    code = (record['error_cause'] or '').split(':', 1)[0]
    if code == NOT_HANDED_OVER and record['direction'] == 'inbound' and record['job_id'] is None:
        # Until Faxbot brings the image in, the fax is not in Faxbot even when the call succeeded.
        return code
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
    if found == NOT_HANDED_OVER:
        return handover_sentence((record['error_cause'] or '').split(':', 1)[1])
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


def _installation_country():
    """The installation's country for reading received numbers, or None when it cannot be read."""
    try:
        from .config import settings
        return settings.fax_default_country or 'US'
    except Exception:
        return None


def _received(value, country=None):
    """A received call's number the way the fax hand-overs store it (read for the installation's country):
    Asterisk's events carry the carrier's form (Telnyx: 3034265097), so a call reads the same whichever
    report came first. Kept as given when it is no telephone number there or the country cannot be read."""
    number = _number(value)
    if number is None:
        return None
    country = country or _installation_country()
    if country is None:
        return number
    try:
        from .inbound.http import received_number
        return _number(received_number(number, country)) or number
    except Exception:
        return number


_TRUNK = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')


def _trunk(value):
    """The trunk account a call went over, for ``trunk_key``: a trunk after the first by its key; the first
    trunk (``sip``), and a call that names none, as NULL, which every reader takes as the first trunk."""
    value = str(value or '').strip()
    return value if _TRUNK.fullmatch(value) and value != 'sip' else None


def _identity(value):
    text = str(value or '').strip()
    return text if _ID.fullmatch(text) else None


_SUBADDRESS = re.compile(r'[0-9#*+]{1,20}')
_PEER = re.compile(r'[a-f0-9]{32}')


def _subaddress(value):
    """The subaddress a call asked for (patch 0005), for ``subaddress``; None when absent or malformed."""
    text = str(value or '').strip()
    return text if _SUBADDRESS.fullmatch(text) else None


def _peer(value):
    """The enrolled partner (its enrollment ID) a peer fax call went to or came from, for ``peer_id``."""
    text = str(value or '').strip()
    return text if _PEER.fullmatch(text) else None


def _sip_call_id(encoded):
    """The SIP Call-ID the dialplan captured (base64), the carrier's key for its bill; None when absent."""
    if not encoded:
        return None
    try:
        text = base64.b64decode(str(encoded), validate=True).decode('ascii')
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    text = text.strip()
    return text if _SIP_CALL_ID.fullmatch(text) else None


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
        if _trunk(event.get('Trunk')):
            values['trunk_key'] = _trunk(event.get('Trunk'))
        # The subaddress this call asked for (patch 0005): requested; carried only if the far end takes one.
        subaddress = _subaddress(event.get('Subaddress'))
        # A peer fax call to an enrolled partner inside its tunnel, with no carrier (direct/peer_call.py).
        peer = _peer(event.get('Peer'))

        def write(connection, table):
            extra = {name: value for name, value in (('subaddress', subaddress), ('peer_id', peer))
                     if value and name in table.c}
            return self._outbound_row(connection, table, job_id, attempt_id, now, **values, **extra)['id']
        return self._write(write)

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
            sip_call_id = _sip_call_id(event.get('CallID64'))
            if sip_call_id and not row['sip_call_id']:
                changes['sip_call_id'] = sip_call_id
            connection.execute(table.update().where(table.c.id == row['id']).values(updated_at=now, **changes))
            return row['id']
        return self._write(apply)

    # The SSL Fax engine (HylaFAX+) --------------------------------------------

    def record_engine_call(self, event, *, preset=None, now=None):
        """A call the SSL Fax engine placed or answered (the FaxEngineCall events).

        Outbound calls send two events, in either order: the engine's channel
        (``Side: engine``: answered, ended, the gateway's T.38 session) and the
        trunk's (``Side: trunk``: its T.38 state, the RTCP packets it received,
        the carrier's Call-ID); each fills in only what it knows. Inbound rows
        are keyed ``engine.<token>``, the same key the engine's hand-over uses,
        and filled the same way. Returns the row's id.
        """
        now = now or utcnow()
        direction = 'inbound' if str(event.get('Direction') or '').lower() == 'in' else 'outbound'
        side = str(event.get('Side') or 'engine').strip().lower()
        answered, ended = _epoch(event.get('Answered')), _epoch(event.get('Ended')) or now
        started = _epoch(event.get('Started'))
        # T.38 ran when the gateway started a fax session (its number, 0 when none); the trunk's
        # PJSIP state at hang-up is back to audio by then, but still says ENABLED mid-call.
        state = str(event.get('T38') or '').strip().upper()
        session = str(event.get('T38Session') or '').strip()
        if (session.isdigit() and int(session) > 0) or state == 'ENABLED':
            t38 = 'yes'
        elif session == '0' or (side != 'trunk' and state in ('DISABLED', 'REJECTED')):
            t38 = 'no'
        else:
            t38 = 'unknown'
        cause = str(event.get('Cause') or '').strip()
        dial_status = str(event.get('DialStatus') or '').strip().upper()
        # Audio calls only: RTCP packets the trunk received (None when not reported).
        heard = _count(event.get('RtpRx')) if t38 != 'yes' else None
        heard = None if heard is None else heard > 0
        _log_gateway(event)
        if direction == 'outbound' and side == 'trunk':
            return self._record_trunk_side(event, t38, heard, now)
        if answered:
            disposition = 'answered'
        else:
            disposition = {'BUSY': 'busy', 'NOANSWER': 'no_answer', 'CONGESTION': 'congestion'}.get(
                dial_status) or {'17': 'busy', '18': 'no_answer', '19': 'no_answer', '34': 'congestion',
                                 '38': 'congestion', '42': 'congestion'}.get(cause, 'failed')
        sip_call_id = _sip_call_id(event.get('CallID64'))
        if direction == 'outbound':
            job_id, attempt_id = _identity(event.get('JobID')), _identity(event.get('AttemptID'))
            if job_id is None or attempt_id is None:
                return None

            def apply(connection, table):
                row = self._outbound_row(connection, table, job_id, attempt_id, now)
                changes = {'disposition': disposition, 'answered_at': row['answered_at'] or answered,
                           'ended_at': row['ended_at'] or ended}
                changes['connected_seconds'] = _seconds(changes['answered_at'], changes['ended_at']) or (
                    0 if not answered else None)
                if row['t38'] == 'unknown':
                    changes['t38'] = t38
                kept = row['error_cause'] if row['fax_status'] is None else None
                if not answered and (row['error_cause'] is None or kept in (_HEARD, _SILENT)):
                    changes['error_cause'] = _REASON_TEXT.get({'busy': '5', 'no_answer': '3', 'congestion': '8'}.get(
                        disposition, '0'), 'call failed')
                if sip_call_id and not row['sip_call_id']:
                    changes['sip_call_id'] = sip_call_id
                connection.execute(table.update().where(table.c.id == row['id']).values(updated_at=now, **changes))
                self._settle_engine(connection, table, row['id'], _kept_heard(kept), now)
                return row['id']
            return self._write(apply)
        token = re.sub(r'[^0-9]', '', str(event.get('Token') or ''))[:40]
        if not token:
            return None
        call_id = 'engine.' + token
        did, caller = _received(event.get('DID')), _received(event.get('Caller'))
        record = {
            'id': uuid4().hex, 'direction': 'inbound', 'call_id': call_id, 'job_id': None, 'attempt_id': None,
            'trunk_preset': str(preset or '')[:32] or None, 'did': did, 'caller': caller, 'called': did,
            'started_at': started or answered or now, 'answered_at': answered, 'ended_at': ended,
            'disposition': 'answered' if answered else 'failed', 'connected_seconds': _seconds(answered, ended),
            't38': t38, 'pages': None, 'fax_status': None, 'remote_station_id': None,
            'error_cause': None if answered else 'caller hung up before answer', 'fax_preference': 0,
            'sip_call_id': sip_call_id, 'trunk_key': _trunk(event.get('Trunk')), 'created_at': now, 'updated_at': now}

        def apply(connection, table):
            row = self._find(connection, table, 'inbound', call_id)
            if row is None:
                connection.execute(table.insert().values(**record))
                return record['id']
            changes = {name: record[name] for name in ('sip_call_id', 'answered_at', 'connected_seconds', 'did',
                                                       'caller', 'called', 'trunk_key')
                       if row[name] is None and record[name]}
            if row['t38'] == 'unknown' and t38 != 'unknown':
                changes['t38'] = t38
            # Asterisk's own times win over the engine's report time.
            changes['ended_at'] = ended
            if started:
                changes['started_at'] = started
            if record['connected_seconds'] is not None:
                changes['connected_seconds'] = record['connected_seconds']
            if changes:
                connection.execute(table.update().where(table.c.id == row['id']).values(updated_at=now, **changes))
            self._settle_engine(connection, table, row['id'], heard, now)
            return row['id']
        return self._write(apply)

    def _record_trunk_side(self, event, t38, heard, now):
        """The trunk's own event for an engine call it carried: the carrier's Call-ID, T.38 when the carrier
        leg was still in T.38 at hang-up, and on an audio call whether sound came back."""
        job_id, attempt_id = _identity(event.get('JobID')), _identity(event.get('AttemptID'))
        if job_id is None or attempt_id is None:
            return None
        sip_call_id = _sip_call_id(event.get('CallID64'))

        def apply(connection, table):
            row = self._outbound_row(connection, table, job_id, attempt_id, now)
            changes = {}
            if sip_call_id and not row['sip_call_id']:
                changes['sip_call_id'] = sip_call_id
            if t38 == 'yes' and row['t38'] == 'unknown':
                changes['t38'] = 'yes'
            if changes:
                connection.execute(table.update().where(table.c.id == row['id']).values(updated_at=now, **changes))
            self._settle_engine(connection, table, row['id'], heard if t38 != 'yes' else None, now)
            return row['id']
        return self._write(apply)

    def record_engine_result(self, job_id, attempt_id, *, success, pages=None, station=None, reason=None,
                             now=None):
        """What the SSL Fax engine reported for one sent fax: confirmed pages, the other machine, why it failed."""
        job_id, attempt_id = _identity(job_id), _identity(attempt_id)
        if job_id is None or attempt_id is None:
            return None
        now = now or utcnow()
        station = ''.join(character for character in str(station or '') if character.isprintable()).strip()[:40]
        cause = None if success else re.sub(r'[^A-Za-z0-9 _.,:-]', '', str(reason or 'fax failed'))[:64]

        def apply(connection, table):
            row = self._outbound_row(connection, table, job_id, attempt_id, now)
            if row['fax_status'] is not None:
                return row['id']  # The same result again (a report sent twice): the first one stands.
            kept = row['error_cause'] if row['fax_status'] is None else None
            changes = {'fax_status': 'SUCCESS' if success else 'FAILED', 'pages': _pages(pages),
                       'remote_station_id': station or row['remote_station_id'], 'error_cause': cause}
            connection.execute(table.update().where(table.c.id == row['id']).values(updated_at=now, **changes))
            self._settle_engine(connection, table, row['id'], _kept_heard(kept), now)
            return row['id']
        return self._write(apply)

    def record_engine_receive(self, call_id, *, success, pages=None, station=None, reason=None, did=None,
                              caller=None, inbound_fax_id=None, preset=None, trunk=None, now=None):
        """What the SSL Fax engine reported for one received call, fax or not: its result decides the
        call's verdict, whether Asterisk's event for the call came first or not."""
        call_id = str(call_id or '').strip()
        if not _CALL.fullmatch(call_id):
            return None
        now = now or utcnow()
        station = ''.join(character for character in str(station or '') if character.isprintable()).strip()[:40]
        cause = None if success else re.sub(r'[^A-Za-z0-9 _.,:-]', '', str(reason or 'fax failed'))[:64]
        did, caller = _number(did), _number(caller)
        values = {'fax_status': 'SUCCESS' if success else 'FAILED', 'pages': _pages(pages),
                  'remote_station_id': station or None, 'error_cause': cause}

        def apply(connection, table):
            row = self._find(connection, table, 'inbound', call_id)
            if row is None:
                record = {
                    'id': uuid4().hex, 'direction': 'inbound', 'call_id': call_id,
                    'job_id': _identity(inbound_fax_id), 'attempt_id': None,
                    'trunk_preset': str(preset or '')[:32] or None, 'did': did, 'caller': caller, 'called': did,
                    # The engine reports once its session is over; Asterisk's event brings the exact times.
                    'started_at': now, 'answered_at': None, 'ended_at': now, 'disposition': 'answered',
                    'connected_seconds': None, 't38': 'unknown', 'fax_preference': 0, 'sip_call_id': None,
                    'trunk_key': _trunk(trunk), 'created_at': now, 'updated_at': now, **values}
                connection.execute(table.insert().values(**record))
                self._settle_engine(connection, table, record['id'], None, now)
                return record['id']
            if row['fax_status'] is not None and row['fax_status'] != 'FAILED':
                return row['id']  # A received fax stays received.
            kept = row['error_cause'] if row['fax_status'] is None else None
            changes = dict(values)
            changes['disposition'] = 'answered'
            if row['ended_at'] is None:
                changes['ended_at'] = now
            for name, value in (('did', did), ('caller', caller), ('called', did), ('trunk_key', _trunk(trunk))):
                if row[name] is None and value:
                    changes[name] = value
            if row['job_id'] is None and _identity(inbound_fax_id):
                changes['job_id'] = _identity(inbound_fax_id)
            connection.execute(table.update().where(table.c.id == row['id']).values(updated_at=now, **changes))
            self._settle_engine(connection, table, row['id'], _kept_heard(kept), now)
            return row['id']
        return self._write(apply)

    def _settle_engine(self, connection, table, row_id, heard, now):
        """Once every part of an engine call is in (the engine's result, the call's end), in any order, store
        its verdict with the engine's words (``verdict: words``). Until then keep what the trunk heard on an
        audio call: alone while there is no result yet, or as a short mark after the engine's words."""
        row = connection.execute(sa.select(table).where(table.c.id == row_id)).mappings().one()
        if row['fax_status'] is None:
            if heard is not None and (row['error_cause'] is None or row['error_cause'] in (_HEARD, _SILENT)):
                connection.execute(table.update().where(table.c.id == row_id).values(
                    updated_at=now, error_cause=_HEARD if heard else _SILENT))
            return None
        cause = row['error_cause'] or ''
        mark = _HEARD_MARK.search(cause)
        if mark:
            heard = heard if heard is not None else mark.group(1) == '1'
            cause = cause[:mark.start()]
        settled = cause.split(':', 1)[0]
        if settled == NO_FAX_SIGNAL and heard is not None and row['t38'] != 'yes':
            # The trunk's event came last: whether sound came back on this audio call says more.
            words = cause.split(':', 1)[1].strip() if ':' in cause else cause
            found = engine_verdict({**row, 'error_cause': words}, heard)
            if found and found != settled:
                connection.execute(table.update().where(table.c.id == row_id).values(
                    updated_at=now, error_cause=(found + ': ' + words)[:64].rstrip()))
                return found
        if settled in VERDICTS:
            return settled
        if row['ended_at'] is None:
            if heard is not None and not mark and row['fax_status'] == 'FAILED':
                connection.execute(table.update().where(table.c.id == row_id).values(
                    updated_at=now, error_cause=cause[:59] + (' ~h1' if heard else ' ~h0')))
            return None
        found = engine_verdict({**row, 'error_cause': cause}, heard)
        if found is None:
            if mark:
                connection.execute(table.update().where(table.c.id == row_id).values(updated_at=now,
                                                                                      error_cause=cause or None))
            return None
        words = re.sub(r'[^A-Za-z0-9 _.,-]', '', cause) or 'fax failed'
        connection.execute(table.update().where(table.c.id == row_id).values(
            updated_at=now, error_cause=(found + ': ' + words)[:64].rstrip()))
        return found

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
            'fax_preference': 0, 'sip_call_id': _sip_call_id(call.get('sip_call_id_b64')),
            'trunk_key': _trunk(call.get('trunk')), 'created_at': now, 'updated_at': now}
        # A peer fax call from an enrolled partner inside its tunnel, with no carrier (direct/peer_call.py).
        if _peer(call.get('peer')):
            record['peer_id'] = _peer(call.get('peer'))
        return self._insert_inbound(record)

    def record_inbound_event(self, event, *, preset=None, now=None):
        """A received call that left no fax image (the FaxInboundCall manager event)."""
        call_id = str(event.get('UniqueID') or '').strip()
        if not _CALL.fullmatch(call_id):
            return None
        now = now or utcnow()
        started, answered = _epoch(event.get('Started')), _epoch(event.get('Answered'))
        ended = _epoch(event.get('Ended')) or now
        did, caller = _received(event.get('DID')), _received(event.get('Caller'))
        status = re.sub(r'[^A-Z_]', '', str(event.get('Status') or '').upper())[:16] or None
        handover = _handover(event)
        if handover is not None:
            error_cause = f'{NOT_HANDED_OVER}: {handover}'
        else:
            error_cause = _error_cause(event) if answered else 'caller hung up before answer'
        record = {
            'id': uuid4().hex, 'direction': 'inbound', 'call_id': call_id, 'job_id': None, 'attempt_id': None,
            'trunk_preset': str(preset or '')[:32] or None, 'did': did, 'caller': caller, 'called': did,
            'started_at': started or answered or now, 'answered_at': answered, 'ended_at': ended,
            'disposition': 'answered' if answered else 'failed', 'connected_seconds': _seconds(answered, ended),
            't38': _t38(event.get('Mode')), 'pages': _pages(event.get('Pages')), 'fax_status': status,
            'remote_station_id': _station(event.get('Station64')), 'error_cause': error_cause,
            'fax_preference': 0, 'sip_call_id': _sip_call_id(event.get('CallID64')),
            'trunk_key': _trunk(event.get('Trunk')), 'created_at': now, 'updated_at': now}
        return self._insert_inbound(record)

    def link_inbound(self, call_id, inbound_fax_id):
        """Point a received call that was not handed over at the fax Faxbot brought in later.

        Only an empty link is filled; what the call record observed stays as it was.
        """
        inbound_fax_id = _identity(inbound_fax_id)
        if inbound_fax_id is None or not _CALL.fullmatch(str(call_id or '')):
            return False

        def apply(connection, table):
            result = connection.execute(table.update().where(
                table.c.direction == 'inbound', table.c.call_id == call_id, table.c.job_id.is_(None)).values(
                job_id=inbound_fax_id, updated_at=utcnow()))
            return result.rowcount > 0
        return self._write(apply)

    def inbound_call(self, call_id):
        """The received call's numbers and pages, or None."""
        try:
            with self.engine.connect() as connection:
                row = self._find(connection, self.table, 'inbound', str(call_id))
        except (sa.exc.SQLAlchemyError, SipCallRecordError):
            return None
        if row is None:
            return None
        found = {'did': row['did'], 'caller': row['caller'], 'pages': row['pages']}
        if row.get('trunk_key'):
            found['trunk'] = row['trunk_key']  # a trunk after the first; none is the first trunk
        return found

    def unclaimed_inbound_calls(self):
        """Calls whose image Asterisk stored but could not hand over, not linked to a fax yet."""
        try:
            with self.engine.connect() as connection:
                table = self.table
                rows = connection.execute(sa.select(table.c.call_id).where(
                    table.c.direction == 'inbound', table.c.job_id.is_(None),
                    table.c.error_cause.like(NOT_HANDED_OVER + ':%'))).scalars().all()
        except (sa.exc.SQLAlchemyError, SipCallRecordError):
            return set()
        return set(rows)

    def _insert_inbound(self, record):
        def apply(connection, table):
            existing = self._find(connection, table, 'inbound', record['call_id'])
            if existing is not None and record.get('sip_call_id') and not existing['sip_call_id']:
                # A later report of the same call may carry the SIP Call-ID; only an empty one is filled.
                connection.execute(table.update().where(table.c.id == existing['id']).values(
                    sip_call_id=record['sip_call_id']))
            if existing is not None:
                return existing['id']
            connection.execute(table.insert().values(**{key: value for key, value in record.items()
                                                        if key in table.c or key != 'peer_id'}))
            return record['id']
        return self._write(apply)

    # Reads -------------------------------------------------------------------

    @staticmethod
    def _public(row, country=None):
        result = {name: row[name] for name in COLUMNS}
        if result['direction'] == 'inbound' and country:
            # Rows stored before received numbers were read for the country keep their stored form; they are
            # shown (and named in the summary) the way new rows are stored.
            for name in ('did', 'caller', 'called'):
                result[name] = _received(result[name], country) or result[name]
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

    def call(self, row_id):
        """One call record by its id, or None."""
        table = self.table
        try:
            with self.engine.connect() as connection:
                row = connection.execute(sa.select(table).where(table.c.id == row_id)).mappings().one_or_none()
        except sa.exc.SQLAlchemyError:
            raise SipCallRecordError('Call records are unavailable.') from None
        return self._public(row, _installation_country()) if row is not None else None

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
        country = _installation_country() if any(row['direction'] == 'inbound' for row in rows) else None
        return {'items': [self._public(row, country) for row in rows], 'next_cursor': next_cursor}


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
_record_fax_result = _safely('record_fax_result')


# The built-in engine's negotiation (fax_negotiation): its own step after the call record, so a failure here
# never loses the call record, and neither ever reaches the fax.
_BUILTIN_CALL = re.compile(r'[0-9]{1,20}\.[0-9]{1,10}', re.ASCII)


def record_builtin_negotiation(engine, *, direction, call_key, rate, resolution, pages, job_id=None, number=None):
    """The last page's speed and resolution the built-in engine reported for one call. Never raises."""
    from . import fax_negotiation, hylafax_records
    try:
        values = fax_negotiation.builtin_values(rate, resolution, pages)
        records = hylafax_records.records_for(engine)
    except Exception:
        return None
    return hylafax_records.safely(records.record_negotiation, direction=direction, call_key=call_key,
                                  engine='builtin', values=values, job_id=job_id, number=number)


def _on_fax_result(event):
    _record_fax_result(event)
    records = _current
    attempt_id, job_id = _identity(event.get('AttemptID')), _identity(event.get('JobID'))
    if records is None or attempt_id is None or job_id is None:
        return
    record_builtin_negotiation(records.engine, direction='outbound', call_key=attempt_id, rate=event.get('Rate'),
                               resolution=event.get('Resolution'), pages=event.get('Pages'), job_id=job_id)


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
    call_id = str(event.get('UniqueID') or '').strip()
    if _BUILTIN_CALL.fullmatch(call_id):
        record_builtin_negotiation(records.engine, direction='inbound', call_key=call_id, rate=event.get('Rate'),
                                   resolution=event.get('Resolution'), pages=event.get('Pages'),
                                   number=_number(event.get('Caller')))


def _on_engine_call(event):
    records = _current
    if records is None:
        return
    try:
        row_id = records.record_engine_call(event, preset=_active_preset())
        row = records.call(row_id) if row_id else None
    except Exception:
        logging.getLogger(__name__).warning('A SIP call record could not be saved.')
        return
    # The engine's result may have come first: the audio rule runs on whichever half is last.
    engine_audio_check(row)


def _on_engine_missed(event):
    """A received call the engine's free lines did not answer: the engine starts again once no call is up
    (hylafax/entrypoint.sh reads the request), and the trunk page says why. Never raises."""
    from . import hylafax_engine
    try:
        from .config import configuration_values
        values = configuration_values()
        started = int(str(event.get('Started') or '0').strip() or 0)
        if not hylafax_engine.request_restart(values, reason='missed_call', at=started or None):
            return
        from .audit import audit_event
        audit_event('sip_engine_restart_requested', backend='sip', reason='missed_call',
                    lines=re.sub(r'[^A-Za-z0-9:, ]', '', str(event.get('Lines') or ''))[:80])
    except Exception:
        logging.getLogger(__name__).warning('A fax call the fax engine did not answer could not be recorded.')


def engine_audio_check(row):
    """An engine call on T.38 on which the engine heard no fax machine moves the engine (not the built-in
    engine, not the installation's T.38 setting) to audio fax from its next call on. Runs when any part of
    the call arrives; recorded once. Call from the event loop."""
    if row is None or row.get('verdict') != NO_FAX_SIGNAL or row.get('t38') != 'yes':
        return
    from . import hylafax_engine
    hylafax_engine.engine_t38_failed(at=row.get('ended_at'))


def attach(ami_client, engine):
    """Record calls from this AMI client into ``engine``; safe to call on every start."""
    global _current
    _current = SipCallRecords(engine)
    ami_client.on_submission(_on_submission)
    ami_client.on_originate_response(_on_originate_response)
    ami_client.on_fax_result(_on_fax_result)
    ami_client.on_inbound_call(_on_inbound_call)
    # Calls the SSL Fax engine (HylaFAX+) placed or answered through the trunk.
    ami_client.on_engine_call(_on_engine_call)
    on_missed = getattr(ami_client, 'on_engine_missed', None)
    if on_missed is not None:
        on_missed(_on_engine_missed)
    return _current


def detach():
    global _current
    _current = None


def record_inbound_call(engine, call, *, call_id, inbound_fax_id, preset=None, fax_status=None):
    """Record a received call's details; the fax itself is already stored, so failures only log."""
    try:
        records = SipCallRecords(engine)
        recorded = records.record_inbound(call, call_id=call_id, inbound_fax_id=inbound_fax_id,
                                          preset=preset, fax_status=fax_status)
        # A hand-over that was reported as failed but reached Faxbot later links its call here.
        records.link_inbound(call_id, inbound_fax_id)
    except Exception:
        logging.getLogger(__name__).warning('A SIP call record could not be saved.')
        return None
    # Received by the built-in engine (Asterisk's own call name): what the call negotiated. The SSL Fax
    # engine's calls ('engine.<token>', 'hylafax.<...>') report theirs through the engine's hand-over.
    if isinstance(call, dict) and _BUILTIN_CALL.fullmatch(str(call_id or '')):
        record_builtin_negotiation(engine, direction='inbound', call_key=str(call_id), rate=call.get('rate'),
                                   resolution=call.get('resolution'), pages=call.get('pages'),
                                   job_id=inbound_fax_id, number=_number(call.get('caller')))
    return recorded
