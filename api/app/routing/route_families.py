"""Route families: failures that belong to a route, not to the numbers it called (brief 92, RF).

Faxbot learns per number (``engine_learning.py``, ``schedule.py``). Some failures are not about the number at
all: a trunk whose settings just changed, a carrier's upstream, a fax service having a bad hour. Taught to each
number, they teach every number the wrong lesson. This module groups call outcomes by **route family**:

- the **account** the call went by (``fax_route_selections`` names it; else the cost ledger's route; else the
  first trunk, ``sip``);
- the **transport**: ``t38`` or ``audio`` for trunk calls, ``service`` for a fax service's own calls;
- the **phase** the call failed in (``PHASES``): before it connected, with no fax data coming back, with no fax
  machine heard, after the fax machine answered (T.30), or at the fax service;
- the **settings generation**: the trunk's learning epoch (``fax_learning_epochs``: its settings and the fax
  engines' builds), or the provider profile a fax service's attempt was bound to.

Busy, unanswered and a person answering are the destination's own behaviour and say nothing about the route.

**Incidents.** When failures in one family reach ``MIN_DESTINATIONS`` different numbers after a change point,
make up at least ``FAILURE_SHARE`` of the route's counted calls since then, and at least ``MIN_CORROBORATED`` of
those numbers went through before the change or by another family, Faxbot records an incident
(``route_family_incidents``). The change point is the start of the settings generation when the route worked
before it and nothing has gone through since (``settings``), else the first failure of the run (``outside``).

While an incident is open, and for its whole window after it closes, its failures are not lessons about the
numbers: ``engine_learning`` writes no memory for them and leaves them out of what it learns for each call
(``covered``), and the schedule's learned hours leave them out (``excluded_attempts``, for jo's hook). Lessons
written before Faxbot saw the incident are retired when it closes (``fax_destination_memory.forgotten_at``,
listed in ``route_family_closures``). An incident closes when ``CLOSE_SUCCESSES`` numbers go through on the
route again, when its settings change, after ``QUIET`` with no new failure, or when a person closes it.

Route choice (jo's ``plan.py``/``transport.py``) asks ``family_incident(db, account, transport)``. A trunk's
audio fax is outside its T.38 family, so a T.38 incident leaves the trunk usable by audio fax.

**The 2-by-2 test.** Two of your sending accounts against two of your own receiving numbers separate a route
problem from a number problem (``interpret``). Each of the four test faxes is sent only when a person sends it
(``route_families_http``); nothing here sends anything.

**Shared upstreams.** ``route_upstreams`` says which carrier a provider is known to use upstream, with its
published source and date. Unknown stays unknown, and nothing is ever inferred from a SIP header.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import re
import uuid
import weakref

import sqlalchemy as sa


log = logging.getLogger(__name__)

TRANSPORTS = ('t38', 'audio', 'service')
PHASES = ('connect', 'media', 'answer', 't30', 'service')
# Only recent failures open an incident; corroboration and change points look back further.
WINDOW = timedelta(days=2)
LOOKBACK = timedelta(days=30)
MIN_DESTINATIONS = 3
MIN_CORROBORATED = 2
FAILURE_SHARE = 0.6
CLOSE_SUCCESSES = 2
QUIET = timedelta(days=7)
RETIRED_BY = 'Faxbot, when the route problem ended'
TABLES = ('route_family_incidents', 'route_family_closures', 'route_family_tests', 'route_family_test_sends',
          'route_upstreams')
CELLS = ('a1', 'a2', 'b1', 'b2')

_NO_DATA = frozenset({'no_media_back', 'no_t38_data_back', 'no_fax_data_back'})
_NO_MACHINE = frozenset({'no_fax_answer', 'no_fax_signal'})

TRANSPORT_WORDS = {'t38': 'fax over IP (T.38)', 'audio': 'audio fax', 'service': 'faxes'}
PHASE_WORDS = {
    'connect': 'before the call connected',
    'media': 'with no fax data coming back',
    'answer': 'with no fax machine heard on the line',
    't30': 'after the fax machine answered',
    'service': 'at the fax service',
}
CLOSE_WORDS = {
    'went_through': 'faxes went through on it again',
    'settings_changed': 'its settings changed',
    'quiet': 'no new failure for a week',
    'person': 'you closed it',
}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# -- tables -------------------------------------------------------------------------------------------------

_REFLECTED = weakref.WeakKeyDictionary()


def _tables(db):
    """The tables this module reads, reflected once per database; None for one not created yet."""
    engine = getattr(db, 'engine', db)
    try:
        return _REFLECTED[engine]
    except (KeyError, TypeError):
        pass
    wanted = ('route_family_incidents', 'route_family_closures', 'route_family_tests', 'route_family_test_sends',
              'route_upstreams', 'sip_call_records', 'fax_route_selections', 'delivery_attempt_costs',
              'outbound_attempts', 'fax_learning_epochs', 'fax_destination_memory', 'fax_jobs',
              'outbound_deliveries', 'provider_profiles')
    present = set(sa.inspect(db).get_table_names())
    metadata = sa.MetaData()
    metadata.reflect(db, only=[name for name in wanted if name in present])
    found = {name: metadata.tables.get(name) for name in wanted}
    try:
        _REFLECTED[engine] = found
    except TypeError:
        pass
    return found


def available(db) -> bool:
    return _tables(db).get('route_family_incidents') is not None


# -- one outcome per call --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    """One finished call or fax-service attempt, as route families read it."""
    at: datetime
    account: str
    provider: str
    transport: str
    generation: str
    number: str
    failed: bool
    phase: str | None = None          # the failure's phase; None for a success
    record_id: str | None = None      # sip_call_records.id, for a trunk call
    attempt_id: str | None = None

    @property
    def stream(self):
        return (self.account, self.transport)

    @property
    def family(self):
        return (self.account, self.transport, self.phase, self.generation)


def trunk_phase(record) -> tuple[bool, str | None] | None:
    """(failed, phase) for one outbound trunk call record, or None when it says nothing about the route:
    still in progress, busy, unanswered, ambiguous, or a person answered."""
    from ..sip_calls import stored_verdict
    disposition = record.get('disposition')
    if disposition in ('busy', 'no_answer', 'ambiguous'):
        return None
    if disposition in ('congestion', 'failed'):
        return True, 'connect'
    if disposition != 'answered' or record.get('ended_at') is None:
        return None
    verdict = stored_verdict(record)
    if verdict == 'sent':
        return False, None
    if verdict in _NO_DATA:
        return True, 'media'
    if verdict in _NO_MACHINE:
        return True, 'answer'
    if verdict == 'remote_fax_failed':
        return True, 't30'
    return None


class Epochs:
    """The trunk's learning epochs, oldest first, to name the generation a call at a moment belongs to."""

    def __init__(self, rows):
        self.rows = sorted(((row['started_at'], row['id']) for row in rows), key=lambda item: item)

    @classmethod
    def read(cls, connection, table):
        if table is None:
            return cls([])
        return cls(connection.execute(sa.select(table.c.id, table.c.started_at)).mappings().all())

    def at(self, moment):
        """The epoch in force at ``moment``; the first epoch covers calls from before epochs were kept."""
        found = None
        for started, identity in self.rows:
            if started <= moment:
                found = identity
            else:
                break
        if found is None:
            return self.rows[0][1] if self.rows else 'before-learning'
        return found

    def starts(self):
        return {identity: started for started, identity in self.rows}

    def newest(self):
        return self.rows[-1][1] if self.rows else None


