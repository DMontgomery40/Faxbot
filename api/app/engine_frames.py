"""What the far end's fax machine said, and what Faxbot learns from it (M8, M5, M6, M9).

Asterisk patch 0004 keeps the T.30 frames of every built-in engine fax
session and the dialplan reports them in one FaxFrames event per call, sent
and received (``extensions.conf``, ``[faxbot-frames]``). This module reads
that event, decodes the frames (ITU-T T.30 Table 2, bit numbers as spandsp
0.0.6's ``t30_dis_dtc_dcs_bits.h``) and keeps one row per call in
``fax_call_frames`` (migration 0036), written once and never changed.

From those rows, per number and only from calls Faxbot was already making
(never a probing call):

- **Early T.38 (M5).** When a number's last calls (at least ``EARLY_T38_CALLS``)
  all switched to T.38 only because Faxbot asked, about ten seconds after the
  answer (SendFAX's three CNG tones), later calls ask at once
  (``FAXBOT_T38_NOW``). The trunk's own T.38 setting comes first: with T.38
  off, never.
- **Starting speed (M6).** When a number's calls keep failing to train at a
  speed and then train cleanly at a lower one, later calls start at the lower
  speed (``FAXBOT_MAXRATE``), skipping the failed training. A lower setting
  already in force (yours, or the recipient's own limits) always wins.
- **Received speed (M6).** A caller whose own fax line trained cleanly at
  14,400 bit/s over audio on Faxbot's calls to it gets that speed when it
  calls in over audio, not the 9,600 every other caller gets
  (``faxbot-inrate`` in Asterisk's database).

Learning expires: only calls in the last ``LEARN_DAYS`` days on the same trunk
(carrier preset and server) count, and a value needs enough calls to stand.

Internet Aware Fax (M9) is never learned. It applies only to a fax server an
administrator approved here (``fax_iaf_endpoints``) or to an enrolled direct
partner whose record says it is IAF capable (Builder AJ's migration 0034 adds
that flag; this module reads it when it exists). It sets ``FAXBOT_IAF`` on
calls to that number and, through ``faxbot-iaf`` in Asterisk's database, on
calls from it.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import uuid

import sqlalchemy as sa


EARLY_T38_CALLS = 3
# Faxbot's own request comes after about 10.5 s (three CNG tones); a switch this late was Faxbot's fallback.
LATE_T38_MS = 9000
LEARN_DAYS = 30
RATE_CALLS = 2
FAMILY_IAF = 'faxbot-iaf'
FAMILY_INRATE = 'faxbot-inrate'
_HEX = re.compile(r'(?:[0-9a-f]{2}){0,32}')
_RATES = re.compile(r'(?:[0-9a-f]{2}(?:\.[0-9a-f]{2}){0,15})?')

# DCS speed codes (FIF octet 2 & 0x3C), as spandsp 0.0.6's fallback table.
DCS_RATES = {0x20: 14400, 0x28: 12000, 0x24: 9600, 0x04: 9600, 0x2C: 7200, 0x0C: 7200, 0x08: 4800, 0x00: 2400}
DCS_MODEMS = {0x20: 'V.17', 0x28: 'V.17', 0x24: 'V.17', 0x04: 'V.29', 0x2C: 'V.17', 0x0C: 'V.29', 0x08: 'V.27ter',
              0x00: 'V.27ter'}
# DIS modem codes: the far end's highest speed.
DIS_RATES = {0x2C: 14400, 0x0C: 9600, 0x04: 9600, 0x08: 4800, 0x00: 2400}
RATES = (14400, 12000, 9600, 7200, 4800, 2400)

IAF_KINDS = ('peer', 'endpoint')
NO_LABEL = 'Name this fax server, such as "Head office SR140".'
NOT_A_NUMBER = '{number} is not a fax number Faxbot can read. Enter it with its country code, such as +13035550100.'


class FramesRefused(ValueError):
    """A change refused with one plain sentence."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# -- decoding --------------------------------------------------------------------------------

def _octets(text):
    text = str(text or '').strip().lower()
    return bytes.fromhex(text) if text and _HEX.fullmatch(text) else b''


