"""The live outbound board (``GET /outbound/now``, ``faxbot status``): what is sending, what waits and why, what
needs a person, how today went, how full the lines are, and how a big send is progressing.

The board only reads, and a poll never takes the database's write lock. It is worked out in two parts:

- **One aggregate for every viewer** (``Board.aggregate``), at most once every ``AGGREGATE_SECONDS``: one row per
  fax that is waiting, sending, waiting for a person or finished today, with its section, why it waits and how it
  failed, read on one plain connection. Why a fax waits comes from the claim's own rules, in the order the claim
  applies them: sending-rule holds (``routing/holds.py``), a call shared with other faxes (``batching``), room on
  the number and on the account (``capacity.py``), the recipient's schedule (``routing/schedule.py``), then an
  earlier attempt that did not get through. It is worked out in one pass for the first ``REASONED`` waiting faxes in
  queue order; the faxes after them are counted in ``waiting.more``.
- **What each person may see** (``Board.scope``): the containers and faxes Sent lists for them
  (``FaxResources.outbound_read_scope_on``) and whether they may approve held faxes. Both are read in one access
  transaction and kept until the access policy changes (``access_state.policy_version``, read without a lock),
  another credential asks, or ``SCOPE_SECONDS`` pass. A grant on a container covers the faxes accepted under it
  later, so a new fax shows at the next aggregate.

The access layer checks visibility only under its write lock (``AccessStore.lock_on``), so a read-only check in a
deferred transaction is not possible without changing it; the per-person scope is read under that lock once and
then reused. Authentication itself still takes the lock once per request, as on every signed-in route.

A section that cannot be read is named in ``not_checked`` (its cause is logged) and the others still answer.
Every sentence is product text; times in the JSON are UTC with a ``Z``, and the console and CLI show them in local
time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import threading
import time
from types import SimpleNamespace

import sqlalchemy as sa


SECTIONS = ('sending', 'waiting', 'needs_you', 'today', 'lines', 'engine', 'groups')
# Every list holds at most this many faxes.
LIST_LIMIT = 20
# Why a fax waits is worked out for at most this many waiting faxes, in queue order.
REASONED = 200
# One aggregate serves every viewer for this long.
AGGREGATE_SECONDS = 2.0
# A person's visible faxes are read again after this long even when the access policy did not change.
SCOPE_SECONDS = 30.0
# A submitter with at least this many faxes accepted today gets a progress line.
GROUP_MIN = 5
# Faxes read one by one for a list: at most this many sending, needing a person or shared on their own, and the
# latest this many failed today (older ones are counted under "other"). Counts come from SQL (``Board._counts``).
ROWS_READ = 500
FAILED_READ = 500
FAX_SECTIONS = ('sending', 'waiting', 'needs_you', 'today', 'groups')

SENDING = ('submitting', 'in_progress')
QUEUED = ('ready', 'preparing')
ACTIVE = QUEUED + SENDING + ('reconciliation_required', 'held')

# Why a fax waits: (key, the sentence for the faxes waiting for it), in the order the claim decides.
REASONS = (
    ('lines_busy', 'Waiting for a free line.'),
    ('number_busy', 'Waiting: another fax is calling the same number.'),
    ('unknown_result', 'Waiting: an earlier fax to the same number has an unknown result.'),
    ('recipient_hours', "Waiting for the recipient's fax hours."),
    ('recipient_busy', "Waiting for a time the recipient's line is usually free."),
    ('sending_together', 'Held briefly to go in one call with other faxes to the same number.'),
    ('send_later', 'Scheduled to go later.'),
    ('retry', "Waiting to try again after an attempt that didn't get through."),
    ('preparing', 'Being prepared.'),
    ('next', 'Next to go.'),
)
REASON_SENTENCES = dict(REASONS)
# Reasons whose sentence for one fax comes from the claim's own words (``Capacity.waiting_sentence``).
OWN_SENTENCE = ('lines_busy', 'number_busy', 'unknown_result', 'recipient_hours', 'recipient_busy', 'send_later')
# How a fax that failed today ended: (key, label).
FAILURES = (
    ('busy', 'The line was busy'),
    ('no_answer', 'No one answered'),
    ('not_fax', 'A person or voicemail answered, not a fax machine'),
    ('no_speed', "The two fax machines couldn't agree a speed"),
    ('rejected', 'The number or the provider turned it down'),
    ('other', 'Another problem (see Sent)'),
)
HELD_TEST_FAX = 'This test fax is held and will not be sent.'
ENGINES = {'hylafax': 'SSL Fax engine', 'builtin': 'built-in engine'}
# The schedule's waits for a better hour (``schedule.Decision.why``).
LATER = ('faster', 'reliable', 'cheaper')

TABLES = ('outbound_deliveries', 'fax_jobs', 'outbound_attempts', 'delivery_attempt_costs', 'delivery_destinations',
          'access_resources', 'access_principals', 'access_state', 'outbound_events', 'outbound_holds',
          'dialing_guard_holds', 'outbound_batch_members', 'fax_engine_calls', 'sip_call_records',
          'fax_job_bindings', 'provider_profiles', 'certainty_items', 'access_assignments', 'access_key_grants')
REQUIRED = ('outbound_deliveries', 'fax_jobs', 'outbound_attempts', 'access_resources', 'access_principals',
            'access_state', 'outbound_events', 'fax_job_bindings', 'provider_profiles', 'delivery_destinations')


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def iso(moment):
    """Naive UTC as ISO 8601 with a Z; None stays None."""
    if moment is None:
        return None
    return moment.replace(microsecond=0).isoformat() + 'Z'


def local_midnight(now, zone_name):
    """The installation's last local midnight, as naive UTC."""
    from .people_time import zone
    local = now.replace(tzinfo=timezone.utc).astimezone(zone(zone_name))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class Fax:
    """One fax on the board, as the aggregate saw it."""
    id: str
    state: str
    resource: str | None
    parent: str | None
    enabled: bool
    to_number: str | None
    recipient: str | None
    pages: int | None
    created_at: datetime
    updated_at: datetime
    route_provider: str | None
    backend: str | None
    section: str = ''                # sending, waiting, needs_you; '' once finished
    position: int = 0                # queue order among waiting faxes
    reason: str | None = None        # why it waits; None beyond REASONED
    until: datetime | None = None
    route: str | None = None
    engine: str | None = None
    started_at: datetime | None = None
    kind: str | None = None          # what it needs a person for
    sentence: str | None = None
    since: datetime | None = None
    hold: dict | None = None         # {id, version, separate_approver, requested_by_principal_id}
    failure: str | None = None       # how it failed today
    via_key: bool = False
    row: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Count:
    """How many faxes share one place on the board: their container, whether a key sent them, where they are now
    (``Board._category``), how they ended (``Board._outcome``: delivered, failed or none yet), and whether they were
    accepted today and changed today."""
    parent: str | None
    enabled: bool
    via_key: bool
    category: str
    today: bool
    finished: bool
    count: int
    outcome: str = 'none'


