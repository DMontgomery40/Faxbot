"""Collecting a fax by polling (M21): your side places the call and the other fax server sends the fax it holds.

When two sites of one organization fax each other and the receiving site's
outbound calls are cheaper (a flat plan, a toll-free route), the receiving
site can place the call and collect the document with ITU-T T.30 polling: it
dials, sends DTC instead of waiting to be called, and the other machine sends
the document it holds. Between two Faxbots, direct delivery is better; this is
for a site whose other end runs a different fax server that holds faxes for
polling (many fax servers and office machines do; "polled transmission").

What runs where (verified in source, 2026-10-08):

- Faxbot's SSL Fax engine (HylaFAX+ 7.0.11) can poll: ``hylafax_engine
  .create_poll_job`` (hfaxd ``JPARM POLL``; faxsend ``sendPoll``; Class 1
  ``pollBegin``). What it receives is handed over like any received fax
  (``hylafax/bin/pollrcvd``) and lands in Received.
- It can be polled too, with Faxbot's patch (hylafax/patches/0002-polled-
  transmit.patch): a fax held in its ``pollq`` for a number is offered in the
  DIS that number gets (bit 9, "ready to transmit") and sent when the caller
  answers DTC (``hold``, ``hylafax_engine.hold_document``). Stock HylaFAX+
  never sets bit 9 and ends a call that brings DTC (faxd/Class1Recv.c++, E107).
- Faxbot's built-in engine (Asterisk's SendFAX and ReceiveFAX on spandsp) does
  neither: SendFAX always runs spandsp as the calling party and neither
  application has a polling option (docs.asterisk.org; res_fax_spandsp.c).

Rules:

- **Per-number opt-in** (``poll_sources``, append-only; the newest row of a
  direction counts). Faxbot never polls a number you have not turned on, and
  never holds a fax for a number you have not turned on either: both are
  settings a person makes for one number, never automatic for an outside
  party. Each collection is asked for by a person (the console's "Collect
  now" or ``faxbot recipients collect``), or by the timetable that person set
  for the number (``due``, off by default).
- **One call, one try.** A collection that ended uncertain (the call dropped
  while a document was arriving) is never collected again by itself; a
  timetable skips a number whose last collection is uncertain until a person
  collects it by hand.
- **Passwords** (T.30 PWD) are sealed with the installation key (``PollSeal``)
  and go only into the engine's job (collecting) or the engine's sidecar for a
  held fax; they are never shown or logged.
- **Advice** (``advice``): from the faxes this number sent you in the last 30
  days, what your own trunk would pay to place those calls.

Records: ``poll_requests`` (written before the call) and ``poll_results``
(once), migration 0059; ``poll_held`` and ``poll_collections`` (one row per
event, the engine's report reference kept once), migration 0062; never
rewritten.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import re
import uuid
import weakref

import sqlalchemy as sa


TABLES = ('poll_sources', 'poll_requests', 'poll_results', 'poll_held', 'poll_collections')
OUTCOMES = ('received', 'nothing_waiting', 'refused', 'failed', 'uncertain', 'not_sent')
COLLECTION_OUTCOMES = ('sent', 'refused', 'failed', 'withdrawn')
DIRECTIONS = ('collect', 'hold')
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
ADVICE_DAYS = 30
_HEX32 = re.compile(r'[a-f0-9]{32}', re.ASCII)
_TABLES = weakref.WeakKeyDictionary()

NOT_ON = 'Turn on collecting faxes from this number first.'
HOLD_NOT_ON = 'Turn on holding faxes for this number first.'
NOT_SENT = "Faxbot's fast fax service could not take the call, so nothing was dialed."
NOT_HELD = "Faxbot's fast fax service could not take the fax, so it is not held."
WAITING = 'Faxbot is calling the other fax server to collect the fax it holds for you.'
HELD_WAITING = 'Waiting for the other site to call and collect it.'
ADVICE_NOTE = ('Collecting works only when the other fax server holds the fax for you to collect. Turn it on only '
               "for your organization's own sites, and only after the other site has set its fax server to hold "
               'faxes for you.')
HOLD_NOTE = ('A held fax goes out only when this number calls Faxbot and asks for it, so that site pays for the '
             "call. Turn it on only for your organization's own sites, and give them the selective polling "
             'address and password you set here.')


class PollRefused(ValueError):
    """A documented refusal: polling is not turned on for this number, or the SSL Fax engine cannot take the call;
    the sentence says which. Nothing was dialed."""


class PollStoreError(RuntimeError):
    """The polling records cannot be read or written now (before migration 0062, or the database is down)."""


def _tables(engine):
    found = _TABLES.get(engine)
    if found is None:
        from .database import DeliveryStoreError, reflect
        try:
            found = reflect(engine, TABLES)
        except DeliveryStoreError:
            raise PollStoreError('Collecting faxes is not available until the database is upgraded.') from None
        _TABLES[engine] = found
    return found


class PollSeal:
    """Seals T.30 polling passwords with the installation configuration key, bound to the fax number (as the
    other per-number secrets are). Only whether a password is set is ever read back for display."""
    KIND = 'polling_password'

    def __init__(self, configuration):
        self.configuration = configuration

    def _context(self):
        with self.configuration.engine.connect() as connection:
            head = self.configuration._head(connection)
        if head is None:
            raise PollStoreError('Installation configuration is not ready.')
        return self.configuration._cipher(), head['installation_id']

    def seal(self, password, number):
        cipher, installation = self._context()
        return cipher.seal({'password': password}, installation_id=installation, kind=self.KIND, record_id=number)

    def open(self, envelope, number):
        cipher, installation = self._context()
        payload = cipher.open(envelope, installation_id=installation, kind=self.KIND, record_id=number)
        password = payload.get('password')
        return password if isinstance(password, str) else None


@dataclass(frozen=True)
class Source:
    """Your setting for one number and one direction: collecting faxes from it, or holding faxes for it."""
    number: str
    enabled: bool
    label: str | None
    selective: str | None
    recorded_by_name: str | None
    created_at: datetime
    direction: str = 'collect'
    has_password: bool = False
    password_sealed: str | None = None
    collect_times: str | None = None
    collect_days: str | None = None
    time_zone: str | None = None


def _selective(value):
    """The T.30 selective polling address (SEP): digits, * and #, at most 20; '' for none."""
    text = re.sub(r'\s', '', str(value or ''))
    if text and not re.fullmatch(r'[0-9*#]{1,20}', text):
        raise ValueError('Give the selective polling address as up to 20 digits.')
    return text