def _selections(connection, t, attempt_ids):
    found = {}
    ids = [item for item in attempt_ids if item]
    if not ids:
        return found
    selections, costs = t.get('fax_route_selections'), t.get('delivery_attempt_costs')
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        if costs is not None:
            for row in connection.execute(sa.select(costs.c.id, costs.c.route).where(costs.c.id.in_(chunk))):
                found[row.id] = row.route
        if selections is not None:
            for row in connection.execute(sa.select(selections.c.attempt_id, selections.c.account_key)
                                          .where(selections.c.attempt_id.in_(chunk))):
                found[row.attempt_id] = row.account_key
    return found


def read_outcomes(db, *, since, until=None, connection=None) -> tuple[list, Epochs]:
    """Every counted outcome from ``since``: outbound trunk calls and fax-service attempts, oldest first."""
    t = _tables(db)
    until = until or utcnow()
    records, costs, attempts = t.get('sip_call_records'), t.get('delivery_attempt_costs'), t.get('outbound_attempts')

    def read(connection):
        epochs = Epochs.read(connection, t.get('fax_learning_epochs'))
        found = []
        if records is not None:
            rows = [dict(row) for row in connection.execute(sa.select(records).where(
                records.c.direction == 'outbound', records.c.started_at >= since, records.c.started_at <= until)
                .order_by(records.c.started_at, records.c.id)).mappings()]
            accounts = _selections(connection, t, [row['attempt_id'] for row in rows])
            for row in rows:
                judged = trunk_phase(row)
                if judged is None or not row.get('called'):
                    continue
                failed, phase = judged
                found.append(Outcome(row['started_at'], accounts.get(row['attempt_id']) or 'sip', 'sip',
                                     't38' if row['t38'] == 'yes' else 'audio', epochs.at(row['started_at']),
                                     row['called'], failed, phase, row['id'], row['attempt_id']))
        if costs is not None:
            columns = [costs.c.id, costs.c.route, costs.c.provider_id, costs.c.destination, costs.c.outcome,
                       sa.func.coalesce(costs.c.ended_at, costs.c.started_at, costs.c.created_at).label('at')]
            source = costs
            if attempts is not None:
                columns += [attempts.c.profile_id, attempts.c.error_category]
                source = costs.outerjoin(attempts, attempts.c.id == costs.c.id)
            rows = connection.execute(sa.select(*columns).select_from(source).where(
                costs.c.provider_id != 'sip', costs.c.outcome.in_(('success', 'failed')),
                costs.c.created_at >= since, costs.c.created_at <= until)).mappings().all()
            for row in rows:
                if row.get('error_category') == 'person_answered':
                    continue
                failed = row['outcome'] == 'failed'
                found.append(Outcome(row['at'], row['route'] or row['provider_id'], row['provider_id'], 'service',
                                     row.get('profile_id') or row['provider_id'], row['destination'], failed,
                                     'service' if failed else None, None, row['id']))
        found.sort(key=lambda item: (item.at, item.record_id or item.attempt_id or ''))
        return found, epochs

    if connection is not None:
        return read(connection)
    with db.connect() as connection:
        return read(connection)