def _bit(fif, number):
    index = (number - 1) // 8
    return index < len(fif) and bool(fif[index] & (1 << ((number - 1) % 8)))


def decode_dis(text) -> dict | None:
    """The far end's capabilities from its DIS (hex frame: address, control, FCF, FIF), or None."""
    frame = _octets(text)
    if len(frame) < 5 or frame[2] not in (0x80, 0x81):
        return None
    fif = frame[3:]
    modem = fif[1] & 0x3C
    width = (fif[2] & 0x03) if len(fif) > 2 else 0
    length = 'unlimited' if _bit(fif, 19) else 'B4' if _bit(fif, 20) else 'A4'
    return {
        'max_rate': DIS_RATES.get(modem),
        'v17': modem == 0x2C, 'v29': modem in (0x2C, 0x0C, 0x04), 'v27ter': modem in (0x2C, 0x0C, 0x08, 0x00),
        'fine': _bit(fif, 15), 'superfine': _bit(fif, 41), 'r300': _bit(fif, 42),
        'mr': _bit(fif, 16), 'ecm': _bit(fif, 27), 'mmr': _bit(fif, 27) and _bit(fif, 31),
        'jbig': _bit(fif, 27) and (_bit(fif, 78) or _bit(fif, 79)),
        'widths': ('A4', 'A4 and B4', 'A4, B4 and A3', 'A4')[width], 'length': length,
        'letter': _bit(fif, 76), 'legal': _bit(fif, 77),
        'subaddress': _bit(fif, 49), 'password': _bit(fif, 50),
        't38_iaf': _bit(fif, 3), 't38_continuous': _bit(fif, 123),
        'internet_routing': _bit(fif, 102),
    }


def dcs_rate(text) -> int | None:
    frame = _octets(text)
    if len(frame) < 5 or (frame[2] & 0xFE) != 0x82:
        return None
    return DCS_RATES.get(frame[4] & 0x3C)


def decode_rates(text) -> list:
    """Each DCS's speed in bit/s, in order, from the dot-separated codes."""
    text = str(text or '').strip().lower()
    if not text or not _RATES.fullmatch(text):
        return []
    return [rate for rate in (DCS_RATES.get(int(code, 16)) for code in text.split('.')) if rate]


def decode_sub(text) -> str | None:
    """A SUB frame's subaddress: 20 characters sent last first (T.30 5.3.6.2.4), blanks trimmed."""
    frame = _octets(text)
    if len(frame) < 4 or (frame[2] & 0xFE) != 0xC2:
        return None
    chars = ''.join(chr(octet) for octet in reversed(frame[3:]) if 32 <= octet < 127).strip()
    return chars or None


def decode_address(text, fcf) -> dict | None:
    """A CSA or TSA frame's internet address: its type octet and the address characters."""
    frame = _octets(text)
    if len(frame) < 5 or (frame[2] & 0xFE) != fcf:
        return None
    address = ''.join(chr(octet) for octet in frame[4:] if 32 <= octet < 127).strip()
    return {'type': frame[3], 'address': address} if address else None


# -- the event ---------------------------------------------------------------------------------