def _password(value):
    """The T.30 polling password (PWD): digits, * and #, at most 20; '' for none."""
    text = re.sub(r'\s', '', str(value or ''))
    if text and not re.fullmatch(r'[0-9*#]{1,20}', text):
        raise ValueError('Give the polling password as up to 20 digits.')
    return text


def _times(value):
    """A timetable's times of day, "08:00,16:00"; '' for none."""
    parts = [part.strip() for part in str(value or '').split(',') if part.strip()]
    for part in parts:
        if not re.fullmatch(r'([01][0-9]|2[0-3]):[0-5][0-9]', part):
            raise ValueError('Give each collection time as HH:MM, such as 08:00 or 16:30.')
    return ','.join(sorted(set(parts)))[:60]


def _days(value):
    """A timetable's days, "mon,tue,wed,thu,fri"; '' for none."""
    parts = [part.strip().lower()[:3] for part in str(value or '').split(',') if part.strip()]
    for part in parts:
        if part not in DAYS:
            raise ValueError('Give the collection days as mon, tue, wed, thu, fri, sat or sun.')
    return ','.join(day for day in DAYS if day in parts)


def _zone(value):
    text = str(value or '').strip()
    if text:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Give the time zone by its name, such as America/Denver.') from None
    return text[:64]


def _from_row(row):
    return Source(row['number'], bool(row['enabled']), row['label'], row['selective'], row['recorded_by_name'],
                  row['created_at'], direction=row['direction'] or 'collect',
                  has_password=row['password_sealed'] is not None, password_sealed=row['password_sealed'],
                  collect_times=row['collect_times'], collect_days=row['collect_days'], time_zone=row['time_zone'])