# -- finding incidents (pure) ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    account: str
    provider: str
    transport: str
    phase: str
    generation: str
    change_at: datetime
    change_cause: str
    first_failure_at: datetime
    last_failure_at: datetime
    destinations: int
    failures: int
    calls: int
    corroborated: int

    @property
    def family(self):
        return (self.account, self.transport, self.phase, self.generation)


def _digits(number):
    return re.sub(r'[^0-9]', '', str(number or ''))


def _same(a, b):
    return _digits(a)[-10:] == _digits(b)[-10:] and len(_digits(a)) >= 3


def _number_key(number):
    return _digits(number)[-10:]


def detect(outcomes, *, now, generation_starts=None) -> list:
    """Incidents the outcomes show (``Candidate``), one per family at most: the earliest change point that
    meets every condition. Pure: reads nothing, writes nothing."""
    generation_starts = generation_starts or {}
    by_stream = defaultdict(list)
    for item in outcomes:
        by_stream[item.stream].append(item)
    found = []
    for stream, items in by_stream.items():
        items.sort(key=lambda item: item.at)
        families = {item.family for item in items if item.failed and item.at >= now - WINDOW}
        for family in sorted(families, key=str):
            candidate = _family_candidate(family, items, outcomes, now, generation_starts)
            if candidate is not None:
                found.append(candidate)
    return found


def _family_candidate(family, stream_items, everything, now, generation_starts):
    account, transport, phase, generation = family
    in_generation = [item for item in stream_items if item.generation == generation]
    failures = [item for item in in_generation if item.family == family]
    if not failures:
        return None
    starts = []
    started = generation_starts.get(generation)
    earlier = [item for item in stream_items if item.generation != generation and started is not None
               and item.at < started]
    if (started is not None and any(not item.failed for item in earlier)
            and not any(not item.failed and item.at >= started for item in in_generation)):
        starts.append((started, 'settings'))
    starts += [(item.at, 'outside') for item in failures]
    for change_at, cause in starts:
        since = [item for item in in_generation if item.at >= change_at]
        failed_here = [item for item in since if item.family == family]
        numbers = {_number_key(item.number) for item in failed_here}
        if len(numbers) < MIN_DESTINATIONS or not failed_here or failed_here[-1].at < now - WINDOW:
            continue
        counted_failures = sum(1 for item in since if item.failed)
        if counted_failures / max(1, len(since)) < FAILURE_SHARE:
            continue
        corroborated = _corroborated(numbers, family, change_at, everything)
        if corroborated < MIN_CORROBORATED:
            continue
        return Candidate(account, failures[0].provider, transport, phase, generation, change_at, cause,
                         failed_here[0].at, failed_here[-1].at, len(numbers), len(failed_here), len(since),
                         corroborated)
    return None


