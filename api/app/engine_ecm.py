"""What error correction and T.38 did on Faxbot's own fax calls, and the settings Faxbot chooses from it (brief 98,
item 1; research faxbot-deep-opportunities-2026-10-08 sections 2 and 3).

Records (``fax_call_ecm``, migration 0080), one row per call and source, never rewritten:

- ``builtin``: the built-in engine's own count, from Asterisk patch 0008 (the FaxFrames event's ``Ecm``, ``T38Rx``
  and ``T38Protect``): the error-correction frame size, pages, partial pages and their frames, the requests to send
  frames again and how many, the runs of neighbouring frames a block's first request asked for, continue-to-correct
  requests, the timed turnarounds (block edges, rounds of resent frames), and how T.38 arrived (packets received,
  recovered by UDPTL redundancy, still missing).
- ``hylafax``: the same error-correction counts from the SSL Fax engine's session log (``hylafax/bin/negotiation``).
- ``gateway``: how T.38 arrived at the gateway on an SSL Fax engine call (the FaxEngineCall event's ``T38Rx``).

What Faxbot chooses from them:

- **The frame size** (``choose_frame``). With error correction a page goes in frames of 256 octets, or 64 when the
  sender asks. A damaged frame is sent again whole, so smaller frames resend less after scattered errors; but every
  frame carries the same 7 octets of framing, and a partial page holds 256 frames, so 64-octet frames mean four times
  as many block edges (each a turnaround of about 3.3 seconds, measured) on a page over 16 KB. Which wins depends on
  the error shape: how often errors start (bursts per octet on the line) and how far each one reaches (a burst spoils
  its neighbours: the runs a block's first request names). ``page_seconds`` is the expected time on the line for
  one page under that shape, block edges, framing and retransmission rounds included; Faxbot asks for 64-octet frames
  only when that saves at least ``FRAME_MARGIN_SECONDS`` on the number's typical page, from at least ``MIN_CALLS``
  calls with error correction to the number (else on the trunk). Losses are often bursty, so the shape is fitted from
  the runs, not assumed independent; with no errors on record 256 always wins (less framing).
- **The turnaround** of one partial page (``turnaround``): the median of the measured block edges once ``MIN_CALLS``
  calls timed any, else the loopback's 3.3 seconds (``pages.coding.ECM_BLOCK_SECONDS``). The predictor prices
  partial pages with it (``routing.error_correction``).
- **T.38 protection by packet category** (``protection_for``). From the packets the far end sent on calls over a
  trunk, what share were lost before UDPTL redundancy repaired them (``t38_loss``; the path's loss, which Faxbot's
  own repeats do not change, so the choice does not flap). From ``LOSS_LOW`` (0.5%) Faxbot sends each indicator
  and end-of-data packet three times and each control packet twice; from ``LOSS_HIGH`` (3%) four and three. Page
  data always goes once: error correction repairs it, and repeating it would double the call's bandwidth. A lost
  control packet stalls the call for seconds (T.30 waits, then sends again), so that is where bandwidth buys most.

Every figure here is measured on calls Faxbot already made; nothing places a call to find out.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
import re
import statistics
import uuid
import weakref

import sqlalchemy as sa

TABLE = 'fax_call_ecm'
SOURCES = ('builtin', 'hylafax', 'gateway')
FRAME_OVERHEAD = 7  # octets of framing per error-correction frame (routing.error_correction.FRAME_OVERHEAD_OCTETS)
FRAMES_PER_BLOCK = 256
MIN_CALLS = 3  # as engine_learning.MIN_CALLS: no change before this many calls show it
# A text page measured 1 to 30 KB coded (research faxbot-novel-vectors-2026-10-08, ecm-blocks-results.json); the
# number's own pages replace it once its calls are on record.
TYPICAL_PAGE_OCTETS = 30_000
# A smaller frame must save at least this on a typical page before Faxbot asks for it.
FRAME_MARGIN_SECONDS = 0.5
LOSS_LOW, LOSS_HIGH = 0.005, 0.03
# spandsp's five T.38 categories: indicator, control data, end of control data, image data, end of image data.
PROTECT_LOW, PROTECT_HIGH = '3.2.3.1.3', '4.3.4.1.4'
ECM_FIELDS = ('frame_octets', 'pps', 'blocks', 'frames', 'ppr', 'bad_frames', 'bad_runs', 'longest_run', 'resent',
              'ctc', 'edges', 'edge_ms', 'rounds', 'round_ms', 'small_frames', 'pages')
_PROTECT = re.compile(r'[1-4](?:\.[1-4]){4}')
_KEY = re.compile(r'[A-Za-z0-9._:-]{1,100}')
_ID = re.compile(r'[A-Za-z0-9_-]{1,40}')
_TRUNK = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')


class EcmStoreError(RuntimeError):
    """The error-correction records cannot be read or written (a database before migration 0080, or unavailable)."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# -- reading what the engines report ----------------------------------------------------------------------------