def _newest(engine, number, direction):
    table = _tables(engine)['poll_sources']
    where = table.c.direction == direction
    if direction == 'collect':
        where = sa.or_(table.c.direction.is_(None), table.c.direction == 'collect')
    with engine.connect() as connection:
        return connection.execute(sa.select(table).where(table.c.number == number, where).order_by(
            table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().first()


def source(engine, number) -> Source | None:
    """The newest collecting setting for ``number``, or None (off)."""
    row = _newest(engine, number, 'collect')
    return _from_row(row) if row is not None else None


def hold_setting(engine, number) -> Source | None:
    """The newest holding setting for ``number`` (whether it may collect faxes from Faxbot), or None (off)."""
    row = _newest(engine, number, 'hold')
    return _from_row(row) if row is not None else None


def save_source(engine, number, *, enabled, label=None, selective=None, actor=None, actor_name=None, now=None,
                direction='collect', password=None, seal=None, collect_times=None, collect_days=None,
                time_zone=None):
    """A new setting row for ``number`` and ``direction`` (rows are never changed); returns the ``Source``.

    ``password``: None keeps the password the newest row has, '' clears it, and digits seal a new one with
    ``seal`` (a ``PollSeal``). The timetable fields work the same way: None keeps, '' clears."""
    if direction not in DIRECTIONS:
        raise ValueError('Unsupported polling direction')
    label = (str(label).strip()[:100] or None) if label else None
    selective = _selective(selective) or None
    previous = _newest(engine, number, direction)
    envelope = previous['password_sealed'] if previous is not None else None
    if password is not None:
        password = _password(password)
        if password and seal is None:
            raise PollStoreError('Polling passwords cannot be kept until the installation key is ready.')
        envelope = seal.seal(password, number) if password else None
    times = previous['collect_times'] if (previous is not None and collect_times is None) else (_times(collect_times) or None)
    days = previous['collect_days'] if (previous is not None and collect_days is None) else (_days(collect_days) or None)
    zone = previous['time_zone'] if (previous is not None and time_zone is None) else (_zone(time_zone) or None)
    if direction == 'hold':
        times = days = zone = None
    now = now or datetime.utcnow()
    table = _tables(engine)['poll_sources']
    with engine.begin() as connection:
        connection.execute(table.insert().values(
            id=uuid.uuid4().hex, number=number, enabled=1 if enabled else 0, label=label, selective=selective,
            recorded_by=str(actor)[:40] if actor else None, recorded_by_name=str(actor_name)[:200] if actor_name else None,
            created_at=now, direction=direction, password_sealed=envelope, collect_times=times, collect_days=days,
            time_zone=zone))
    return Source(number, bool(enabled), label, selective, actor_name, now, direction=direction,
                  has_password=envelope is not None, password_sealed=envelope, collect_times=times,
                  collect_days=days, time_zone=zone)


def record_request(engine, request_id, number, *, selective=None, engine_job=None, actor=None, actor_name=None,
                   now=None):
    if not _HEX32.fullmatch(str(request_id or '')):
        raise ValueError('Unsupported polling request')
    table = _tables(engine)['poll_requests']
    with engine.begin() as connection:
        connection.execute(table.insert().values(
            id=request_id, number=number, selective=selective or None, engine_job=engine_job,
            requested_by=str(actor)[:40] if actor else None,
            requested_by_name=str(actor_name)[:200] if actor_name else None, requested_at=now or datetime.utcnow()))


def is_request(engine, request_id) -> bool:
    """Whether ``request_id`` is a collection Faxbot asked for (an engine result's tag)."""
    if not _HEX32.fullmatch(str(request_id or '')):
        return False
    try:
        table = _tables(engine)['poll_requests']
    except PollStoreError:
        return False
    with engine.connect() as connection:
        return connection.execute(sa.select(table.c.id).where(table.c.id == request_id)).scalar() is not None


def record_result(engine, request_id, outcome, sentence, *, pages=None, inbound_fax_id=None, seconds=None,
                  now=None):
    """What one collection brought back, once; a second result for the same request keeps the first (an engine
    report sent again). A collection that brought a fax gets its result from the hand-over that carries the fax
    (``link_received``), never from the engine's job report alone. Returns the outcome kept."""
    if outcome not in OUTCOMES:
        raise ValueError('Unsupported polling outcome')
    table = _tables(engine)['poll_results']
    with engine.begin() as connection:
        kept = connection.execute(sa.select(table.c.outcome).where(table.c.request_id == request_id)).scalar()
        if kept is not None:
            return kept
        connection.execute(table.insert().values(
            id=uuid.uuid4().hex, request_id=request_id, outcome=outcome, pages=pages, inbound_fax_id=inbound_fax_id,
            seconds=seconds, sentence=str(sentence)[:300], created_at=now or datetime.utcnow()))
    return outcome


def link_received(engine, request_id, inbound_fax_id, *, pages=None, now=None):
    """The fax a collection brought into Received: its result (received, with the fax), once."""
    return record_result(engine, request_id, 'received', 'The other fax server sent the fax it held for you.',
                         pages=pages, inbound_fax_id=inbound_fax_id, now=now)


def requests(engine, number, *, limit=10) -> list:
    """The newest collections for ``number`` with what each brought back (None while the call is up)."""
    tables = _tables(engine)
    asked, results = tables['poll_requests'], tables['poll_results']
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(asked, results.c.outcome, results.c.sentence, results.c.pages, results.c.inbound_fax_id)
            .select_from(asked.outerjoin(results, results.c.request_id == asked.c.id))
            .where(asked.c.number == number).order_by(asked.c.requested_at.desc(), asked.c.id.desc())
            .limit(limit)).mappings().all()
    return [dict(row) for row in rows]


# Advice -------------------------------------------------------------------------------------------------------

def received_from(engine, number, *, now, days=ADVICE_DAYS):
    """(faxes, pages, seconds on the line) of trunk calls that brought faxes from ``number`` in the last ``days``
    (``sip_call_records``, both engines)."""
    from .database import reflect
    records = reflect(engine, ('sip_call_records',))['sip_call_records']
    with engine.connect() as connection:
        rows = connection.execute(sa.select(records.c.caller, records.c.pages, records.c.connected_seconds).where(
            records.c.direction == 'inbound', records.c.fax_status == 'SUCCESS',
            records.c.started_at > now - timedelta(days=days), records.c.started_at <= now,
            records.c.caller.like('%' + re.sub(r'[^0-9]', '', number)[-7:]))).mappings().all()
    from ..engine_frames import same_number
    rows = [row for row in rows if same_number(row['caller'], number)]
    return len(rows), sum(row['pages'] or 0 for row in rows), sum(row['connected_seconds'] or 0 for row in rows)


def advice(engine, number, *, now=None, predict=None):
    """One sentence on what collecting would cost your own trunk for the faxes this number sent you, or None
    when it sent none. ``predict`` defaults to ``routing.predict.predict``."""
    from . import predict as predictor
    now = now or datetime.utcnow()
    faxes, pages, seconds = received_from(engine, number, now=now)
    if not faxes or not pages:
        return None
    predict = predict or predictor.predict
    shape = predictor.Shape(max(1, pages), None, 'fine', 'normal')
    try:
        prediction = predict('sip', number, shape, now=now)
    except (ValueError, sa.exc.SQLAlchemyError):
        logging.getLogger(__name__).warning('The cost of collecting faxes could not be predicted.', exc_info=True)
        prediction = None
    minutes = max(1, round(seconds / 60))
    what = (f'This number sent you {faxes} fax{"es" if faxes != 1 else ""} ({pages} page'
            f'{"s" if pages != 1 else ""}, about {minutes} minute{"s" if minutes != 1 else ""} on the line) in the '
            f'last {ADVICE_DAYS} days, and the other site paid for those calls.')
    amount = predictor.amount_text(prediction) if prediction is not None else None
    if amount is None:
        return what + ' Faxbot cannot tell what your own phone line would pay to collect them.'
    if not prediction.cost.micros:
        # A flat plan, or calls your plan already includes: the case collecting is for.
        return what + " Collecting them would add nothing to your phone line's bill."
    return what + f' Collecting them would cost your own phone line about {amount}.'


# Collecting ---------------------------------------------------------------------------------------------------

async def collect(engine, values, ami, number, *, actor=None, actor_name=None, now=None, seal=None):
    """Ask Faxbot's SSL Fax engine to call ``number`` once and collect the fax its server holds for you. Returns
    the request ID. Raises ``PollRefused`` (polling not turned on for the number, or the engine cannot take the
    call; nothing dialed). A lost answer after submission leaves the collection uncertain; it is never asked
    for again by itself. With ``seal`` (a ``PollSeal``) the number's polling password goes with the call."""
    import asyncio
    from .. import hylafax_engine
    found = await asyncio.to_thread(source, engine, number)
    if found is None or not found.enabled:
        raise PollRefused(NOT_ON)
    choice = await hylafax_engine.choose(values, ami=ami)
    if choice.engine != 'hylafax':
        raise PollRefused(f"Faxbot collects faxes with its fast fax service, which cannot take the call now: "
                          f"{choice.reason}")
    password = ''
    if found.password_sealed and seal is not None:
        password = await asyncio.to_thread(seal.open, found.password_sealed, number) or ''
    request_id = uuid.uuid4().hex
    now = now or datetime.utcnow()
    try:
        job = await hylafax_engine.prepare_poll(values, ami, request_id=request_id, number=number,
                                                selective=found.selective or '', password=password)
    except (hylafax_engine.EngineError, ConnectionError, TimeoutError, OSError, ValueError):
        logging.getLogger(__name__).warning('The SSL Fax engine could not take a collection.', exc_info=True)
        await asyncio.to_thread(record_request, engine, request_id, number, selective=found.selective,
                                actor=actor, actor_name=actor_name, now=now)
        await asyncio.to_thread(record_result, engine, request_id, 'not_sent', NOT_SENT, now=now)
        raise PollRefused(NOT_SENT) from None
    try:
        # Written before the call: a collection Faxbot asked for is on record whatever happens next.
        await asyncio.to_thread(record_request, engine, request_id, number, selective=found.selective,
                                engine_job=job.engine_job, actor=actor, actor_name=actor_name, now=now)
    except BaseException:
        await asyncio.to_thread(job.discard)
        await hylafax_engine.forget_plan(ami, job.tag)
        raise
    try:
        emit = getattr(ami, '_emit', None)
        if emit is not None and job.submission:
            emit('Submission', job.submission)  # the trunk call's record starts now, as for a sent fax
        await asyncio.to_thread(job.submit)
    except Exception:
        # The engine may have the job: uncertain, never asked for again by itself.
        await asyncio.to_thread(record_result, engine, request_id, 'uncertain',
                                "Faxbot's fast fax service did not confirm the call; check Received before "
                                'collecting again.', now=now)
        raise
    finally:
        await asyncio.to_thread(job.close)
    return request_id


# Views --------------------------------------------------------------------------------------------------------

OUTCOME_TEXT = {'received': 'Collected', 'nothing_waiting': 'Nothing waiting', 'refused': 'Not allowed',
                'failed': 'Not collected', 'uncertain': 'Check Received', 'not_sent': 'Not dialed'}


def timetable_text(found) -> str | None:
    """One sentence for a number's collection timetable, or None when there is none."""
    if found is None or not found.collect_times or not found.collect_days:
        return None
    names = {'mon': 'Monday', 'tue': 'Tuesday', 'wed': 'Wednesday', 'thu': 'Thursday', 'fri': 'Friday',
             'sat': 'Saturday', 'sun': 'Sunday'}
    days = found.collect_days.split(',')
    if days == list(DAYS[:5]):
        when = 'on weekdays'
    elif days == list(DAYS):
        when = 'every day'
    else:
        when = 'on ' + ', '.join(names[day] + 's' for day in days)
    times = found.collect_times.split(',')
    clock = ' and '.join(times) if len(times) <= 2 else ', '.join(times[:-1]) + ' and ' + times[-1]
    zone = f' ({found.time_zone})' if found.time_zone else ''
    return f'Faxbot collects at {clock} {when}{zone}.'


def view(engine, number, *, now=None, predict=None):
    """Recipients details, "Collect faxes from this number", and ``faxbot recipients polling``."""
    from ..people_time import short
    found = source(engine, number)
    asked = requests(engine, number)
    return {
        'number': number,
        'enabled': bool(found and found.enabled),
        'label': found.label if found else None,
        'selective': found.selective if found else None,
        'has_password': bool(found and found.has_password),
        'collect_times': found.collect_times if found else None,
        'collect_days': found.collect_days if found else None,
        'time_zone': found.time_zone if found else None,
        'timetable': timetable_text(found),
        'advice': advice(engine, number, now=now, predict=predict),
        'note': ADVICE_NOTE,
        'requests': [{'id': row['id'], 'requested_at': row['requested_at'], 'requested': short(row['requested_at']),
                      'requested_by': row['requested_by_name'],
                      'state': OUTCOME_TEXT.get(row['outcome'], 'Calling'),
                      'sentence': row['sentence'] or WAITING, 'pages': row['pages'],
                      'inbound_fax_id': row['inbound_fax_id']} for row in asked],
    }


# Timed collection ---------------------------------------------------------------------------------------------

TIMETABLE_BY = 'the timetable'


def due(engine, now, *, recent_minutes=3) -> list:
    """The numbers whose timetable names this minute (in their own time zone, else UTC) and that may be collected
    now: collecting is on, no collection was asked for in the last ``recent_minutes`` (so one slot collects once),
    and the newest result is not uncertain (a person must check Received and collect by hand first)."""
    from zoneinfo import ZoneInfo
    table = _tables(engine)['poll_sources']
    with engine.connect() as connection:
        rows = connection.execute(sa.select(table).where(table.c.collect_times.is_not(None),
                                                         table.c.collect_days.is_not(None)).order_by(
            table.c.created_at.desc(), table.c.id.desc())).mappings().all()
    numbers = []
    for row in rows:
        number = row['number']
        if number in numbers:
            continue
        found = source(engine, number)
        if found is None or not found.enabled or not found.collect_times or not found.collect_days:
            continue
        zone = ZoneInfo(found.time_zone) if found.time_zone else timezone.utc
        local = now.replace(tzinfo=timezone.utc).astimezone(zone)
        if DAYS[local.weekday()] not in found.collect_days.split(','):
            continue
        if local.strftime('%H:%M') not in found.collect_times.split(','):
            continue
        asked = requests(engine, number, limit=1)
        if asked:
            if asked[0]['requested_at'] > now - timedelta(minutes=recent_minutes):
                continue
            if asked[0]['outcome'] == 'uncertain':
                continue
        numbers.append(number)
    return numbers


async def collect_due(engine, values, ami, *, now=None, seal=None) -> list:
    """Collect from every number whose timetable is due now; the request IDs. A number whose collection is refused
    (the engine cannot take the call) is skipped this time and logged."""
    now = now or datetime.utcnow()
    import asyncio
    started = []
    for number in await asyncio.to_thread(due, engine, now):
        try:
            started.append(await collect(engine, values, ami, number, actor_name=TIMETABLE_BY, now=now, seal=seal))
        except PollRefused as refused:
            logging.getLogger(__name__).info('Timed collection from %s skipped: %s', number, refused)
    return started


# Holding faxes for another site to collect ----------------------------------------------------------------------

COLLECTION_TEXT = {'sent': 'Collected', 'refused': 'Asked for and refused', 'failed': 'Collection failed',
                   'withdrawn': 'Withdrawn'}


def record_held(engine, hold_id, number, *, document, pages, selective=None, password_sealed=None, source_name=None,
                tsi=None, actor=None, actor_name=None, now=None):
    if not _HEX32.fullmatch(str(hold_id or '')):
        raise ValueError('Unsupported held fax')
    table = _tables(engine)['poll_held']
    with engine.begin() as connection:
        connection.execute(table.insert().values(
            id=hold_id, number=number, selective=selective or None, password_sealed=password_sealed,
            document=str(document)[:120], pages=int(pages or 0), source_name=(str(source_name)[:200] or None) if source_name else None,
            tsi=(str(tsi)[:32] or None) if tsi else None, held_by=str(actor)[:40] if actor else None,
            held_by_name=str(actor_name)[:200] if actor_name else None, held_at=now or datetime.utcnow()))


def held_fax(engine, hold_id):
    """One held fax's row (a dict), or None."""
    if not _HEX32.fullmatch(str(hold_id or '')):
        return None
    table = _tables(engine)['poll_held']
    with engine.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.id == hold_id)).mappings().first()
    return dict(row) if row is not None else None