def _corroborated(numbers, family, change_at, everything):
    """How many of the failed numbers went through before the change on this route, or by another family."""
    account, transport, _, _ = family
    good = set()
    for item in everything:
        if item.failed:
            continue
        key = _number_key(item.number)
        if key not in numbers:
            continue
        if (item.account, item.transport) != (account, transport) or item.at < change_at:
            good.add(key)
    return len(good)


def closing(incident, outcomes, *, now, newest_generation=None):
    """Why an open incident ends now ('went_through', 'settings_changed', 'quiet'), or None. Pure."""
    account, transport, generation = incident['account_key'], incident['transport'], incident['generation']
    if transport != 'service' and newest_generation and newest_generation != generation:
        return 'settings_changed'
    family = (account, transport, incident['phase'], generation)
    stream = [item for item in outcomes if item.stream == (account, transport)]
    last = max([item.at for item in stream if item.family == family] + [incident['last_failure_at']])
    through = {_number_key(item.number) for item in stream if not item.failed and item.at > last}
    if len(through) >= CLOSE_SUCCESSES:
        return 'went_through'
    if now - last >= QUIET:
        return 'quiet'
    return None


# -- keeping incidents ----------------------------------------------------------------------------------------

def _closures(connection, t):
    closures = t['route_family_closures']
    return {row['incident_id']: dict(row) for row in connection.execute(sa.select(closures)).mappings()}


def incidents(db, *, connection=None, include_closed=True, limit=200) -> list:
    """Incidents newest first, each with ``closed`` (its closure row, or None)."""
    if not available(db):
        return []
    t = _tables(db)
    table = t['route_family_incidents']

    def read(connection):
        rows = [dict(row) for row in connection.execute(sa.select(table).order_by(
            table.c.opened_at.desc(), table.c.id).limit(limit)).mappings()]
        ended = _closures(connection, t)
        for row in rows:
            row['closed'] = ended.get(row['id'])
        return [row for row in rows if include_closed or row['closed'] is None]
    if connection is not None:
        return read(connection)
    with db.connect() as connection:
        return read(connection)


def open_incidents(db, *, connection=None) -> list:
    return incidents(db, connection=connection, include_closed=False)


def family_incident(db, account, transport=None, *, connection=None):
    """The open incident on ``account`` (and ``transport`` when given), newest first; None when the route is clear.

    For route choice: an account with an open incident on every transport it would use is outside the
    approved routes to prefer. A trunk's audio fax is outside its T.38 family."""
    for row in open_incidents(db, connection=connection):
        if row['account_key'] == account and (transport is None or row['transport'] == transport):
            return row
    return None


def t38_problem(db, account='sip', *, connection=None):
    """The open T.38 incident on a trunk account, for the call's engine to use audio fax meanwhile; or None."""
    return family_incident(db, account, 't38', connection=connection)


def watch(db, *, now=None) -> dict:
    """Background: record new incidents and close finished ones; {'opened': n, 'closed': n}."""
    now = now or utcnow()
    if not available(db):
        return {'opened': 0, 'closed': 0}
    outcomes, epochs = read_outcomes(db, since=now - LOOKBACK, until=now)
    opened = 0
    current = open_incidents(db)
    busy = {(row['account_key'], row['transport'], row['phase'], row['generation']) for row in current}
    for candidate in detect(outcomes, now=now, generation_starts=epochs.starts()):
        if candidate.family in busy:
            continue
        if _open(db, candidate, now):
            opened += 1
            busy.add(candidate.family)
    closed = 0
    for row in current:
        reason = closing(row, outcomes, now=now, newest_generation=epochs.newest())
        if reason is not None and close(db, row['id'], reason=reason, now=now, outcomes=outcomes):
            closed += 1
    return {'opened': opened, 'closed': closed}


def _open(db, candidate, now):
    table = _tables(db)['route_family_incidents']
    try:
        with db.begin() as connection:
            if connection.execute(sa.select(table.c.id).where(
                    table.c.account_key == candidate.account, table.c.transport == candidate.transport,
                    table.c.phase == candidate.phase, table.c.generation == candidate.generation,
                    table.c.change_at == candidate.change_at)).first():
                return False
            connection.execute(table.insert().values(
                id=uuid.uuid4().hex, account_key=candidate.account[:64], provider_id=candidate.provider[:64],
                transport=candidate.transport, phase=candidate.phase, generation=candidate.generation[:64],
                change_at=candidate.change_at, change_cause=candidate.change_cause,
                first_failure_at=candidate.first_failure_at, last_failure_at=candidate.last_failure_at,
                destinations=candidate.destinations, failures=candidate.failures, calls=candidate.calls,
                corroborated=candidate.corroborated, opened_at=now))
    except sa.exc.IntegrityError:
        return False
    log.info('Faxbot recorded a route problem on one account; its failures are not counted against the numbers.')
    return True