def _count(text, limit=10_000_000):
    try:
        value = int(str(text))
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= limit else None


def parse_ecm(text) -> dict | None:
    """Patch 0008's FAXBOT_ECM ("frame.pps.blocks.frames.ppr.bad.runs.longest.resent.ctc.edges.edge_ms.rounds.
    round_ms.small.pages"), or None when it is not one. A call without error correction gives frame_octets None."""
    parts = str(text or '').strip().split('.')
    if len(parts) != len(ECM_FIELDS):
        return None
    values = [_count(part, 1_000_000_000) for part in parts]
    if any(value is None for value in values):
        return None
    found = dict(zip(ECM_FIELDS, values))
    if found['frame_octets'] not in (0, 64, 256):
        return None
    found['frame_octets'] = found['frame_octets'] or None
    found['small_frames'] = 1 if found['small_frames'] else 0
    return found


def parse_t38_rx(text) -> dict | None:
    """Patch 0008's FAXBOT_T38_RX ("received.recovered.missing"), or None."""
    parts = str(text or '').strip().split('.')
    if len(parts) != 3:
        return None
    values = [_count(part) for part in parts]
    if any(value is None for value in values):
        return None
    return {'t38_received': values[0], 't38_recovered': values[1], 't38_missing': values[2]}


def parse_protect(text) -> str | None:
    text = str(text or '').strip()
    return text if _PROTECT.fullmatch(text) else None


def trunk_of_endpoint(endpoint) -> str | None:
    """The trunk account an Asterisk endpoint belongs to: 'sip' for trunk-endpoint (the first trunk), the key for
    trunk-<key>-endpoint, None for anything else (a partner's peer endpoint)."""
    text = str(endpoint or '').strip()
    if text == 'trunk-endpoint':
        return 'sip'
    found = re.fullmatch(r'trunk-([a-z0-9][a-z0-9_-]{0,63})-endpoint', text)
    return found.group(1) if found else None


def frames_row(event) -> dict | None:
    """A ``builtin`` row from the built-in engine's FaxFrames event (Asterisk patch 0008), or None when the event
    carries neither error correction nor T.38 counts. The far end's number of a sent call comes from its fax."""
    direction = {'out': 'outbound', 'in': 'inbound'}.get(str(event.get('Direction') or ''))
    ecm, t38 = parse_ecm(event.get('Ecm')), parse_t38_rx(event.get('T38Rx'))
    if direction is None or (ecm is None and t38 is None) or (ecm and not ecm['frame_octets'] and t38 is None):
        return None
    attempt = str(event.get('AttemptID') or '')
    unique = str(event.get('UniqueID') or '')
    key = attempt if direction == 'outbound' else unique
    if not _KEY.fullmatch(key):
        return None
    caller = re.sub(r'[^0-9+]', '', str(event.get('Caller') or ''))[:32] or None
    job = str(event.get('JobID') or '')
    trunk = trunk_of_endpoint(event.get('Endpoint')) if direction == 'outbound' else (
        str(event.get('Trunk') or '').strip() or 'sip')
    return {'direction': direction, 'call_key': key, 'source': 'builtin',
            'number': caller if direction == 'inbound' else None,
            'trunk': trunk if trunk and _TRUNK.fullmatch(trunk) else None,
            'job_id': job if _ID.fullmatch(job) else None,
            'attempt_id': attempt if _ID.fullmatch(attempt) else None,
            **(ecm or {}), **(t38 or {}), 't38_protect': parse_protect(event.get('T38Protect'))}