def record_collection(engine, held_id, outcome, sentence, *, pages=None, caller=None, cig=None, sep=None,
                      seconds=None, engine_ref=None, now=None):
    """What happened to a held fax, once per engine report (``engine_ref``); a report posted again keeps the first
    row. Returns the outcome kept."""
    if outcome not in COLLECTION_OUTCOMES:
        raise ValueError('Unsupported collection outcome')
    table = _tables(engine)['poll_collections']
    with engine.begin() as connection:
        if engine_ref:
            kept = connection.execute(sa.select(table.c.outcome).where(table.c.engine_ref == engine_ref)).scalar()
            if kept is not None:
                return kept
        connection.execute(table.insert().values(
            id=uuid.uuid4().hex, held_id=held_id, outcome=outcome, sentence=str(sentence)[:300], pages=pages,
            caller=(str(caller)[:32] or None) if caller else None, cig=(str(cig)[:32] or None) if cig else None,
            sep=(str(sep)[:20] or None) if sep else None, seconds=seconds, engine_ref=engine_ref or None,
            created_at=now or datetime.utcnow()))
    return outcome


def held(engine, number, *, limit=20) -> list:
    """The faxes held for ``number``, newest first, each with its collections (newest first)."""
    tables = _tables(engine)
    docs, events = tables['poll_held'], tables['poll_collections']
    with engine.connect() as connection:
        rows = connection.execute(sa.select(docs).where(docs.c.number == number)
                                  .order_by(docs.c.held_at.desc(), docs.c.id.desc()).limit(limit)).mappings().all()
        ids = [row['id'] for row in rows]
        found = connection.execute(sa.select(events).where(events.c.held_id.in_(ids or ['']))
                                   .order_by(events.c.created_at.desc(), events.c.id.desc())).mappings().all() if ids else []
    by_doc = {}
    for event in found:
        by_doc.setdefault(event['held_id'], []).append(dict(event))
    return [{**dict(row), 'collections': by_doc.get(row['id'], [])} for row in rows]