def _family_records(incident, outcomes, until):
    family = (incident['account_key'], incident['transport'], incident['phase'], incident['generation'])
    return [item.record_id for item in outcomes if item.family == family and item.record_id
            and incident['change_at'] <= item.at <= until]


def close(db, incident_id, *, reason, now=None, actor_id=None, actor_name=None, outcomes=None) -> bool:
    """End an incident and retire the per-number lessons its failures taught; False when already closed."""
    now = now or utcnow()
    t = _tables(db)
    table, closures, memory = t['route_family_incidents'], t['route_family_closures'], t.get('fax_destination_memory')
    with db.connect() as connection:
        incident = connection.execute(sa.select(table).where(table.c.id == incident_id)).mappings().one_or_none()
    if incident is None:
        raise LookupError('No such route problem.')
    incident = dict(incident)
    if outcomes is None:
        outcomes, _ = read_outcomes(db, since=incident['change_at'] - timedelta(seconds=1), until=now)
    records = _family_records(incident, outcomes, now)
    try:
        with db.begin() as connection:
            if connection.execute(sa.select(closures.c.id).where(closures.c.incident_id == incident_id)).first():
                return False
            retired = []
            if memory is not None and records:
                for start in range(0, len(records), 500):
                    chunk = records[start:start + 500]
                    retired += list(connection.execute(sa.select(memory.c.id).where(
                        memory.c.evidence.in_(chunk), memory.c.direction == 'outbound',
                        memory.c.forgotten_at.is_(None))).scalars())
                for start in range(0, len(retired), 500):
                    connection.execute(memory.update().where(memory.c.id.in_(retired[start:start + 500]),
                                                             memory.c.forgotten_at.is_(None)).values(
                        forgotten_at=now, forgotten_by=None, forgotten_by_name=RETIRED_BY))
            connection.execute(closures.insert().values(
                id=uuid.uuid4().hex, incident_id=incident_id, closed_at=now, reason=reason,
                closed_by=(str(actor_id)[:40] if actor_id else None),
                closed_by_name=(str(actor_name)[:200] if actor_name else None),
                retired=len(retired), retired_ids=json.dumps(sorted(retired))))
    except sa.exc.IntegrityError:
        return False
    return True


# -- what learning leaves out -----------------------------------------------------------------------------------

def _windows(db, connection=None):
    """Every incident's family and window (change point to closing, or open), for leaving its failures out."""
    rows = incidents(db, connection=connection, limit=1000)
    return [((row['account_key'], row['transport'], row['phase'], row['generation']), row['change_at'],
             row['closed']['closed_at'] if row['closed'] else None) for row in rows]


def _inside(outcome, windows):
    if not outcome.failed:
        return False
    for family, start, end in windows:
        if outcome.family == family and outcome.at >= start and (end is None or outcome.at <= end):
            return True
    return False


def covered_records(db, record_ids, *, connection=None) -> set:
    """The trunk call records among ``record_ids`` whose failure belongs to a route family incident."""
    ids = [item for item in record_ids if item]
    if not ids or not available(db):
        return set()
    windows = _windows(db, connection)
    if not windows:
        return set()
    t = _tables(db)
    records = t['sip_call_records']

    def read(connection):
        epochs = Epochs.read(connection, t.get('fax_learning_epochs'))
        rows = []
        for start in range(0, len(ids), 500):
            rows += [dict(row) for row in connection.execute(sa.select(records).where(
                records.c.id.in_(ids[start:start + 500]), records.c.direction == 'outbound')).mappings()]
        accounts = _selections(connection, t, [row['attempt_id'] for row in rows])
        found = set()
        for row in rows:
            judged = trunk_phase(row)
            if judged is None or not judged[0]:
                continue
            outcome = Outcome(row['started_at'], accounts.get(row['attempt_id']) or 'sip', 'sip',
                              't38' if row['t38'] == 'yes' else 'audio', epochs.at(row['started_at']),
                              row['called'] or '', True, judged[1], row['id'], row['attempt_id'])
            if _inside(outcome, windows):
                found.add(row['id'])
        return found
    if connection is not None:
        return read(connection)
    with db.connect() as connection:
        return read(connection)


