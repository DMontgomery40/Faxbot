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
- It cannot be polled: HylaFAX+ never sets DIS bit 9 ("ready to transmit") on
  its own DIS and ends a call that brings DTC (faxd/Class1Recv.c++, E107), and
  ``pollq`` is never read by faxd. So Faxbot never holds a fax for another
  machine to collect; that needs an engine change (see the report).
- Faxbot's built-in engine (Asterisk's SendFAX and ReceiveFAX on spandsp) does
  neither: SendFAX always runs spandsp as the calling party and neither
  application has a polling option (docs.asterisk.org; res_fax_spandsp.c).

Rules:

- **Per-number opt-in** (``poll_sources``, append-only; the newest row counts).
  Faxbot never polls a number you have not turned on, and polling is never
  automatic for an outside party: each collection is asked for by a person
  (the console's "Collect now" or ``faxbot recipients collect``).
- **One call, one try.** A collection that ended uncertain (the call dropped
  while a document was arriving) is never collected again by itself.
- **Advice** (``advice``): from the faxes this number sent you in the last 30
  days, what your own trunk would pay to place those calls.

Records: ``poll_requests`` (written before the call) and ``poll_results``
(once), migration 0059; never rewritten.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import re
import uuid
import weakref

import sqlalchemy as sa


TABLES = ('poll_sources', 'poll_requests', 'poll_results')
OUTCOMES = ('received', 'nothing_waiting', 'refused', 'failed', 'uncertain', 'not_sent')
ADVICE_DAYS = 30
_HEX32 = re.compile(r'[a-f0-9]{32}', re.ASCII)
_TABLES = weakref.WeakKeyDictionary()

NOT_ON = 'Turn on collecting faxes from this number first.'
NOT_SENT = "Faxbot's fax engine could not take the call, so nothing was dialed."
WAITING = 'Faxbot is calling the other fax server to collect the fax it holds for you.'
ADVICE_NOTE = ('Collecting works only when the other fax server holds the fax for you to collect. Turn it on only '
               "for your organization's own sites, and only after the other site has set its fax server to hold "
               'faxes for you.')


class PollRefused(ValueError):
    """A documented refusal: polling is not turned on for this number, or the SSL Fax engine cannot take the call;
    the sentence says which. Nothing was dialed."""


class PollStoreError(RuntimeError):
    """The polling records cannot be read or written now (before migration 0059, or the database is down)."""


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


@dataclass(frozen=True)
class Source:
    """Your setting for collecting faxes from one number."""
    number: str
    enabled: bool
    label: str | None
    selective: str | None
    recorded_by_name: str | None
    created_at: datetime


def _selective(value):
    """The T.30 selective polling address (SEP): digits, * and #, at most 20; '' for none."""
    text = re.sub(r'\s', '', str(value or ''))
    if text and not re.fullmatch(r'[0-9*#]{1,20}', text):
        raise ValueError('Give the selective polling address as up to 20 digits.')
    return text


def source(engine, number) -> Source | None:
    """The newest setting for ``number``, or None (off)."""
    table = _tables(engine)['poll_sources']
    with engine.connect() as connection:
        row = connection.execute(sa.select(table).where(table.c.number == number).order_by(
            table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().first()
    if row is None:
        return None
    return Source(row['number'], bool(row['enabled']), row['label'], row['selective'], row['recorded_by_name'],
                  row['created_at'])


def save_source(engine, number, *, enabled, label=None, selective=None, actor=None, actor_name=None, now=None):
    """A new setting row for ``number`` (rows are never changed); returns the ``Source``."""
    label = (str(label).strip()[:100] or None) if label else None
    selective = _selective(selective) or None
    now = now or datetime.utcnow()
    table = _tables(engine)['poll_sources']
    with engine.begin() as connection:
        connection.execute(table.insert().values(
            id=uuid.uuid4().hex, number=number, enabled=1 if enabled else 0, label=label, selective=selective,
            recorded_by=str(actor)[:40] if actor else None, recorded_by_name=str(actor_name)[:200] if actor_name else None,
            created_at=now))
    return Source(number, bool(enabled), label, selective, actor_name, now)


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

async def collect(engine, values, ami, number, *, actor=None, actor_name=None, now=None):
    """Ask Faxbot's SSL Fax engine to call ``number`` once and collect the fax its server holds for you. Returns
    the request ID. Raises ``PollRefused`` (polling not turned on for the number, or the engine cannot take the
    call; nothing dialed). A lost answer after submission leaves the collection uncertain; it is never asked
    for again by itself."""
    import asyncio
    from .. import hylafax_engine
    found = await asyncio.to_thread(source, engine, number)
    if found is None or not found.enabled:
        raise PollRefused(NOT_ON)
    choice = await hylafax_engine.choose(values, ami=ami)
    if choice.engine != 'hylafax':
        raise PollRefused(f"Faxbot collects faxes with its fax engine, which cannot take the call now: "
                          f"{choice.reason}")
    request_id = uuid.uuid4().hex
    now = now or datetime.utcnow()
    try:
        job = await hylafax_engine.prepare_poll(values, ami, request_id=request_id, number=number,
                                                selective=found.selective or '')
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
                                "Faxbot's fax engine did not confirm the call; check Received before "
                                'collecting again.', now=now)
        raise
    finally:
        await asyncio.to_thread(job.close)
    return request_id


# Views --------------------------------------------------------------------------------------------------------

OUTCOME_TEXT = {'received': 'Collected', 'nothing_waiting': 'Nothing waiting', 'refused': 'Not allowed',
                'failed': 'Not collected', 'uncertain': 'Check Received', 'not_sent': 'Not dialed'}


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
        'advice': advice(engine, number, now=now, predict=predict),
        'note': ADVICE_NOTE,
        'requests': [{'id': row['id'], 'requested_at': row['requested_at'], 'requested': short(row['requested_at']),
                      'requested_by': row['requested_by_name'],
                      'state': OUTCOME_TEXT.get(row['outcome'], 'Calling'),
                      'sentence': row['sentence'] or WAITING, 'pages': row['pages'],
                      'inbound_fax_id': row['inbound_fax_id']} for row in asked],
    }