class DatabaseBusy(RuntimeError):
    """The database was too busy to read any fax: ask again in a moment."""


@dataclass
class Aggregate:
    at: datetime
    since: datetime
    faxes: list
    not_checked: set
    counts: list = field(default_factory=list)
    lines: list | None = None
    engine: dict | None = None
    lines_sentence: str | None = None
    names: dict = field(default_factory=dict)
    values: object = None


@dataclass(frozen=True)
class Scope:
    """What one person may see on the board: Sent's own visibility, and whether they may approve held faxes."""
    containers: frozenset
    faxes: frozenset
    approver: bool
    principal: str | None
    version: int
    read_at: float

    def sees(self, fax):
        return bool(fax.resource and fax.enabled and (fax.parent in self.containers or fax.resource in self.faxes))


class Board:
    """The aggregate and the per-person scopes of one installation database."""

    def __init__(self, engine, *, clock=time.monotonic):
        self.engine = engine
        self.clock = clock
        self.t = None
        self._lock = threading.Lock()
        self._last = None
        self._last_read = None
        self._scopes = {}
        # The recipient's schedule decision for each waiting fax, kept as long as the claim keeps one (RECHECK), and
        # the claim's own sentence for each waiting fax shown, kept as long.
        self._decided, self._sentences = {}, {}
        self._scope_lock = threading.Lock()
        # How many aggregates and scopes were read, and rows the aggregates loaded (tests count them, not time).
        self.reads = 0
        self.scope_reads = 0
        self.rows_read = 0

    # Reading --------------------------------------------------------------------------------------------------

    def _tables(self, connection):
        if self.t is None:
            present = set(sa.inspect(connection).get_table_names())
            missing = [name for name in REQUIRED if name not in present]
            if missing:
                raise RuntimeError(f'The board needs tables this database does not have yet: {", ".join(missing)}.')
            metadata = sa.MetaData()
            metadata.reflect(connection, only=[name for name in TABLES if name in present])
            self.t = dict(metadata.tables)
        return self.t

    def aggregate(self, values, *, now=None):
        """The current aggregate: one read for every viewer within ``AGGREGATE_SECONDS``."""
        with self._lock:
            if self._last is not None and self.clock() - self._last_read < AGGREGATE_SECONDS:
                return self._last
            found = self._read(values, now or utcnow())
            self._last, self._last_read = found, self.clock()
            self.reads += 1
            return found

    def forget(self):
        """Read everything afresh at the next poll."""
        with self._lock:
            self._last, self._decided, self._sentences = None, {}, {}
        with self._scope_lock:
            self._scopes.clear()

    def _read(self, values, now):
        from .people_time import installation_zone_name
        zone_name = getattr(values, 'time_zone', None)
        since = local_midnight(now, installation_zone_name() if zone_name is None else zone_name)
        found = Aggregate(now, since, [], set(), values=values)
        busy = []
        with self.engine.connect() as connection:
            def section(names, read):
                try:
                    return read()
                except Exception as error:
                    from .database_busy import busy_cause
                    busy.append(busy_cause(error))
                    logging.getLogger(__name__).warning('The outbound board could not read %s.', ', '.join(names),
                                                        exc_info=True)
                    found.not_checked.update(names)
                    connection.rollback()
                    return None

            faxes = None
            counts = section(FAX_SECTIONS, lambda: self._counts(connection, since, now))
            if counts is not None:
                found.counts = counts
                faxes = section(FAX_SECTIONS, lambda: self._faxes(connection, since, now))
            if faxes is None and busy and all(busy):
                # Nothing about any fax could be read because the database was busy: the caller says so and asks
                # again (Retry-After), rather than showing an empty board.
                raise DatabaseBusy(busy[0])
            if faxes is not None:
                found.faxes = faxes
                section(('needs_you',), lambda: self._needs_you(connection, faxes, now))
                section(('sending',), lambda: self._sending(connection, values, faxes))
                section(('waiting',), lambda: self._waiting(connection, values, faxes, now, found))
                section(('today',), lambda: self._today(connection, faxes, since))
                section(('groups',), lambda: self._groups(connection, counts, found))
            section(('lines',), lambda: self._lines(connection, values, now, found))
        try:
            found.engine = self._engine(values)
        except Exception:
            logging.getLogger(__name__).warning('The outbound board could not read the fax engine.', exc_info=True)
            found.not_checked.add('engine')
        return found

    def _fetch(self, connection, query):
        """Every row of ``query`` as mappings, counted in ``rows_read`` (tests bound what reaches Python)."""
        rows = connection.execute(query).mappings().all()
        self.rows_read += len(rows)
        return rows

    def _uncertain(self, connection):
        """Faxes waiting for a person to settle an uncertain outcome, as SQL, exactly as Sent's settling list has them
        (``certainty_items``): the fax's latest item is open, whatever its delivery's state now; or its delivery is
        uncertain and the certainty worker has not filed an item yet. A fax whose latest item is settled ("can't
        tell" included) is done, though its delivery stays uncertain."""
        t = self._tables(connection)
        d, items = t['outbound_deliveries'], t.get('certainty_items')
        if items is None:
            return d.c.state == 'reconciliation_required'
        return sa.or_(self._open_item(connection).exists(), sa.and_(
            d.c.state == 'reconciliation_required', ~sa.select(items.c.id).where(items.c.job_id == d.c.id).exists()))

    def _open_item(self, connection):
        """The fax's latest certainty item while it is open (correlated to the delivery row)."""
        t = self._tables(connection)
        d, items = t['outbound_deliveries'], t['certainty_items']
        later = items.alias('later')
        return sa.select(items.c.category, items.c.created_at).where(
            items.c.job_id == d.c.id, items.c.state == 'open',
            ~sa.select(later.c.id).where(later.c.job_id == items.c.job_id,
                                         later.c.created_at > items.c.created_at).exists())

    def _category(self, connection, now):
        """Each delivery's place on the board, as SQL: sending, needs_you, waiting, delivered, cant_tell, failed or
        other.

        An uncertain outcome a person has not settled needs a person first (``_uncertain``). A ready fax with an
        open sending-rule hold the claim respects (``routing.holds.blocking``) needs a person too. A fax a person
        settled as can't tell (``outbound_store.CANT_TELL``) is failed for the API but never counted as failed today:
        it may have arrived (``cant_tell``, also its outcome).
        """
        t = self._tables(connection)
        d, holds = t['outbound_deliveries'], t.get('outbound_holds')
        state = d.c.state
        cant_tell = self._cant_tell(connection)
        held = sa.false()
        if holds is not None:
            held = sa.and_(state == 'ready', d.c.id.in_(sa.select(holds.c.job_id).where(
                holds.c.state == 'open', sa.or_(holds.c.kind != 'window', holds.c.release_at.is_(None),
                                                holds.c.release_at > now))))
        return sa.case((self._uncertain(connection), 'needs_you'), (state.in_(SENDING), 'sending'),
                       (state == 'held', 'needs_you'), (held, 'needs_you'), (state.in_(QUEUED), 'waiting'),
                       (state == 'success', 'delivered'), (cant_tell, 'cant_tell'), (state == 'failed', 'failed'),
                       else_='other')

    def _cant_tell(self, connection):
        """A delivery a person settled as can't tell (``outbound_store.CANT_TELL``), as SQL."""
        from .outbound_store import CANT_TELL
        t = self._tables(connection)
        d = t['outbound_deliveries']
        job = t['fax_jobs'].alias('cant_tell_job')  # its own alias: the board's lists join fax_jobs themselves
        return sa.and_(d.c.state == 'failed',
                       sa.select(job.c.error).where(job.c.id == d.c.id).scalar_subquery() == CANT_TELL)

    def _outcome(self, connection):
        """How the delivery ended, as SQL: delivered, cant_tell (settled by a person; it may have arrived), failed or
        none yet (today's counts go by this, wherever the fax sits on the board)."""
        state = self._tables(connection)['outbound_deliveries'].c.state
        return sa.case((state == 'success', 'delivered'), (self._cant_tell(connection), 'cant_tell'),
                       (state == 'failed', 'failed'), else_='none')

    def _window(self, connection, since):
        """Deliveries the board counts: active, accepted or changed today, or with an uncertain outcome to settle,
        however old."""
        t = self._tables(connection)
        d = t['outbound_deliveries']
        window = [d.c.state.in_(ACTIVE), d.c.created_at >= since, d.c.updated_at >= since]
        if t.get('certainty_items') is not None:
            window.append(self._open_item(connection).exists())
        return sa.or_(*window)

    def _counts(self, connection, since, now):
        """How many faxes there are in each place, by container, submitter, accepted today and finished today, counted
        in SQL: each viewer's counts are the sum over the containers they see (``view``)."""
        t = self._tables(connection)
        d, r = t['outbound_deliveries'], t['access_resources']
        category = self._category(connection, now).label('category')
        outcome = self._outcome(connection).label('outcome')
        via_key = sa.case((d.c.principal_scope.like('key:%'), 1), else_=0).label('via_key')
        today = sa.case((d.c.created_at >= since, 1), else_=0).label('today')
        finished = sa.case((d.c.updated_at >= since, 1), else_=0).label('finished')
        rows = self._fetch(connection, sa.select(
            r.c.parent_id.label('parent'), r.c.enabled, via_key, category, outcome, today, finished,
            sa.func.count().label('count'))
            .select_from(d.outerjoin(r, sa.and_(r.c.fax_job_id == d.c.id, r.c.kind == 'outbound')))
            .where(self._window(connection, since))
            .group_by(r.c.parent_id, r.c.enabled, via_key, category, outcome, today, finished))
        return [Count(row['parent'], row['enabled'] == 1, bool(row['via_key']), row['category'], bool(row['today']),
                      bool(row['finished']), int(row['count']), row['outcome']) for row in rows]

    def _faxes(self, connection, since, now):
        """The faxes the board lists, each read once: sending, needing a person, the first ``REASONED`` waiting in
        queue order, the latest ``FAILED_READ`` failed today, and any fax shared with someone on its own (a grant on
        that fax, so each viewer's counts can add it).

        Lists come from these rows only. A viewer who sees few of the faxes near the front of the queue gets correct
        counts (``_counts``) and a shorter list of waiting faxes; their faxes further back are counted in ``more``.
        """
        t = self._tables(connection)
        d, j, a = t['outbound_deliveries'], t['fax_jobs'], t['outbound_attempts']
        r, dest = t['access_resources'], t['delivery_destinations']
        binding, profile = t['fax_job_bindings'], t['provider_profiles']
        category = self._category(connection, now)
        columns = [d.c.id, d.c.state, d.c.dispatch_mode, d.c.version, d.c.attempt_id, d.c.created_at, d.c.updated_at,
                   d.c.principal_scope, j.c.to_number, j.c.pages, j.c.backend, j.c.error,
                   r.c.id.label('resource'), r.c.parent_id.label('parent'), r.c.enabled,
                   dest.c.display_name.label('recipient'), a.c.submitted_at, a.c.phase, a.c.error_category,
                   profile.c.provider_id.label('route_provider'), category.label('category'),
                   self._uncertain(connection).label('uncertain')]
        if t.get('certainty_items') is not None:
            # The open item's own reason and time, as Sent's settling list shows them.
            item = self._open_item(connection).order_by(None)
            columns += [item.with_only_columns(t['certainty_items'].c.category).limit(1).scalar_subquery()
                        .label('certainty_category'),
                        item.with_only_columns(t['certainty_items'].c.created_at).limit(1).scalar_subquery()
                        .label('certainty_since')]
        urgent = j.c.urgent if 'urgent' in j.c else sa.literal(0)
        columns.append(urgent.label('urgent'))
        source = (d.join(j, j.c.id == d.c.id)
                  .outerjoin(r, sa.and_(r.c.fax_job_id == d.c.id, r.c.kind == 'outbound'))
                  .outerjoin(dest, dest.c.phone_number == j.c.to_number)
                  .outerjoin(a, a.c.id == d.c.attempt_id)
                  .outerjoin(binding, binding.c.id == d.c.id)
                  .outerjoin(profile, profile.c.id == sa.func.coalesce(a.c.profile_id, binding.c.profile_id)))
        base = sa.select(*columns).select_from(source)
        reads = [
            base.where(category == 'sending').order_by(d.c.updated_at, d.c.id).limit(ROWS_READ),
            base.where(category == 'needs_you').order_by(d.c.updated_at, d.c.id).limit(ROWS_READ),
            # Queue order: urgent faxes first, then oldest first (``capacity.Capacity.next_ready`` decides the rest).
            base.where(category == 'waiting').order_by(sa.func.coalesce(urgent, 0).desc(), d.c.created_at, d.c.id)
            .limit(REASONED),
            base.where(d.c.state == 'failed', d.c.updated_at >= since).order_by(d.c.updated_at.desc(), d.c.id)
            .limit(FAILED_READ),
        ]
        grants, ceilings = t.get('access_assignments'), t.get('access_key_grants')
        if grants is not None and ceilings is not None:
            shared = sa.union(sa.select(grants.c.resource_id), sa.select(ceilings.c.resource_id))
            reads.append(base.where(r.c.id.in_(shared), self._window(connection, since)).limit(ROWS_READ))
        faxes = {}
        for query in reads:
            for row in self._fetch(connection, query):
                if row['id'] not in faxes:
                    faxes[row['id']] = self._fax(row)
        # The name typed when a fax was sent goes before its number's saved Recipients name (outbound_store).
        from .outbound_store import typed_recipient_names_on
        for job_id, name in typed_recipient_names_on(connection, faxes).items():
            faxes[job_id].recipient = name
        waiting = sorted((fax for fax in faxes.values() if fax.section == 'waiting'),
                         key=lambda fax: (-int(fax.row.get('urgent') or 0), fax.created_at, fax.id))
        for position, fax in enumerate(waiting):
            fax.position = position
        return list(faxes.values())

    @staticmethod
    def _fax(row):
        fax = Fax(row['id'], row['state'], row['resource'], row['parent'], row['enabled'] == 1, row['to_number'],
                  row['recipient'], row['pages'], row['created_at'], row['updated_at'], row['route_provider'],
                  row['backend'], section=row['category'],
                  via_key=str(row['principal_scope'] or '').startswith('key:'), row=dict(row))
        if row['uncertain']:
            from .access.queries import delivery_fields
            from .work.certainty_service import category_text
            fax.kind, fax.since = 'uncertain', row.get('certainty_since') or fax.updated_at
            if row.get('certainty_category'):
                fax.sentence = category_text(row['certainty_category'])
            else:
                fax.sentence = delivery_fields({**fax.row, 'delivery_state': 'reconciliation_required',
                                                'delivery_version': row['version']})['reconciliation_reason']
        elif fax.state == 'held':
            fax.kind, fax.since, fax.sentence = 'other', fax.updated_at, HELD_TEST_FAX
        return fax

    def _needs_you(self, connection, faxes, now):
        """The sending-rule hold of each ready fax that needs a person (``routing/holds.blocking``): one hold each."""
        from .rules.text import WAITS
        t = self._tables(connection)
        holds, guard = t.get('outbound_holds'), t.get('dialing_guard_holds')
        held = {fax.id: fax for fax in faxes if fax.section == 'needs_you' and fax.state == 'ready' and not fax.kind}
        for fax in held.values():
            fax.kind, fax.since, fax.sentence = 'other', fax.updated_at, WAITS
        if holds is None or not held:
            return True
        columns = [holds.c.id, holds.c.job_id, holds.c.kind, holds.c.version, holds.c.reason, holds.c.release_at,
                   holds.c.requested_at, holds.c.separate_approver, holds.c.requested_by_principal_id]
        source = holds
        if guard is not None:
            columns.append(guard.c.id.label('guard'))
            source = holds.outerjoin(guard, guard.c.id == holds.c.id)
        rows = self._fetch(connection, sa.select(*columns).select_from(source).where(
            holds.c.job_id.in_(sorted(held)), holds.c.state == 'open',
            sa.or_(holds.c.kind != 'window', holds.c.release_at.is_(None), holds.c.release_at > now))
            .order_by(holds.c.requested_at, holds.c.id))
        first = {'no_route': 0, 'approval': 1, 'window': 2}
        for row in sorted(rows, key=lambda row: first.get(row['kind'], 3)):
            fax = held[row['job_id']]
            if fax.hold is not None:
                continue
            fax.kind = 'dialing' if row.get('guard') else row['kind'] if row['kind'] in first else 'other'
            fax.since, fax.sentence = row['requested_at'], row['reason'] or WAITS
            fax.until = row['release_at'] if row['kind'] == 'window' else None
            fax.hold = {'id': row['id'], 'version': row['version'], 'separate_approver': row['separate_approver'],
                        'requested_by_principal_id': row['requested_by_principal_id']}
        return True

    def _sending(self, connection, values, faxes):
        """The route, engine and start of each fax being sent."""
        from .hylafax_records import handled_by
        from .outbound_store import requested_engine_on
        t = self._tables(connection)
        sending = [fax for fax in faxes if fax.section == 'sending']
        attempts = [fax.row['attempt_id'] for fax in sending if fax.row.get('attempt_id')]
        calls, records, costs = t.get('fax_engine_calls'), t.get('sip_call_records'), t.get('delivery_attempt_costs')
        engines, started, routes = {}, {}, {}
        if attempts and costs is not None:
            routes = dict(connection.execute(sa.select(costs.c.id, costs.c.route).where(
                costs.c.id.in_(attempts))).all())
        if attempts and calls is not None:
            for row in connection.execute(sa.select(calls.c.call_key, calls.c.engine, calls.c.engine_ref).where(
                    calls.c.direction == 'outbound', calls.c.call_key.in_(attempts))).mappings():
                engines[row['call_key']] = handled_by({'engine': row['engine'], 'engine_ref': row['engine_ref'],
                                                       'reason': None})[0]
        if attempts and records is not None:
            started = dict(connection.execute(sa.select(records.c.attempt_id, sa.func.min(records.c.started_at))
                                              .where(records.c.direction == 'outbound',
                                                     records.c.attempt_id.in_(attempts))
                                              .group_by(records.c.attempt_id)).all())
        for fax in sending:
            attempt = fax.row.get('attempt_id')
            # The account its attempt was given, else the one it was accepted with, as Sent names it.
            route = routes.get(attempt) or fax.backend
            fax.route = route_label(values, route)
            if 'sip' in (fax.route_provider, route):
                engine = engines.get(attempt) or requested_engine_on(connection, fax.id)
                fax.engine = ENGINES.get(engine)
            fax.started_at = started.get(attempt) or fax.row.get('submitted_at')
        return True

    def _waiting(self, connection, values, faxes, now, found):
        """Why each of the first ``REASONED`` waiting faxes waits, in one pass on ``connection``."""
        from . import capacity as rooms
        t = self._tables(connection)
        waiting = sorted((fax for fax in faxes if fax.section == 'waiting'), key=lambda fax: fax.position)
        first = waiting[:REASONED]
        ready = [fax for fax in first if fax.state == 'ready']
        for fax in first:
            if fax.state == 'preparing':
                fax.reason, fax.since = 'preparing', fax.created_at
            else:
                fax.since = fax.created_at
        if not ready:
            return True
        ids = [fax.id for fax in ready]
        # Faxes held to go in one call with others (batching.store.waiting_ids).
        members = t.get('outbound_batch_members')
        together = set()
        if members is not None:
            from .batching.store import waiting_ids
            together = set(connection.execute(waiting_ids({'outbound_batch_members': members})
                                              .where(members.c.id.in_(ids))).scalars())
        capacity = rooms.for_engine(self.engine, connection)
        # Room on the number: calls holding it now (Capacity.holds), against its own limit.
        held = {}
        for row in connection.execute(capacity.holds(now)).mappings():
            held.setdefault(row['number'], []).append(row)
        numbers = sorted({fax.to_number for fax in ready if fax.to_number in held})
        dest = t['delivery_destinations']
        limits = dict(connection.execute(sa.select(dest.c.phone_number, dest.c.max_calls).where(
            dest.c.phone_number.in_(numbers))).all()) if numbers else {}
        # Room on the account: every full account once, then which faxes wait for one.
        limited = rooms.limited_accounts(values)
        account_rooms = capacity.rooms(connection, values, now, limited=limited)
        full = {key: room for key, room in account_rooms.items() if room.full}
        for_room = capacity.waiting_for_room(connection, values, now, ids, limited=limited,
                                             rooms=account_rooms) if full else {}
        events = t['outbound_events']
        retried = set(connection.execute(sa.select(events.c.job_id).where(
            events.c.kind == 'route_fallback', events.c.job_id.in_(ids))).scalars())
        memo, decided = {}, {}
        from .routing.schedule import RECHECK
        for fax in ready:
            blocking = held.get(fax.to_number) or []
            limit = limits.get(fax.to_number)
            limit = rooms.DEFAULT_CALLS_TO_A_NUMBER if limit is None else limit
            if fax.id in together:
                fax.reason = 'sending_together'
            elif blocking and limit != 0 and len({row['call'] for row in blocking}) >= limit:
                unknown = [row['submitted_at'] for row in blocking
                           if row['state'] == 'reconciliation_required' and row['submitted_at'] is not None]
                if unknown and len(unknown) == len(blocking):
                    fax.reason, fax.until = 'unknown_result', min(unknown) + rooms.HOLD
                else:
                    fax.reason = 'number_busy'
            elif fax.id in for_room:
                fax.reason = 'lines_busy'
            else:
                kept = self._decided.get(fax.id)
                if kept is not None and self.clock() - kept[1] < RECHECK.total_seconds() and (
                        kept[0] is None or kept[0].hold_until is None or kept[0].hold_until > now):
                    decision = kept[0]
                else:
                    decision, _ = capacity.decision(connection, values, [fax.id], now, memo)
                    kept = (decision, self.clock())
                decided[fax.id] = kept
                if decision is not None and decision.hold_until is not None:
                    fax.reason = {'hours': 'recipient_hours', 'busy': 'recipient_busy'}.get(
                        decision.why, 'send_later' if decision.why in LATER else 'next')
                    fax.until = decision.hold_until
                elif fax.id in retried:
                    fax.reason = 'retry'
                else:
                    fax.reason = 'next'
        self._decided = decided
        several = sum(item.trunk for item in limited.values()) > 1
        waited = {for_room[fax.id].key for fax in ready if fax.id in for_room}
        if len(waited) == 1:
            found.lines_sentence = rooms.room_sentence(full[waited.pop()], several=several)
        return True

    def _today(self, connection, faxes, since):
        """How each fax that failed today ended (``failure_key``)."""
        t = self._tables(connection)
        failed = [fax for fax in faxes if fax.state == 'failed' and fax.updated_at >= since]
        attempts = [fax.row['attempt_id'] for fax in failed if fax.row.get('attempt_id')]
        records = t.get('sip_call_records')
        calls = {}
        if attempts and records is not None:
            for row in connection.execute(sa.select(records.c.attempt_id, records.c.disposition, records.c.pages,
                                                    records.c.error_cause).where(
                    records.c.direction == 'outbound', records.c.attempt_id.in_(attempts))
                    .order_by(records.c.started_at)).mappings():
                calls[row['attempt_id']] = dict(row)
        for fax in failed:
            call = calls.get(fax.row.get('attempt_id')) or {}
            fax.failure = failure_key(sentence=fax.row.get('error'), category=fax.row.get('error_category'),
                                      disposition=call.get('disposition'), pages=call.get('pages'),
                                      cause=call.get('error_cause'))
        return True

    def _groups(self, connection, counts, found):
        """The names of the submitters (their personal containers) of the faxes accepted today."""
        t = self._tables(connection)
        parents = sorted({count.parent for count in counts if count.parent and count.today})
        if not parents:
            return True
        r, p = t['access_resources'], t['access_principals']
        from .access.read import INSTALLATION_NAME, LEGACY_NAME
        for identity, kind, name, principal_kind in connection.execute(
                sa.select(r.c.id, r.c.kind, p.c.display_name, p.c.kind).select_from(
                    r.outerjoin(p, p.c.id == r.c.principal_id)).where(r.c.id.in_(parents))).all():
            if kind == 'legacy':
                found.names[identity] = LEGACY_NAME
            elif principal_kind == 'bootstrap':
                found.names[identity] = 'Installation key'
            elif kind == 'installation':
                found.names[identity] = INSTALLATION_NAME
            else:
                found.names[identity] = name or 'Someone no longer listed'
        return True

    def _lines(self, connection, values, now, found):
        """Calls in use on each trunk and each account with a limit at once (``capacity.rooms``)."""
        from . import capacity as rooms, sip_trunk
        capacity = rooms.for_engine(self.engine, connection)
        limited = rooms.limited_accounts(values)
        # Each trunk's carrier, for its published limit at once (capacity.CARRIERS).
        presets = {trunk.key: getattr(trunk.values, 'sip_trunk_preset', '') or ''
                   for trunk in sip_trunk.extra_trunks(values)}
        presets[rooms.TRUNK] = getattr(values, 'sip_trunk_preset', '') or ''
        lines = []
        for key, room in capacity.rooms(connection, values, now, limited=limited).items():
            label = room.label or key
            lines.append({'key': key, 'label': label[:1].upper() + label[1:],
                          'in_use': room.trunk_calls, 'limit': room.trunk_limit,
                          'incoming': room.incoming if room.trunk else 0,
                          'sentence': line_sentence(room, presets.get(room.key, ''))})
        found.lines = lines
        return True

    def _engine(self, values):
        """Audio fax for new trunk calls, while Faxbot chose it by itself (``sip_fax_mode.reason_for``)."""
        from . import sip_fax_mode, sip_trunk
        from .provider_labels import trunk_name
        if not sip_trunk.configured(values):
            return None
        found = sip_fax_mode.reason_for(values)
        if not found:
            return None
        said = sip_fax_mode.off_sentence(found['reason'], carrier=trunk_name(getattr(values, 'sip_trunk_preset', '')))
        if not said:
            return None
        said = said.removeprefix('Off: ')
        at = found.get('at')
        fix = ENGINE_FIXES.get(found['reason'])
        record = sip_fax_mode.read(values) or {}
        if found['reason'] == sip_fax_mode.NO_DATA_BACK and record.get('network') == 'blocked':
            # The network check turns T.38 on again by itself only after it saw this network blocked
            # (sip_fax_mode.network_decision).
            fix = ENGINE_FIXES['no_data_back_blocked']
        return {'audio': True, 'since': (at if not at or at.endswith('Z') else at + 'Z'),
                'sentence': said[:1].upper() + said[1:], 'fix': fix}

    def sentence(self, fax, found):
        """The claim's own sentence for one waiting fax shown (``Capacity.waiting_sentence``): asked once for its
        reason and wait, and again after ``schedule.RECHECK``, so a watched board does not ask the predictor every
        poll."""
        from . import capacity
        from .routing.schedule import RECHECK
        kept = self._sentences.get(fax.id)
        if (kept is not None and kept[:2] == (fax.reason, fax.until)
                and self.clock() - kept[3] < RECHECK.total_seconds()):
            return kept[2]
        said = capacity.for_engine(self.engine).waiting_sentence(fax.id, found.values, found.at)
        with self._scope_lock:
            if len(self._sentences) > 4 * LIST_LIMIT * 16:
                self._sentences.clear()
            self._sentences[fax.id] = (fax.reason, fax.until, said, self.clock())
        return said

    # What one person may see ------------------------------------------------------------------------------------

    def _policy_version(self):
        state = sa.table('access_state', sa.column('id'), sa.column('policy_version'))
        with self.engine.connect() as connection:
            return connection.scalar(sa.select(state.c.policy_version).where(state.c.id == 'state'))

    def scope(self, access, actor, *, now=None):
        """This person's ``Scope``, read under the access lock only when the policy changed or it is old."""
        credential = getattr(actor, 'credential', None)
        key = (getattr(actor, 'principal_id', None), getattr(actor, 'principal_security_version', None),
               getattr(actor, 'replay_scope', None), getattr(credential, 'binding_id', None),
               getattr(credential, 'session_id', None))
        version = self._policy_version()
        with self._scope_lock:
            cached = self._scopes.get(key)
        if cached is not None and cached.version == version and self.clock() - cached.read_at < SCOPE_SECONDS:
            return cached
        from .access.types import ResourceRef
        with access.store.transaction() as connection:
            moment = now or utcnow()
            containers, faxes = access.fax_resources.outbound_read_scope_on(connection, actor, now=moment)
            approver = access.control.authorize_on(connection, actor, 'fax:approve', ResourceRef('installation'),
                                                   now=moment).allowed
            version = access.store.require_lock_on(connection)
        found = Scope(containers, faxes, bool(approver), getattr(actor, 'principal_id', None), version, self.clock())
        self.scope_reads += 1
        with self._scope_lock:
            if len(self._scopes) > 256:
                self._scopes.clear()
            self._scopes[key] = found
        return found