def covered(db, view) -> bool:
    """Whether one joined call view (``engine_learning.merge``) is a route family's failure, not the number's.

    Raise-free by design for learning's sake: a database that cannot be read here answers False (learning as
    before) and logs why. Only the database's own errors are caught."""
    if not view or view.get('direction') != 'outbound' or not view.get('record_id'):
        return False
    try:
        return view['record_id'] in covered_records(db, [view['record_id']])
    except sa.exc.SQLAlchemyError:
        log.warning('Faxbot could not read its route problems; this call is learned about as usual.')
        return False


def without_covered(db, views) -> list:
    """``views`` without the failed calls that belong to a route family incident (successes always stay)."""
    ids = [view['record_id'] for view in views if view.get('direction') == 'outbound' and view.get('record_id')]
    if not ids:
        return list(views)
    try:
        inside = covered_records(db, ids)
    except sa.exc.SQLAlchemyError:
        log.warning('Faxbot could not read its route problems; these calls are learned about as usual.')
        return list(views)
    return [view for view in views if view.get('record_id') not in inside]


def excluded_attempts(db, attempt_ids, *, connection=None) -> set:
    """The attempts among ``attempt_ids`` whose failure belongs to a route family incident.

    For the schedule's learned busy and call hours (``Scheduler.observations`` and ``call_timings``, jo's
    hook): leave these attempts out, as their failures were the route's."""
    ids = [item for item in attempt_ids if item]
    if not ids or not available(db):
        return set()
    t = _tables(db)
    records = t['sip_call_records']
    found = set()

    def read(connection):
        record_ids = {}
        if records is not None:
            for start in range(0, len(ids), 500):
                for row in connection.execute(sa.select(records.c.id, records.c.attempt_id).where(
                        records.c.attempt_id.in_(ids[start:start + 500]), records.c.direction == 'outbound')):
                    record_ids[row.id] = row.attempt_id
        inside = covered_records(db, list(record_ids), connection=connection)
        found.update(record_ids[item] for item in inside)
        windows = [window for window in _windows(db, connection) if window[0][1] == 'service']
        if windows:
            outcomes, _ = read_outcomes(db, since=min(start for _, start, _ in windows), connection=connection)
            wanted = set(ids)
            found.update(item.attempt_id for item in outcomes
                         if item.attempt_id in wanted and _inside(item, windows))
        return found
    if connection is not None:
        return read(connection)
    with db.connect() as connection:
        return read(connection)


# -- the 2-by-2 test --------------------------------------------------------------------------------------------

def interpret(cells, *, route_a='route A', route_b='route B', number_a='your first number',
              number_b='your second number') -> dict:
    """What four test faxes show: {'verdict', 'sentence'}. ``cells`` maps a1, a2, b1, b2 (route, number) to
    'success', 'failed', 'pending' or None (not sent). Pure."""
    state = {cell: cells.get(cell) for cell in CELLS}
    unsent = [cell for cell in CELLS if state[cell] is None]
    if unsent:
        left = 'the one test fax' if len(unsent) == 1 else f'the {len(unsent)} test faxes'
        return {'verdict': 'unsent', 'sentence': f'Faxbot never sends a test fax by itself: send {left} not sent yet '
                                                 'when you are ready.'}
    if any(value not in ('success', 'failed') for value in state.values()):
        return {'verdict': 'waiting', 'sentence': 'Waiting for the test faxes to finish.'}
    failed = {cell for cell, value in state.items() if value == 'failed'}
    if not failed:
        return {'verdict': 'all_through', 'sentence': 'All four test faxes went through: both routes and both of '
                'your numbers work, so the problem is more likely the recipient\'s line or the document.'}
    if failed == set(CELLS):
        return {'verdict': 'all_failed', 'sentence': 'All four test faxes failed. Something both routes share is '
                'the likely cause: this office\'s internet or power, the document, or a shared carrier upstream.'}
    if failed == {'a1', 'a2'}:
        return {'verdict': 'route_a', 'sentence': f'Both test faxes by {route_a} failed and both by {route_b} went '
                f'through, so the problem is on {route_a}, not on the numbers it calls.'}
    if failed == {'b1', 'b2'}:
        return {'verdict': 'route_b', 'sentence': f'Both test faxes by {route_b} failed and both by {route_a} went '
                f'through, so the problem is on {route_b}, not on the numbers it calls.'}
    if failed == {'a1', 'b1'}:
        return {'verdict': 'number_a', 'sentence': f'Both test faxes to {number_a} failed and both to {number_b} '
                f'went through, so the problem is on the receiving side of {number_a}, not on your routes.'}
    if failed == {'a2', 'b2'}:
        return {'verdict': 'number_b', 'sentence': f'Both test faxes to {number_b} failed and both to {number_a} '
                f'went through, so the problem is on the receiving side of {number_b}, not on your routes.'}
    if len(failed) == 1:
        cell = next(iter(failed))
        route = route_a if cell[0] == 'a' else route_b
        number = number_a if cell[1] == '1' else number_b
        return {'verdict': 'one_failed', 'sentence': f'Only the test fax by {route} to {number} failed. One failure '
                'cannot tell a route problem from a number problem; run a new test to see whether it repeats.'}
    return {'verdict': 'mixed', 'sentence': 'The failures do not line up with one route or one number. Run a new '
            'test; if the same mix repeats, the problem is likely intermittent, such as a busy network.'}