def gateway_row(event) -> dict | None:
    """A ``gateway`` row from a FaxEngineCall event (the gateway on an SSL Fax engine call), or None without T.38
    counts. A sent call is keyed by its attempt, a received one as its call record is (engine.<token>)."""
    t38 = parse_t38_rx(event.get('T38Rx'))
    direction = {'out': 'outbound', 'in': 'inbound'}.get(str(event.get('Direction') or ''))
    if t38 is None or direction is None or (direction == 'outbound' and event.get('Side') != 'engine'):
        return None
    if direction == 'outbound':
        key = str(event.get('AttemptID') or '')
        number = None
        trunk = trunk_of_endpoint(event.get('Endpoint'))
    else:
        token = re.sub(r'[^0-9]', '', str(event.get('Token') or ''))
        key = f'engine.{token}' if token else ''
        number = re.sub(r'[^0-9+]', '', str(event.get('Caller') or ''))[:32] or None
        trunk = str(event.get('Trunk') or '').strip() or 'sip'
    if not _KEY.fullmatch(key):
        return None
    job = str(event.get('JobID') or '')
    return {'direction': direction, 'call_key': key, 'source': 'gateway', 'number': number,
            'trunk': trunk if trunk and _TRUNK.fullmatch(trunk) else None,
            'job_id': job if _ID.fullmatch(job) else None,
            'attempt_id': key if direction == 'outbound' and _ID.fullmatch(key) else None,
            **t38, 't38_protect': parse_protect(event.get('T38Protect'))}


def engine_log_row(values, *, direction, call_key, number=None, trunk=None, job_id=None, attempt_id=None) -> dict | None:
    """A ``hylafax`` row from the SSL Fax engine's negotiation values (``fax_negotiation.engine_values``: its
    ``ecm_*`` fields from the session log), or None when the call reported no error correction."""
    if not isinstance(values, dict) or not values.get('ecm_frame') or not _KEY.fullmatch(str(call_key or '')):
        return None
    if not isinstance(values.get('ecm_frame'), int):
        return None
    frame = values.get('ecm_frame')
    if frame not in (64, 256):
        return None
    names = {'ecm_pps': 'pps', 'ecm_blocks': 'blocks', 'ecm_frames': 'frames', 'ecm_ppr': 'ppr',
             'ecm_bad': 'bad_frames', 'ecm_runs': 'bad_runs', 'ecm_longest': 'longest_run', 'ecm_resent': 'resent',
             'ecm_ctc': 'ctc', 'ecm_edges': 'edges', 'ecm_edge_ms': 'edge_ms', 'ecm_rounds': 'rounds',
             'ecm_round_ms': 'round_ms', 'ecm_pages': 'pages'}
    row = {'direction': direction, 'call_key': str(call_key), 'source': 'hylafax', 'number': number,
           'trunk': trunk if trunk and _TRUNK.fullmatch(str(trunk)) else None,
           'job_id': job_id if job_id and _ID.fullmatch(job_id) else None,
           'attempt_id': attempt_id if attempt_id and _ID.fullmatch(attempt_id) else None,
           'frame_octets': frame, 'small_frames': 1 if values.get('ecm_small') else 0}
    for given, column in names.items():
        row[column] = _count(values.get(given), 1_000_000_000)
    return row


def record_engine_report(engine, *, direction, call_key, negotiation_b64, small=False, number=None, trunk=None,
                         job_id=None, attempt_id=None) -> bool:
    """Keep the SSL Fax engine's error-correction counts for one call (its session log, ``bin/negotiation``);
    ``small``: the job asked for 64-byte frames (``bin/notify``'s ``ecm_small``). True when a row was kept."""
    from .fax_negotiation import _report
    data = dict(_report(negotiation_b64))
    if data.get('ecm_frame') and small:
        data.update(ecm_frame=64, ecm_small=1)
    row = engine_log_row(data, direction=direction, call_key=call_key, number=number, trunk=trunk, job_id=job_id,
                         attempt_id=attempt_id)
    return EcmStore(engine).record(row) if row is not None else False


# -- storage --------------------------------------------------------------------------------------------------

_REFLECTED = weakref.WeakKeyDictionary()