ENGINE_FIXES = {
    'network': ('Faxbot switches back by itself once your network lets fax over IP (T.38) data come back. Carrier '
                'trunk shows the network check.'),
    'no_data_back': ('Once your network lets fax over IP (T.38) data come back, open Carrier trunk and choose '
                     '"Try T.38 again".'),
    'no_data_back_blocked': ('Faxbot switches back by itself once its network check finds the network fixed. You can '
                             'also open Carrier trunk and choose "Try T.38 again" when you have fixed it.'),
    'carrier': 'Nothing to do: your carrier carries fax as audio inside its own network.',
    'encrypted': 'Faxbot switches back by itself when calls over this trunk no longer need encryption.',
}


def route_label(values, key):
    """The name Sent gives the route a fax went by: this Faxbot, direct delivery, or the account's own name."""
    from .accounts import account_named
    from .provider_labels import provider_label
    if key == 'local':
        return 'This Faxbot'
    if key == 'direct':
        return 'Direct delivery'
    account = account_named(values, key) if key else None
    return getattr(account, 'label', None) or provider_label(key)


def line_sentence(room, preset=''):
    """'2 of 2 calls in use (1 incoming). Your carrier allows 2 at once.' for a trunk (``preset`` its carrier), or
    the faxes in progress on an account with a limit at once."""
    in_use, limit = room.trunk_calls, room.trunk_limit
    if not room.trunk:
        if limit is None:
            return f"{in_use} {'fax' if in_use == 1 else 'faxes'} in progress."
        return f"{in_use} of {limit} {'fax' if limit == 1 else 'faxes'} in progress, this account's limit at once."
    incoming = f' ({room.incoming} incoming)' if room.incoming else ''
    if limit is None:
        return f"{in_use} {'call' if in_use == 1 else 'calls'} in use{incoming}."
    sentence = f"{in_use} of {limit} {'call' if limit == 1 else 'calls'} in use{incoming}."
    from .capacity import CARRIERS
    carrier = CARRIERS.get(preset or '')
    if carrier is not None and carrier.calls_at_once == limit:
        return f'{sentence} Your carrier allows {limit} at once.'
    return f'{sentence} Faxbot places at most {limit} at once.'