def create_test(db, *, route_a, route_b, number_a, number_b, incident_id=None, actor_id=None, actor_name=None,
                now=None) -> dict:
    """Record a 2-by-2 test plan. Nothing is sent: each cell waits for a person to send it."""
    if route_a == route_b:
        raise ValueError('Choose two different sending accounts.')
    if _same(number_a, number_b):
        raise ValueError('Choose two different numbers of your own.')
    row = {'id': uuid.uuid4().hex, 'route_a': route_a[:64], 'route_b': route_b[:64], 'number_a': number_a[:32],
           'number_b': number_b[:32], 'incident_id': incident_id, 'created_at': now or utcnow(),
           'created_by': (str(actor_id)[:40] if actor_id else None),
           'created_by_name': (str(actor_name)[:200] if actor_name else None)}
    with db.begin() as connection:
        connection.execute(_tables(db)['route_family_tests'].insert().values(**row))
    return row


def cell_target(test, cell):
    """(account, number) for one cell."""
    if cell not in CELLS:
        raise ValueError('Choose a1, a2, b1 or b2.')
    return (test['route_a'] if cell[0] == 'a' else test['route_b'],
            test['number_a'] if cell[1] == '1' else test['number_b'])


def record_send_on(connection, db, test_id, cell, job_id, *, actor_id=None, actor_name=None, now=None):
    """Keep which fax a person sent for one cell, once (in the fax's own acceptance transaction)."""
    connection.execute(_tables(db)['route_family_test_sends'].insert().values(
        id=uuid.uuid4().hex, test_id=test_id, cell=cell, job_id=job_id, sent_at=now or utcnow(),
        sent_by=(str(actor_id)[:40] if actor_id else None),
        sent_by_name=(str(actor_name)[:200] if actor_name else None)))


def get_test(db, test_id) -> dict | None:
    t = _tables(db)
    tests, sends = t['route_family_tests'], t['route_family_test_sends']
    with db.connect() as connection:
        row = connection.execute(sa.select(tests).where(tests.c.id == test_id)).mappings().one_or_none()
        if row is None:
            return None
        sent = {item['cell']: dict(item) for item in connection.execute(
            sa.select(sends).where(sends.c.test_id == test_id)).mappings()}
        states = _job_states(connection, t, [item['job_id'] for item in sent.values()])
    test = dict(row)
    test['cells'] = {cell: (dict(sent[cell], state=states.get(sent[cell]['job_id'], 'pending'))
                            if cell in sent else None) for cell in CELLS}
    return test


def tests(db, *, limit=20) -> list:
    table = _tables(db)['route_family_tests']
    with db.connect() as connection:
        ids = list(connection.execute(sa.select(table.c.id).order_by(table.c.created_at.desc()).limit(limit))
                   .scalars())
    return [get_test(db, identity) for identity in ids]


def _job_states(connection, t, job_ids):
    """'success', 'failed' or 'pending' per fax, from its delivery state."""
    deliveries, jobs = t.get('outbound_deliveries'), t.get('fax_jobs')
    found = {}
    ids = [item for item in job_ids if item]
    if not ids:
        return found
    if deliveries is not None:
        for row in connection.execute(sa.select(deliveries.c.id, deliveries.c.state).where(deliveries.c.id.in_(ids))):
            found[row.id] = row.state
    if jobs is not None:
        for row in connection.execute(sa.select(jobs.c.id, jobs.c.status).where(jobs.c.id.in_(ids))):
            found.setdefault(row.id, row.status)
    words = {}
    for job, state in found.items():
        text = str(state or '').lower()
        words[job] = ('success' if text in ('success', 'delivered', 'sent') else
                      'failed' if text in ('failed', 'cancelled') else 'pending')
    return words


def cell_states(test) -> dict:
    return {cell: (item['state'] if item else None) for cell, item in test['cells'].items()}


# -- shared upstreams ---------------------------------------------------------------------------------------------