class EcmStore:
    """``fax_call_ecm``: append-only, one row per call and source. Its tables are reflected once per database
    (every price reads it; the schema changes only at startup)."""

    def __init__(self, engine):
        self.engine = engine
        try:
            self.table, self.jobs = _REFLECTED[engine]
            return
        except (KeyError, TypeError):
            pass
        try:
            metadata = sa.MetaData()
            metadata.reflect(engine, only=[TABLE, 'fax_jobs'])
        except sa.exc.SQLAlchemyError:
            raise EcmStoreError('The error-correction records are not available.') from None
        self.table = metadata.tables[TABLE]
        self.jobs = metadata.tables['fax_jobs']
        try:
            _REFLECTED[engine] = (self.table, self.jobs)
        except TypeError:
            pass

    def record(self, row, *, now=None) -> bool:
        """Keep one call's record once; True when new. A sent call's far end comes from its fax."""
        row = {key: value for key, value in dict(row).items() if key in self.table.c}
        try:
            with self.engine.begin() as connection:
                found = connection.execute(sa.select(self.table.c.id).where(
                    self.table.c.direction == row['direction'], self.table.c.call_key == row['call_key'],
                    self.table.c.source == row['source'])).first()
                if found is not None:
                    return False
                if not row.get('number') and row.get('job_id'):
                    row['number'] = connection.execute(sa.select(self.jobs.c.to_number).where(
                        self.jobs.c.id == row['job_id'])).scalar()
                connection.execute(self.table.insert().values(id=uuid.uuid4().hex, created_at=now or utcnow(),
                                                              **row))
        except sa.exc.IntegrityError:
            return False
        except sa.exc.SQLAlchemyError as error:
            raise EcmStoreError('An error-correction record could not be kept.') from error
        return True

    def rows(self, *, number=None, trunk=None, direction='outbound', since=None, limit=200) -> list:
        """Records, newest first: of calls with ``number`` (as stored or presented), or on ``trunk``."""
        table = self.table
        query = sa.select(table)
        if direction:
            query = query.where(table.c.direction == direction)
        if since is not None:
            query = query.where(table.c.created_at >= since)
        if trunk is not None:
            query = query.where(table.c.trunk == trunk)
        if number is not None:
            digits = re.sub(r'[^0-9]', '', number)
            if len(digits) < 3:
                return []
            query = query.where(table.c.number.like('%' + digits[-7:]))
        try:
            with self.engine.connect() as connection:
                found = [dict(row) for row in connection.execute(
                    query.order_by(table.c.created_at.desc()).limit(limit * (3 if number else 1))).mappings()]
        except sa.exc.SQLAlchemyError as error:
            raise EcmStoreError('The error-correction records could not be read.') from error
        if number is not None:
            from .engine_frames import same_number
            found = [row for row in found if same_number(row['number'], number)][:limit]
        return found


# -- the error shape and the frame size ----------------------------------------------------------------------

@dataclass(frozen=True)
class ErrorShape:
    """How errors fell on calls with error correction: bursts per octet on the line and how far each reached."""
    calls: int
    exposure: int        # octets of first-pass image data on the line, framing included
    runs: int            # runs of neighbouring frames named in blocks' first requests
    bad: int             # frames those requests named
    frame_octets: float  # the frame size the evidence came from (mean, weighted by frames)
    page_octets: float | None = None  # coded octets of a typical page on those calls

    @property
    def bursts_per_octet(self) -> float:
        return self.runs / self.exposure if self.exposure else 0.0

    @property
    def burst_octets(self) -> float:
        """Octets one burst spoils beyond the frame it starts in: a burst of L octets starting at random spans
        1 + L / (frame size with framing) frames on average, so L follows from the mean run length."""
        if not self.runs:
            return 0.0
        return max(0.0, self.bad / self.runs - 1.0) * (self.frame_octets + FRAME_OVERHEAD)


def shape_of(rows) -> ErrorShape | None:
    """The ``ErrorShape`` from error-correction records (one per call; the newest source per call), or None
    without any."""
    calls = exposure = runs = bad = frames = 0
    weighted = 0.0
    page_octets = []
    seen = set()
    for row in rows:
        frame, count = row.get('frame_octets'), row.get('frames')
        if frame not in (64, 256) or not count or (row['direction'], row['call_key']) in seen:
            continue
        seen.add((row['direction'], row['call_key']))
        calls += 1
        exposure += count * (frame + FRAME_OVERHEAD)
        frames += count
        weighted += count * frame
        runs += row.get('bad_runs') or 0
        bad += row.get('bad_frames') or 0
        if row.get('pages'):
            page_octets.append(count * frame / row['pages'])
    if not calls:
        return None
    return ErrorShape(calls, exposure, runs, bad, weighted / frames,
                      statistics.median(page_octets) if page_octets else None)