def _carrier_balance():
    from .routing import telnyx_balance
    try:
        return telnyx_balance.status()
    except Exception:
        return None


def failure_key(*, sentence=None, category=None, disposition=None, pages=None, cause=None):
    """How a fax failed, as one of ``FAILURES``' keys.

    Extends the busy-hour classifier (``routing.schedule.classify``: busy, no answer, not a fax machine) with the
    engines' and providers' own words for two more endings, by exact sentence where an adapter has one: the fax
    machines could not agree a speed (the SSL Fax engine's training failures, and a trunk call whose engine said
    so), and a number or provider that turned the fax down. Anything else is ``other``.
    """
    from . import hylafax_engine, sip_calls
    from .routing.schedule import classify
    training_pattern, training = hylafax_engine._REASONS[2]
    if sentence == training or (cause and training_pattern.search(str(cause))):
        return 'no_speed'
    if category == sip_calls.PERSON_ANSWERED or sentence == sip_calls.PERSON:
        return 'not_fax'
    if sentence in _turned_down():
        return 'rejected'
    kind = classify(phase='failed', disposition=disposition, pages=pages, sentence=sentence)
    return {'busy': 'busy', 'no_answer': 'no_answer', 'no_fax': 'not_fax'}.get(kind, 'other')


_TURNED_DOWN = None