def held_state(doc) -> tuple[str, str]:
    """(state, sentence) for one held fax from its collections: collected (gone), withdrawn (gone), or still held
    (with what the last attempt said)."""
    events = doc.get('collections') or []
    if events and events[0]['outcome'] in ('sent', 'withdrawn'):
        return COLLECTION_TEXT[events[0]['outcome']], events[0]['sentence']
    if events:
        return 'Still held', events[0]['sentence']
    return 'Held', HELD_WAITING


def hold_view(engine, number):
    """Recipients details, "Faxes this number collects from Faxbot", and ``faxbot recipients held``."""
    from ..people_time import short
    setting = hold_setting(engine, number)
    docs = held(engine, number)
    shown = []
    for doc in docs:
        state, sentence = held_state(doc)
        shown.append({'id': doc['id'], 'held_at': doc['held_at'], 'held': short(doc['held_at']),
                      'held_by': doc['held_by_name'], 'pages': doc['pages'], 'name': doc['source_name'],
                      'selective': doc['selective'], 'has_password': doc['password_sealed'] is not None,
                      'state': state, 'sentence': sentence,
                      'gone': bool(doc['collections']) and doc['collections'][0]['outcome'] in ('sent', 'withdrawn')})
    return {
        'number': number,
        'enabled': bool(setting and setting.enabled),
        'label': setting.label if setting else None,
        'selective': setting.selective if setting else None,
        'has_password': bool(setting and setting.has_password),
        'note': HOLD_NOTE,
        'held': shown,
    }