def _field(event, name, pattern=None, limit=64):
    value = str(event.get(name, '') or '').strip()[:limit]
    if pattern is not None and not re.fullmatch(pattern, value):
        return ''
    return value


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_event(event, *, trunk='') -> dict | None:
    """One fax_call_frames row (without the far end's number for a sent fax) from a FaxFrames event, or None."""
    direction = _field(event, 'Direction', r'in|out')
    if not direction:
        return None
    job_id = _field(event, 'JobID', r'[A-Za-z0-9_-]{1,40}') or None
    attempt_id = _field(event, 'AttemptID', r'[A-Za-z0-9_-]{1,40}') or None
    call_key = _field(event, 'UniqueID', r'[0-9.]{1,40}') or None
    if direction == 'out' and not attempt_id:
        return None
    if direction == 'in' and not call_key:
        return None
    frames = {name: _field(event, key, r'(?:[0-9a-f]{2}){0,32}') or None
              for name, key in (('dis', 'Dis'), ('dcs_first', 'DcsFirst'), ('dcs_last', 'DcsLast'), ('csa', 'Csa'),
                                ('tsa', 'Tsa'), ('sub', 'Sub'), ('nsf', 'Nsf'))}
    rates_text = _field(event, 'Rates', _RATES.pattern) or None
    rates = decode_rates(rates_text)
    answered, t38_at = _int(_field(event, 'Answered', r'[0-9]{1,12}')), _int(_field(event, 'T38At', r'[0-9]{1,16}'))
    t38_after = None
    if answered and t38_at and t38_at >= answered * 1000:
        t38_after = min(t38_at - answered * 1000, 2_000_000_000)
    sent = _field(event, 'DcsSent', r'[01]')
    number = None
    if direction == 'in':
        number = _field(event, 'Caller', r'\+?[0-9]{3,20}') or None
    return {
        'id': f"{direction}:{attempt_id if direction == 'out' else call_key}",
        'direction': direction, 'job_id': job_id, 'attempt_id': attempt_id, 'call_key': call_key,
        'number': number, 'did': _field(event, 'DID', r'\+?[0-9]{3,20}') or None,
        'mode': _field(event, 'Mode', r'T38|audio') or None,
        'status': _field(event, 'Status', r'[A-Z_]{1,32}') or None,
        **frames, 'dcs_sent': int(sent) if sent else None, 'rates': rates_text,
        'rate_first': rates[0] if rates else None, 'rate_lowest': min(rates) if rates else None,
        'trainings': _int(_field(event, 'Trainings', r'[0-9]{1,6}')), 'ftt': _int(_field(event, 'Ftt', r'[0-9]{1,6}')),
        't38_after_ms': t38_after, 't38_by': _field(event, 'T38By', r'far|faxbot') or None,
        't38_now': 1 if _field(event, 'T38Now', r'yes') else 0,
        'iaf': _field(event, 'Iaf', r'peer|endpoint') or None, 'trunk': str(trunk or '')[:300] or None,
    }


def trunk_key(values) -> str:
    """The trunk a call used, for expiring what Faxbot learned when it changes: carrier preset and server."""
    preset = getattr(values, 'sip_trunk_preset', '') or ''
    host = getattr(values, 'sip_trunk_host', '') or ''
    return f'{preset}@{host}'[:300]


def _digits(number):
    return re.sub(r'[^0-9]', '', number or '')


def same_number(left, right) -> bool:
    """Two numbers as stored and as a caller presents them (with or without the country code)."""
    a, b = _digits(left), _digits(right)
    if not a or not b:
        return False
    return a == b or (len(a) > len(b) and a.endswith(b) and len(b) >= 7) or (len(b) > len(a) and b.endswith(a)
                                                                            and len(a) >= 7)


# -- storage -------------------------------------------------------------------------------