def _turned_down():
    """The adapters' own sentences for a number or a provider that refused the fax."""
    global _TURNED_DOWN
    if _TURNED_DOWN is None:
        from . import sinch_service, sip_calls
        from .humblefax_service import _FAILURES
        humble = {words[0]: sentence for words, sentence in _FAILURES}
        # The trunk carrier's own refusal, or a number it could not reach as dialled (sip_calls.carrier_sentence).
        _TURNED_DOWN = frozenset({*sinch_service.REFUSED.values(), sinch_service.CALL_ERRORS[34],
                                  humble['invalid'], sip_calls.CLEARED,
                                  *sip_calls.carrier_sentences(sip_calls.CARRIER_REFUSED, sip_calls.CARRIER_NOT_FOUND)})
    return _TURNED_DOWN


# One view per person ----------------------------------------------------------------------------------------------

def _counted(pairs, order):
    """[{key, count, ...}] largest first, then in ``order``."""
    index = {key: place for place, key in enumerate(order)}
    return sorted(pairs, key=lambda item: (-item['count'], index[item['key']]))


def visible_counts(found, scope):
    """This person's ``Count`` rows: the containers they see, plus each fax shared with them on its own (read one by
    one in the aggregate, as no container of theirs counts it)."""
    mine = [count for count in found.counts if count.enabled and count.parent in scope.containers]
    for fax in found.faxes:
        if fax.resource in scope.faxes and fax.enabled and fax.parent not in scope.containers:
            outcome = {'success': 'delivered', 'failed': 'failed'}.get(fax.state, 'none')
            mine.append(Count(fax.parent, True, fax.via_key, fax.section, fax.created_at >= found.since,
                              fax.updated_at >= found.since, 1, outcome))
    return mine