async def hold(engine, values, number, *, path, source_name=None, actor=None, actor_name=None, now=None, seal=None,
               tsi=None, header=None, ami=None):
    """Hold the document at ``path`` (a PDF or a fax TIFF) in Faxbot's SSL Fax engine for ``number`` to collect.
    Returns the hold ID. Raises ``PollRefused`` when holding is not turned on for the number or the engine cannot
    take it (nothing is held), ``ValueError`` for a document Faxbot cannot turn into fax pages."""
    import asyncio
    import tempfile
    from .. import hylafax_engine
    from ..conversion import DocumentConversionError, pdf_to_tiff, read_fax_frames, write_fax_tiff
    setting = await asyncio.to_thread(hold_setting, engine, number)
    if setting is None or not setting.enabled:
        raise PollRefused(HOLD_NOT_ON)
    choice = await hylafax_engine.choose(values, ami=ami)
    if choice.engine != 'hylafax':
        raise PollRefused(f"Faxbot holds faxes with its fast fax service, which cannot take it now: {choice.reason}")
    hold_id = uuid.uuid4().hex
    now = now or datetime.utcnow()
    password = ''
    if setting.password_sealed and seal is not None:
        password = await asyncio.to_thread(seal.open, setting.password_sealed, number) or ''
    with tempfile.TemporaryDirectory() as folder:
        source = str(path)
        frames = await asyncio.to_thread(read_fax_frames, source)
        if frames is None:
            # Not a fax image: a PDF, drawn as fax pages.
            source = str(Path(folder) / 'drawn.tiff')
            try:
                await asyncio.to_thread(pdf_to_tiff, str(path), source)
            except DocumentConversionError as error:
                raise ValueError(str(error)) from None
            frames = await asyncio.to_thread(read_fax_frames, source)
        if not frames:
            raise ValueError('The document has no pages Faxbot can hold as a fax.')
        # Written as the engine sends a page: one Group 4 strip a page (its sender reads the first strip only).
        tiff = str(Path(folder) / 'held.tiff')
        await asyncio.to_thread(write_fax_tiff, frames, tiff)
        bits = tuple(range(len(frames)))
        sidecar = hylafax_engine.held_sidecar(number=number, selective=setting.selective or '', password=password,
                                             job=hold_id, tsi=tsi or '', tagline=header or '',
                                             held_at=int(now.replace(tzinfo=timezone.utc).timestamp()))
        try:
            document = await asyncio.to_thread(hylafax_engine.hold_document, values, hold_id=hold_id, tiff_path=tiff,
                                               sidecar=sidecar)
        except (hylafax_engine.EngineError, ConnectionError, TimeoutError, OSError):
            logging.getLogger(__name__).warning('The SSL Fax engine could not hold a fax.', exc_info=True)
            raise PollRefused(NOT_HELD) from None
    await asyncio.to_thread(record_held, engine, hold_id, number, document=document, pages=len(bits),
                            selective=setting.selective, password_sealed=setting.password_sealed,
                            source_name=source_name, tsi=tsi, actor=actor, actor_name=actor_name, now=now)
    return hold_id