class FrameStore:
    """fax_call_frames and fax_iaf_endpoints (migration 0036)."""

    def __init__(self, engine):
        self.engine = engine
        metadata = sa.MetaData()
        metadata.reflect(engine, only=['fax_call_frames', 'fax_iaf_endpoints', 'fax_jobs'])
        self.frames = metadata.tables['fax_call_frames']
        self.endpoints = metadata.tables['fax_iaf_endpoints']
        self.jobs = metadata.tables['fax_jobs']

    def record(self, row, *, now=None) -> bool:
        """Keep one call's frames, once; True when new. A sent fax's far end comes from its job."""
        row = dict(row)
        now = now or utcnow()
        with self.engine.begin() as connection:
            if connection.execute(sa.select(self.frames.c.id).where(self.frames.c.id == row['id'])).first():
                return False
            if row['direction'] == 'out' and row.get('job_id') and not row.get('number'):
                row['number'] = connection.execute(sa.select(self.jobs.c.to_number)
                                                   .where(self.jobs.c.id == row['job_id'])).scalar()
            try:
                connection.execute(self.frames.insert().values(**row, created_at=now))
            except sa.exc.IntegrityError:
                return False
        return True

    def calls(self, number, *, direction=None, since=None, trunk=None, limit=20) -> list:
        """The latest calls with a number, newest first."""
        digits = _digits(number)
        if not digits:
            return []
        query = sa.select(self.frames).where(self.frames.c.number.like('%' + digits[-7:]))
        if direction:
            query = query.where(self.frames.c.direction == direction)
        if since is not None:
            query = query.where(self.frames.c.created_at >= since)
        if trunk is not None:
            query = query.where(self.frames.c.trunk == trunk)
        with self.engine.connect() as connection:
            rows = connection.execute(query.order_by(self.frames.c.created_at.desc()).limit(limit * 3)).mappings()
            return [dict(row) for row in rows if same_number(row['number'], number)][:limit]

    def for_attempt(self, attempt_id) -> dict | None:
        with self.engine.connect() as connection:
            row = connection.execute(sa.select(self.frames).where(self.frames.c.id == f'out:{attempt_id}')).mappings().first()
        return dict(row) if row else None

    # Approved IAF fax servers ------------------------------------------------------------------
    def endpoints_list(self, *, include_removed=False) -> list:
        query = sa.select(self.endpoints)
        if not include_removed:
            query = query.where(self.endpoints.c.removed_at.is_(None))
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query.order_by(self.endpoints.c.created_at)).mappings()]

    def add_endpoint(self, number, kind, label, *, actor_id=None, actor_name=None, now=None) -> dict:
        if kind not in IAF_KINDS:
            raise FramesRefused('Choose whether this is another Faxbot or an IAF fax server.')
        label = ' '.join(str(label or '').split())[:200]
        if not label:
            raise FramesRefused(NO_LABEL)
        row = {'id': uuid.uuid4().hex, 'number': number, 'kind': kind, 'label': label, 'added_by': actor_id,
               'added_by_name': (actor_name or None) and str(actor_name)[:200], 'created_at': now or utcnow(),
               'removed_at': None, 'removed_by': None, 'removed_by_name': None}
        with self.engine.begin() as connection:
            connection.execute(self.endpoints.insert().values(**row))
        return row

    def remove_endpoint(self, endpoint_id, *, actor_id=None, actor_name=None, now=None) -> dict | None:
        with self.engine.begin() as connection:
            connection.execute(self.endpoints.update()
                               .where(self.endpoints.c.id == endpoint_id, self.endpoints.c.removed_at.is_(None))
                               .values(removed_at=now or utcnow(), removed_by=actor_id,
                                       removed_by_name=(actor_name or None) and str(actor_name)[:200]))
            row = connection.execute(sa.select(self.endpoints).where(self.endpoints.c.id == endpoint_id)).mappings().first()
        return dict(row) if row else None


def partner_iaf_numbers(engine) -> set:
    """Enrolled direct partners marked IAF capable (Builder AJ's 0034 flag), by fax number; empty without it."""
    try:
        inspector = sa.inspect(engine)
        if not inspector.has_table('direct_peers'):
            return set()
        columns = {column['name'] for column in inspector.get_columns('direct_peers')}
        flag = next((name for name in ('iaf_capable', 'iaf') if name in columns), None)
        if flag is None or 'phone_number' not in columns:
            return set()
        peers = sa.Table('direct_peers', sa.MetaData(), autoload_with=engine)
        # The same partners direct delivery uses: verified and not expired (RouteStore.verified_peer).
        query = sa.select(peers.c.phone_number).where(
            peers.c[flag] == 1, peers.c.state == 'verified',
            sa.or_(peers.c.expires_at.is_(None), peers.c.expires_at > utcnow()))
        with engine.connect() as connection:
            return {value for value in connection.execute(query).scalars() if value}
    except sa.exc.SQLAlchemyError:
        return set()


def iaf_for(store, engine, number) -> str | None:
    """'peer' or 'endpoint' for a number approved for Internet Aware Fax; None for every other number."""
    for endpoint in store.endpoints_list():
        if same_number(endpoint['number'], number):
            return endpoint['kind']
    if any(same_number(partner, number) for partner in partner_iaf_numbers(engine)):
        return 'peer'
    return None


# -- what Faxbot learns -----------------------------------------------------------------------