def view(found, scope, *, mask, sentence_for=None):
    """The board for one person: only the faxes ``scope`` sees, counted and listed (contract 106).

    Counts come from SQL (``visible_counts``); lists from the faxes the aggregate read one by one, so a list may be
    shorter than its count. Every sentence is filled: the claim's own words for a fax, else its reason's sentence.
    """
    from .routing.holds import HoldStore
    from .rules.text import WAITS
    mine = [fax for fax in found.faxes if scope.sees(fax)]
    counts = visible_counts(found, scope)
    not_checked = set(found.not_checked)

    def total(category=None, *, outcome=None, today=None, finished=None):
        return sum(count.count for count in counts if (category is None or count.category == category)
                   and (outcome is None or count.outcome == outcome)
                   and (today is None or count.today == today) and (finished is None or count.finished == finished))

    def base(fax):
        return {'id': fax.id, 'to': mask(fax.to_number), 'recipient': fax.recipient or None,
                'pages': fax.pages}

    sending = [fax for fax in mine if fax.section == 'sending']
    sending.sort(key=lambda fax: (fax.started_at is None, fax.started_at or fax.created_at, fax.id))
    # Only faxes whose reason was worked out are listed (all of them, unless that part could not be read).
    waiting = sorted((fax for fax in mine if fax.section == 'waiting' and fax.reason),
                     key=lambda fax: fax.position)
    needs = sorted((fax for fax in mine if fax.section == 'needs_you'), key=lambda fax: (fax.since or fax.created_at,
                                                                                        fax.id))
    reasons = {key: 0 for key, _ in REASONS}
    for fax in waiting:
        reasons[fax.reason] += 1
    lines_sentence = found.lines_sentence or REASON_SENTENCES['lines_busy']

    def reason_sentence(key):
        return lines_sentence if key == 'lines_busy' else REASON_SENTENCES[key]

    def waiting_item(fax):
        own = sentence_for(fax) if fax.reason in OWN_SENTENCE and sentence_for is not None else None
        return {**base(fax), 'reason': fax.reason, 'sentence': own or reason_sentence(fax.reason),
                'since': iso(fax.since or fax.created_at), 'until': iso(fax.until)}

    def actions(fax):
        if fax.kind == 'uncertain':
            return ['settle']
        if fax.hold is None:
            return []
        row = {'separate_approver': fax.hold['separate_approver'],
               'requested_by_principal_id': fax.hold['requested_by_principal_id']}
        decide = HoldStore.may_decide(row, SimpleNamespace(principal_id=scope.principal), approver=scope.approver)
        if fax.kind == 'approval':
            return ['approve', 'refuse'] if decide else []
        if fax.kind == 'no_route':
            if decide:
                return ['send_by_account', 'check_again', 'refuse']
            mine_to_check = scope.principal and fax.hold['requested_by_principal_id'] == scope.principal
            return ['check_again'] if mine_to_check else []
        return ['refuse'] if decide else []

    def need_item(fax):
        return {**base(fax), 'kind': fax.kind or 'other', 'since': iso(fax.since or fax.created_at),
                'sentence': fax.sentence or WAITS,
                'hold': {'id': fax.hold['id'], 'version': fax.hold['version']} if fax.hold else None,
                'actions': actions(fax), 'until': iso(fax.until)}

    # How today's failures ended: the latest FAILED_READ were read one by one; any older ones are "other".
    failed_today = total(outcome='failed', finished=True)
    failures = {key: 0 for key, _ in FAILURES}
    for fax in mine:
        if fax.state == 'failed' and fax.row.get('category') != 'cant_tell' and fax.updated_at >= found.since:
            failures[fax.failure or 'other'] += 1
    failures['other'] += max(failed_today - sum(failures.values()), 0)
    labels = dict(FAILURES)

    groups = {}
    for count in counts:
        if count.today and count.parent:
            groups.setdefault((count.parent, count.via_key), []).append(count)
    group_items = []
    for (parent, via_key), members in groups.items():
        size = sum(count.count for count in members)
        if size < GROUP_MIN:
            continue
        name = found.names.get(parent) or 'Someone no longer listed'

        def part(category):
            return sum(count.count for count in members if count.category == category)

        def ended(outcome):
            return sum(count.count for count in members if count.outcome == outcome)
        group_items.append({
            'who': f'{name} (API key)' if via_key and name != 'Installation key' else name, 'total': size,
            'delivered': ended('delivered'), 'sending': part('sending'), 'waiting': part('waiting'),
            'needs_you': part('needs_you'), 'failed': ended('failed')})
    group_items.sort(key=lambda item: (-item['total'], item['who']))

    waiting_count = total('waiting')
    return {
        'as_of': iso(found.at),
        'not_checked': [name for name in SECTIONS if name in not_checked],
        'sending': {'count': total('sending'), 'items': [
            {**base(fax), 'page': None, 'route': fax.route, 'engine': fax.engine, 'started_at': iso(fax.started_at)}
            for fax in sending[:LIST_LIMIT]]},
        'waiting': {'count': waiting_count, 'more': max(waiting_count - len(waiting), 0),
                    'reasons': _counted([{'key': key, 'count': reasons[key], 'sentence': reason_sentence(key)}
                                         for key, _ in REASONS], [key for key, _ in REASONS]),
                    'items': [waiting_item(fax) for fax in waiting[:LIST_LIMIT]]},
        'needs_you': {'count': total('needs_you'), 'items': [need_item(fax) for fax in needs[:LIST_LIMIT]]},
        'today': {'since': iso(found.since), 'accepted': total(today=True),
                  'delivered': total(outcome='delivered', finished=True), 'failed': failed_today,
                  'cant_tell': total(outcome='cant_tell', finished=True),
                  'failures': _counted([{'key': key, 'label': labels[key], 'count': count}
                                        for key, count in failures.items() if count],
                                       [key for key, _ in FAILURES])},
        'lines': found.lines or [],
        'engine': found.engine,
        'groups': group_items,
        # Your Telnyx balance when it is low or Telnyx has turned the account off (telnyx_balance.py); else None.
        'carrier_balance': _carrier_balance(),
    }


# One board per database ---------------------------------------------------------------------------------------------

_BOARDS = {}
_BOARDS_LOCK = threading.Lock()


def for_engine(engine):
    """One ``Board`` per database engine, shared by every viewer."""
    with _BOARDS_LOCK:
        found = _BOARDS.get(id(engine))
        if found is None or found.engine is not engine:
            if len(_BOARDS) > 16:
                _BOARDS.clear()
            found = _BOARDS[id(engine)] = Board(engine)
        return found


def board_for(engine, access, actor, values, *, mask, now=None):
    """The board ``actor`` sees now: their scope (cached), the shared aggregate, and the claim's own sentence for
    each waiting fax shown (``Board.sentence``)."""
    board = for_engine(engine)
    scope = board.scope(access, actor, now=now)
    found = board.aggregate(values, now=now)
    return view(found, scope, mask=mask, sentence_for=lambda fax: board.sentence(fax, found))