async def withdraw(engine, values, hold_id, *, actor_name=None, now=None):
    """Take a held fax back: it leaves the engine, and the record says who withdrew it. Returns the state kept
    ('withdrawn', or the outcome of a collection that came first)."""
    import asyncio
    from .. import hylafax_engine
    doc = await asyncio.to_thread(held_fax, engine, hold_id)
    if doc is None:
        raise LookupError('No such held fax')
    events = await asyncio.to_thread(_collections, engine, hold_id)
    if events and events[0]['outcome'] in ('sent', 'withdrawn'):
        return events[0]['outcome']
    try:
        await asyncio.to_thread(hylafax_engine.withdraw_held, values, hold_id=hold_id)
    except hylafax_engine.EngineError:
        raise PollRefused("Faxbot's fast fax service cannot be reached, so the fax is still held; try again.") from None
    who = f' by {actor_name}' if actor_name else ''
    await asyncio.to_thread(record_collection, engine, hold_id, 'withdrawn', f'Withdrawn{who}.', now=now)
    return 'withdrawn'


def _collections(engine, hold_id):
    table = _tables(engine)['poll_collections']
    with engine.connect() as connection:
        rows = connection.execute(sa.select(table).where(table.c.held_id == hold_id)
                                  .order_by(table.c.created_at.desc(), table.c.id.desc())).mappings().all()
    return [dict(row) for row in rows]