@dataclass(frozen=True)
class Learned:
    t38_now: bool = False
    t38_reason: str = ''
    max_rate: int | None = None
    rate_reason: str = ''
    inbound_rate: int | None = None
    calls: int = 0


def _allowed_rate(rate):
    """The highest starting speed Faxbot can ask for (FAXBOT_MAXRATE) at or below ``rate``."""
    return next((allowed for allowed in (14400, 9600, 7200, 4800) if allowed <= rate), None)


def learn(calls, values) -> Learned:
    """What a number's recent calls (newest first, same trunk, within LEARN_DAYS) teach, with one reason each.

    The evidence for a setting is only calls made without it, so a setting in force does not undo itself;
    calls made with it keep it unless one contradicts it. Each setting ends when its evidence ages out.
    """
    sent = [call for call in calls if call['direction'] == 'out']
    t38_now, t38_reason = False, ''
    if getattr(values, 'sip_t38_enabled', True):
        waited = [call for call in sent if not call['t38_now']][:EARLY_T38_CALLS]
        early = [call for call in sent if call['t38_now']][:EARLY_T38_CALLS]
        late = (len(waited) >= EARLY_T38_CALLS
                and all(call['t38_by'] == 'faxbot' and (call['t38_after_ms'] or 0) >= LATE_T38_MS for call in waited))
        # Asking at once and then ending up on audio (the far end refused it) contradicts it.
        refused = any(call['mode'] == 'audio' for call in early)
        if late and not refused:
            t38_now = True
            t38_reason = (f'The last {len(waited)} faxes to this number that waited switched to fax over IP only when '
                          'Faxbot asked, about ten seconds after the answer, so Faxbot now asks at once.')
    max_rate, rate_reason = None, ''
    trained = [call for call in sent if call['rate_first'] and call['rate_lowest'] and call['status'] == 'SUCCESS']
    failing = [call for call in trained if (call['ftt'] or 0) > 0 and call['rate_lowest'] < call['rate_first']]
    if len(failing) >= RATE_CALLS:
        lowest = max(call['rate_lowest'] for call in failing[:RATE_CALLS])
        candidate = _allowed_rate(lowest)
        newest_failure = failing[0]['created_at'] if 'created_at' in failing[0] else None
        # Failing again at the learned speed, or a newer call that started higher and trained cleanly, ends it.
        worse = any(call['rate_first'] <= (candidate or 0) and (call['ftt'] or 0) > 0 for call in trained)
        better = any(call['rate_first'] > (candidate or 0) and not call['ftt']
                     and (newest_failure is None or call.get('created_at') is None or call['created_at'] > newest_failure)
                     for call in trained)
        if candidate and not worse and not better:
            max_rate = candidate
            rate_reason = (f'Faxes to this number failed to train at {failing[0]["rate_first"]:,} bit/s and went '
                           f'through at {lowest:,}, so Faxbot starts at {max_rate:,} bit/s.')
    clean = [call for call in sent if call['mode'] == 'audio' and call['rate_first'] == 14400
             and (call['ftt'] or 0) == 0 and call['status'] == 'SUCCESS']
    inbound_rate = 14400 if len(clean) >= RATE_CALLS else None
    return Learned(t38_now, t38_reason, max_rate, rate_reason, inbound_rate, len(calls))


def learned_for(store, values, number, *, now=None) -> Learned:
    now = now or utcnow()
    return learn(store.calls(number, since=now - timedelta(days=LEARN_DAYS), trunk=trunk_key(values)), values)


@dataclass(frozen=True)
class CallOptions:
    t38_now: bool = False
    iaf: str | None = None
    max_rate: int | None = None


def call_options(values, number, *, engine=None) -> CallOptions:
    """For one sent fax: T.38 at once, Internet Aware Fax and a learned starting speed. Never raises."""
    if engine is None:
        return CallOptions()
    try:
        store = FrameStore(engine)
        learned = learned_for(store, values, number)
        return CallOptions(learned.t38_now, iaf_for(store, engine, number), learned.max_rate)
    except Exception:
        return CallOptions()


# -- Asterisk's database: IAF and received speed per caller -----------------------------------