def upstreams(db) -> dict:
    """The newest upstream row per provider: {provider: row}; a row whose ``upstream`` is None means unknown."""
    table = _tables(db).get('route_upstreams')
    if table is None:
        return {}
    with db.connect() as connection:
        rows = connection.execute(sa.select(table).order_by(table.c.created_at, table.c.id)).mappings().all()
    newest = {}
    for row in rows:
        newest[row['provider']] = dict(row)
    return newest


def set_upstream(db, provider, upstream, *, source_url=None, source_date=None, note=None, actor_id=None,
                 actor_name=None, now=None) -> dict:
    """Record what a provider uses upstream (a new row; the older stay as history). ``upstream`` None: unknown."""
    provider = (provider or '').strip().lower()[:64]
    if not provider:
        raise ValueError('Name the provider.')
    upstream = (upstream or '').strip()[:120] or None
    source_url = (source_url or '').strip()[:500] or None
    if upstream and not (source_url and re.match(r'https?://', source_url)):
        raise ValueError('Give the web address where the upstream carrier is published.')
    if source_date:
        try:
            datetime.strptime(source_date, '%Y-%m-%d')
        except ValueError:
            raise ValueError('Give the day you read the source as a date, such as 2026-10-10.') from None
    row = {'id': uuid.uuid4().hex, 'provider': provider, 'upstream': upstream, 'source_url': source_url,
           'source_date': source_date or None, 'note': (note or '').strip()[:300] or None,
           'created_at': now or utcnow(), 'created_by': (str(actor_id)[:40] if actor_id else None),
           'created_by_name': (str(actor_name)[:200] if actor_name else None)}
    with db.begin() as connection:
        connection.execute(_tables(db)['route_upstreams'].insert().values(**row))
    return row


def diversity(provider_a, provider_b, table, *, host_a=None, host_b=None) -> dict:
    """Whether two routes are really different paths: {'kind', 'upstream', 'row'}. ``kind`` is
    'same_provider', 'same_server', 'shared_upstream', 'different' or 'unknown'. Pure."""
    a, b = (provider_a or '').lower(), (provider_b or '').lower()
    if a and a == b and a != 'sip':
        return {'kind': 'same_provider', 'upstream': None, 'row': None}
    if host_a and host_b and host_a.lower() == host_b.lower():
        return {'kind': 'same_server', 'upstream': None, 'row': None}
    row_a, row_b = table.get(a), table.get(b)
    up_a = (row_a or {}).get('upstream')
    up_b = (row_b or {}).get('upstream')
    if up_a and up_b:
        if up_a.lower() == up_b.lower():
            return {'kind': 'shared_upstream', 'upstream': up_a, 'row': row_a}
        return {'kind': 'different', 'upstream': None, 'row': None}
    return {'kind': 'unknown', 'upstream': None, 'row': None}


# -- sentences ---------------------------------------------------------------------------------------------------

def _clock(moment):
    from ..people_time import short
    return short(moment) if moment else ''


def incident_sentence(row, label) -> str:
    """One sentence for an incident, open or closed."""
    what = TRANSPORT_WORDS.get(row['transport'], 'faxes')
    phase = PHASE_WORDS.get(row['phase'], '')
    numbers = f"{row['destinations']} different numbers"
    after = (' after its settings changed' if row['change_cause'] == 'settings' else '')
    went = (f"; {row['corroborated']} of them went through before or by another route" if row['corroborated'] else '')
    if row.get('closed'):
        closed = row['closed']
        why = CLOSE_WORDS.get(closed['reason'], 'it ended')
        retired = closed.get('retired') or 0
        lessons = (f' Faxbot set aside {retired} {"lesson" if retired == 1 else "lessons"} it had learned from '
                   'these failures.' if retired else '')
        return (f"From {_clock(row['change_at'])}, {what} by {label} failed {phase} for {numbers}{after}{went}. "
                f"It ended {_clock(closed['closed_at'])}: {why}.{lessons}")
    alternative = ('Faxbot uses audio fax on this trunk meanwhile' if row['transport'] == 't38'
                   else 'Faxbot prefers your other routes meanwhile')
    return (f"Since {_clock(row['change_at'])}, {what} by {label} has failed {phase} for {numbers}{after}{went}. "
            f"{alternative} and does not count these failures against the numbers.")


def test_advice(row, label) -> str:
    """The smallest check that tells a route problem from a number problem, for an open incident."""
    return (f'To tell whether {label} or the numbers are at fault, run a 2-by-2 test: one test fax by {label} '
            'and one by another of your routes, to each of two of your own receiving numbers.')