def record_polled(engine, payload, *, now=None) -> tuple[str, str] | None:
    """The engine's report on a call in which another machine collected, or tried to collect, a held fax: one
    collection row (once per report), and the (outcome, sentence) recorded; None when the report names no fax
    Faxbot holds."""
    from .. import hylafax_engine
    hold_id = payload.get('job') if isinstance(payload.get('job'), str) else ''
    doc = held_fax(engine, hold_id)
    if doc is None:
        return None
    outcome, sentence = hylafax_engine.polled_outcome(payload)
    engine_id = payload.get('engine_id') if isinstance(payload.get('engine_id'), str) else ''
    key = payload.get('key') if isinstance(payload.get('key'), str) else ''
    reference = f'{engine_id}:{key}'[:64] if engine_id and key else None
    pages = payload.get('pages') if isinstance(payload.get('pages'), int) and not isinstance(payload.get('pages'), bool) else None
    seconds = payload.get('seconds') if isinstance(payload.get('seconds'), int) and not isinstance(payload.get('seconds'), bool) else None
    kept = record_collection(engine, hold_id, outcome, sentence, pages=pages,
                             caller=payload.get('caller') if isinstance(payload.get('caller'), str) else None,
                             cig=payload.get('cig') if isinstance(payload.get('cig'), str) else None,
                             sep=payload.get('sep') if isinstance(payload.get('sep'), str) else None,
                             seconds=seconds, engine_ref=reference, now=now)
    return kept, sentence