def page_seconds(octets, frame, rate, turnaround, shape=None) -> float:
    """Expected seconds one page of ``octets`` coded octets takes on the line with error correction at ``frame``
    octets a frame and ``rate`` bit/s: its frames with their framing, a ``turnaround`` per block edge past the
    first, and the rounds of resent frames the error ``shape`` predicts in each block (a round costs a turnaround
    and the frames sent again; the resent frames may be hit again, and a second round is counted). The page
    exchange at the end of the page is the same for every frame size and is left out."""
    if frame not in (64, 256) or not rate or octets <= 0:
        raise ValueError('A page, a frame size of 64 or 256 octets and a speed are needed.')
    size = frame + FRAME_OVERHEAD
    frames = math.ceil(octets / frame)
    blocks = math.ceil(frames / FRAMES_PER_BLOCK)
    seconds = frames * size * 8 / rate + (blocks - 1) * turnaround
    rate_per_octet = shape.bursts_per_octet if shape is not None else 0.0
    reach = shape.burst_octets if shape is not None else 0.0
    left = frames
    for _ in range(blocks):
        count = min(FRAMES_PER_BLOCK, left)
        left -= count
        exposed = count * size
        for _round in range(2):
            bursts = rate_per_octet * exposed
            if bursts <= 0:
                break
            bad = min(count, bursts * (1 + reach / size))
            seconds += (1 - math.exp(-bursts)) * turnaround + bad * size * 8 / rate
            count, exposed = bad, bad * size
    return seconds


def choose_frame(shape, *, page_octets, rate, turnaround) -> tuple:
    """(frame octets, expected seconds at 64, at 256) for one typical page: 64 only when it saves at least
    ``FRAME_MARGIN_SECONDS``."""
    small = page_seconds(page_octets, 64, rate, turnaround, shape)
    usual = page_seconds(page_octets, 256, rate, turnaround, shape)
    return (64 if usual - small >= FRAME_MARGIN_SECONDS else 256), small, usual


def turnaround(rows) -> tuple:
    """(seconds, calls): the median block-edge turnaround over calls that timed one, once ``MIN_CALLS`` did;
    (None, calls) before that."""
    timed = [row['edge_ms'] / row['edges'] / 1000 for row in rows if row.get('edges') and row.get('edge_ms')]
    if len(timed) < MIN_CALLS:
        return None, len(timed)
    return round(statistics.median(timed), 2), len(timed)


# -- T.38 loss and protection ---------------------------------------------------------------------------------

def t38_loss(rows) -> tuple:
    """(share lost before UDPTL redundancy, share still missing after it, calls, packets) over records with T.38
    counts; shares None without any."""
    received = recovered = missing = calls = 0
    for row in rows:
        if row.get('t38_received') is None:
            continue
        calls += 1
        received += row['t38_received']
        recovered += row.get('t38_recovered') or 0
        missing += row.get('t38_missing') or 0
    sent = received + missing
    if not sent:
        return None, None, calls, 0
    return (recovered + missing) / sent, missing / sent, calls, sent


def protection(loss, calls) -> str | None:
    """The counts for FAXBOT_T38_PROTECT for a trunk whose calls lost ``loss`` of their packets, or None."""
    if loss is None or calls < MIN_CALLS or loss < LOSS_LOW:
        return None
    return PROTECT_HIGH if loss >= LOSS_HIGH else PROTECT_LOW


def protection_sentence(loss, calls, counts) -> str:
    share = f'{loss * 100:.1f}%'.replace('.0%', '%')
    noun = 'call' if calls == 1 else 'calls'
    if counts is None:
        return (f'Fax-over-IP calls on this trunk lost {share} of their packets ({calls} {noun} recently), so Faxbot '
                'sends each packet once.')
    times = 'three' if counts == PROTECT_LOW else 'four'
    return (f'Fax-over-IP calls on this trunk lost {share} of their packets ({calls} {noun} recently), so Faxbot '
            f'sends the short packets that carry the fax handshake up to {times} times; page data still goes once, '
            'as error correction repairs it.')


# -- what Faxbot uses ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Learned:
    """What Faxbot uses for error correction on a call to one number, with one sentence each."""
    frame_octets: int = 256
    frame_why: str | None = None
    block_seconds: float | None = None
    block_calls: int = 0
    shape: ErrorShape | None = None
    shape_scope: str | None = None   # 'number' or 'trunk'


def _shape_sentence(shape, scope):
    where = 'calls to this number' if scope == 'number' else 'calls on your trunk'
    return (f'{shape.calls} {where} with error correction had {shape.runs} damaged stretch'
            f"{'' if shape.runs == 1 else 'es'} the far machine asked for again")