def caller_keys(number) -> set:
    """The digit strings a carrier may present for a caller, as the dialplan looks them up."""
    from .inbound.screening import keys_for, read_number, ScreeningRefused
    try:
        return keys_for(read_number(number if str(number).startswith('+') else '+' + _digits(number)))
    except (ScreeningRefused, Exception):
        digits = _digits(number)
        return {digits} if len(digits) >= 3 else set()


def desired_families(store, engine, values, *, now=None) -> dict:
    """{family: {key: value}} for faxbot-iaf and faxbot-inrate."""
    now = now or utcnow()
    iaf = {}
    for endpoint in store.endpoints_list():
        for key in caller_keys(endpoint['number']):
            iaf[key] = endpoint['kind']
    for number in partner_iaf_numbers(engine):
        for key in caller_keys(number):
            iaf.setdefault(key, 'peer')
    inrate = {}
    since = now - timedelta(days=LEARN_DAYS)
    with store.engine.connect() as connection:
        numbers = {row for row in connection.execute(
            sa.select(store.frames.c.number).where(store.frames.c.direction == 'out',
                                                   store.frames.c.created_at >= since,
                                                   store.frames.c.number.is_not(None)).distinct()).scalars()}
    for number in sorted(numbers):
        if learned_for(store, values, number, now=now).inbound_rate == 14400:
            for key in caller_keys(number):
                inrate[key] = '14400'
    return {FAMILY_IAF: iaf, FAMILY_INRATE: inrate}


async def sync(ami, store, engine, values, *, now=None) -> dict:
    """Make Asterisk's faxbot-iaf and faxbot-inrate keys exactly what Faxbot knows; {family: changes}."""
    import asyncio
    from .inbound.screening import _tree
    desired = await asyncio.to_thread(desired_families, store, engine, values, now=now)
    changes = {}
    for family, keys in desired.items():
        current = await _tree(ami, family)
        count = 0
        for key, value in current.items():
            if keys.get(key) != value:
                await ami.db_del(family, key)
                count += 1
        for key, value in keys.items():
            if current.get(key) != value:
                await ami.db_put(family, key, value)
                count += 1
        changes[family] = count
    return changes


# -- in words ---------------------------------------------------------------------------------

def describe(row) -> list:
    """Plain sentences about one call's frames, for the console and the command line."""
    lines = []
    dis = decode_dis(row.get('dis'))
    if dis:
        rate = f"{dis['max_rate']:,} bit/s" if dis['max_rate'] else 'an unknown speed'
        what = ['error correction' if dis['ecm'] else 'no error correction']
        if dis['mmr']:
            what.append('MMR compression')
        if dis['jbig']:
            what.append('JBIG compression')
        if dis['superfine']:
            what.append('superfine pages')
        elif dis['fine']:
            what.append('fine pages')
        lines.append(f"The far end's fax machine accepts up to {rate}, with {', '.join(what)}, on "
                     f"{dis['widths']} paper of {'any length' if dis['length'] == 'unlimited' else dis['length'] + ' length'}.")
    if row.get('rate_first'):
        trainings = row.get('trainings') or 0
        text = f"It started at {row['rate_first']:,} bit/s"
        if row.get('rate_lowest') and row['rate_lowest'] != row['rate_first']:
            text += f" and went down to {row['rate_lowest']:,}"
        failures = row.get('ftt') or 0
        text += f', in {trainings} training{"" if trainings == 1 else "s"}'
        if failures:
            text += f', {failures} of them failed'
        lines.append(text + '.')
    if row.get('t38_after_ms') is not None:
        seconds = row['t38_after_ms'] / 1000
        who = 'the far end asked for it' if row.get('t38_by') == 'far' else 'Faxbot asked for it'
        lines.append(f'Fax over IP started {seconds:.1f} seconds after the answer; {who}.')
    address = decode_address(row.get('csa'), 0x24)
    if address:
        lines.append(f"The far end gave its internet address: {address['address']}.")
    sub = decode_sub(row.get('sub'))
    if sub:
        lines.append(f'The sender named subaddress {sub}.')
    if row.get('iaf'):
        lines.append('Sent as Internet Aware Fax: faster than a fax line, to a fax server you approved.')
    return lines