def learned(engine, number, *, since, rate=14400, now=None) -> Learned:
    """``Learned`` for a call to ``number`` from records since ``since`` (inside the learning epoch). Records that
    cannot be read leave the usual settings; the cause is logged."""
    try:
        store = EcmStore(engine)
        own = store.rows(number=number, since=since)
        trunk = store.rows(since=since, limit=400)
    except EcmStoreError as error:
        logging.getLogger(__name__).warning('Error-correction records could not be read (%s); calls use 256-octet '
                                            'frames.', error)
        return Learned()
    seconds, timed = turnaround(trunk)
    from .pages.coding import ECM_BLOCK_SECONDS
    turn = seconds if seconds is not None else ECM_BLOCK_SECONDS
    shape = shape_of(own)
    scope = 'number'
    if shape is None or shape.calls < MIN_CALLS:
        shape, scope = shape_of(trunk), 'trunk'
    if shape is None or shape.calls < MIN_CALLS:
        return Learned(block_seconds=seconds, block_calls=timed if seconds is not None else 0)
    page = (shape_of(own) or shape).page_octets or TYPICAL_PAGE_OCTETS
    frame, small, usual = choose_frame(shape, page_octets=page, rate=rate, turnaround=turn)
    why = None
    if frame == 64:
        saved = usual - small
        why = (f'Faxbot sends this number error-correction frames of 64 bytes, not 256: '
               f'{_shape_sentence(shape, scope)}, and a smaller frame means less to send again; about '
               f'{saved:.1f} seconds less on a typical page.').replace('.0 seconds', ' seconds')
    return Learned(frame, why, seconds, timed if seconds is not None else 0, shape, scope)


def trunk_view(engine, values, *, days=30, now=None) -> dict:
    """What the trunk page shows (Delivery setup → the trunk, "How fax calls went"; ``faxbot delivery providers trunk
    negotiation``): what error correction did on sent calls over ``days`` days, the measured turnaround of a block,
    and each trunk's T.38 protection with its sentence. Unreadable records say so in one sentence."""
    from datetime import timedelta
    from . import engine_learning, sip_trunk
    moment = now or utcnow()
    try:
        rows = EcmStore(engine).rows(direction=None, since=moment - timedelta(days=days), limit=1000)
    except EcmStoreError:
        return {'sentences': ['What error correction did on recent calls cannot be read right now.'], 'trunks': []}
    sent = shape_of([row for row in rows if row['direction'] == 'outbound'])
    seconds, timed = turnaround(rows)
    sentences = []
    if sent is None:
        sentences.append(f'No sent call in the last {days} days has reported what error correction did yet.')
    else:
        calls = 'call' if sent.calls == 1 else 'calls'
        sentences.append(f'On {sent.calls} sent {calls} with error correction in the last {days} days, the receiving '
                         f'machines asked for {sent.bad} damaged frames again, in {sent.runs} stretches.')
    if seconds is not None:
        sentences.append(f'Each block of a page after the first took about {seconds:.1f} seconds more on the line '
                         f'({timed} calls).'.replace('.0 seconds', ' seconds'))
    trunks = []
    for trunk in sip_trunk.trunk_accounts(values):
        found = engine_learning.t38_protection(engine, values, trunk.key, now=moment)
        trunks.append({'trunk': trunk.key, 'label': trunk.label, 'protect': found.counts, 'calls': found.calls,
                       'loss_percent': None if found.loss is None else round(found.loss * 100, 2),
                       'sentence': (f'{trunk.label}: {found.sentence}' if found.sentence else
                                    f'{trunk.label}: Faxbot has not measured fax-over-IP loss on this trunk yet, so '
                                    'each packet goes once.')})
    return {'sentences': sentences, 'trunks': trunks}


@dataclass(frozen=True)
class Protection:
    """T.38 protection by packet category on one trunk: the counts (None: every packet once) and one sentence."""
    counts: str | None
    loss: float | None
    residual: float | None
    calls: int
    sentence: str | None


def protection_for(engine, trunk, *, since) -> Protection:
    """The protection for calls over ``trunk`` ('sip' for the first trunk), from the T.38 the far ends sent on its
    calls since ``since``, both directions. Unreadable records give none (the cause is logged)."""
    try:
        rows = EcmStore(engine).rows(trunk=trunk or 'sip', direction=None, since=since, limit=400)
    except EcmStoreError as error:
        logging.getLogger(__name__).warning('T.38 records could not be read (%s); packets go once.', error)
        return Protection(None, None, None, 0, None)
    loss, residual, calls, _ = t38_loss(rows)
    counts = protection(loss, calls)
    sentence = protection_sentence(loss, calls, counts) if loss is not None and calls >= MIN_CALLS else None
    return Protection(counts, loss, residual, calls, sentence)
