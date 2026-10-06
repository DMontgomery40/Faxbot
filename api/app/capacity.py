"""Room for a fax call: calls at once to a number, calls at once on the trunk, and new calls a second.

The delivery worker claims a fax only while the number it calls, and the trunk
it would call over, have room. Room already taken is worked out from durable
delivery records, so there is no separate lease to lose or double after a
restart. A delivery holds its number (and the trunk, when it goes over the
trunk) from its claim until its outcome is definite:

- ``preparing`` holds while its claim is current (an expired claim placed no call and goes back to the queue);
- ``submitting`` and ``in_progress`` hold;
- ``reconciliation_required`` (an uncertain outcome) keeps holding until it is
  reconciled, because the call may still be on the line;
- every hold ends ``HOLD`` after the attempt was submitted, so a call whose
  result never arrives cannot block a number or a line forever.

A fax delivered inside Faxbot or straight to a partner places no call and holds
nothing. Faxes sent together in one call hold one place. Calls coming in over
the trunk use the same lines, so they count against the trunk too.

The limit per number applies to every route that places a phone call, cloud
providers included: the recipient's fax line answers one call at a time
whoever dials it, and a busy line costs a retry everywhere. Only the trunk and
its new-calls-a-second limit are about Faxbot's own lines.

A fax that cannot start waits; it never fails because of capacity. Urgent faxes
go first; otherwise the sender served least recently goes next, oldest fax
first, so one sender's batch cannot hold up everyone else.
"""
from dataclasses import dataclass
from datetime import timedelta

import sqlalchemy as sa


# Any hold ends this long after its call was submitted: longer than a long fax call
# (a 20-page fax at 9,600 bit/s takes about 15 minutes), short enough that a lost
# result frees the number within the half hour.
HOLD = timedelta(minutes=30)
# A fax line answers one call at a time.
DEFAULT_CALLS_TO_A_NUMBER = 1
NO_CALL_ROUTES = ('local', 'direct')
HOLDING = ('preparing', 'submitting', 'in_progress', 'reconciliation_required')
TRUNK = 'sip'
# Least recently served sender: their attempts over this window are compared.
SERVED_WINDOW = timedelta(days=1)


@dataclass(frozen=True)
class CarrierLimits:
    """A carrier's published call limits, with where and when Faxbot read them."""
    calls_per_second: int | None
    calls_at_once: int | None
    note: str
    sources: tuple[str, ...]
    read_on: str


CARRIERS = {
    # Telnyx rejects more than 20 new calls a second per source IP address or SIP user name
    # (503 "CPS Limit Reached"), and charges a monthly surcharge above a peak of 5 a second per
    # account, so Faxbot starts at most 5 a second. Accounts allow 2 calls at once at the first
    # verification level and 10 after Level 2, across the whole account. No date is shown on
    # either page.
    'telnyx': CarrierLimits(
        calls_per_second=5, calls_at_once=2,
        note=('Telnyx allows 20 new calls a second but charges extra above 5 a second, and allows 2 calls at once '
              'until the account is verified to Level 2 (then 10).'),
        sources=('https://support.telnyx.com/en/articles/7834487-calls-per-second-cps-surcharge',
                 'https://developers.telnyx.com/docs/voice/sip-trunking/configuration/concurrent-limits'),
        read_on='2026-10-06'),
}


def trunk_in_use(values):
    """Whether this installation has a SIP trunk whose lines Faxbot manages."""
    from . import sip_trunk
    return bool(getattr(values, 'effective_outbound', '') == TRUNK or sip_trunk.configured(values))


def trunk_calls_at_once(values):
    """Calls at once on the trunk: the setting, or else the fax engine's lines (SIP_FAX_LINES).

    The built-in engine uses the same number, so switching engines never changes capacity.
    """
    chosen = getattr(values, 'sip_trunk_max_calls', 0) or 0
    if chosen > 0:
        return chosen
    from .hylafax_engine import line_count
    return line_count(values)


def calls_per_second(values):
    """New calls a second on the trunk: the setting, or the carrier's published limit, or None (no limit)."""
    chosen = getattr(values, 'sip_trunk_calls_per_second', 0) or 0
    if chosen > 0:
        return chosen
    limits = CARRIERS.get(getattr(values, 'sip_trunk_preset', '') or '')
    return limits.calls_per_second if limits is not None else None


def _own_numbers(values):
    try:
        from .routing.local import own_numbers
        if not getattr(values, 'local_delivery_enabled', True):
            return set()
        return own_numbers(values)
    except Exception:
        return set()


@dataclass(frozen=True)
class Room:
    """What is full right now; ``trunk_full`` and ``rate_full`` apply only to faxes going over the trunk."""
    trunk_calls: int
    trunk_limit: int | None
    recent_starts: int
    rate_limit: int | None

    @property
    def trunk_full(self):
        return self.trunk_limit is not None and self.trunk_calls >= self.trunk_limit

    @property
    def rate_full(self):
        return self.rate_limit is not None and self.recent_starts >= self.rate_limit


class Capacity:
    TABLES = ('outbound_deliveries', 'outbound_attempts', 'fax_jobs', 'delivery_destinations',
              'delivery_attempt_costs', 'outbound_batch_members', 'access_resources', 'sip_call_records')

    def __init__(self, engine, connection=None):
        """Inside an open transaction pass its ``connection``: reflection then reads through it.

        Reflecting on a second connection while the claim holds the SQLite write
        lock left that connection blocking the next writer ("database is locked").
        """
        metadata = sa.MetaData()
        metadata.reflect(connection if connection is not None else engine, only=list(self.TABLES))
        self.engine = engine
        self.t = {name: metadata.tables[name] for name in self.TABLES}

    # Holds ------------------------------------------------------------------
    def holds(self, now, *, exclude=()):
        """One row per delivery holding room: its number, call (a shared call once), route and state."""
        d, a, j = (self.t[name].alias() for name in ('outbound_deliveries', 'outbound_attempts', 'fax_jobs'))
        c, m = self.t['delivery_attempt_costs'].alias(), self.t['outbound_batch_members'].alias()
        query = (sa.select(d.c.id.label('delivery_id'), j.c.to_number.label('number'),
                           sa.func.coalesce(m.c.batch_id, d.c.id).label('call'), d.c.state,
                           a.c.submitted_at, sa.func.coalesce(c.c.route, j.c.backend).label('route'))
                 .select_from(d.join(j, j.c.id == d.c.id).join(a, a.c.id == d.c.attempt_id)
                              .outerjoin(c, c.c.id == d.c.attempt_id)
                              .outerjoin(m, sa.and_(m.c.id == d.c.id, m.c.batch_id.is_not(None))))
                 .where(d.c.state.in_(HOLDING),
                        sa.or_(sa.and_(d.c.state == 'preparing', d.c.claim_expires_at > now),
                               sa.and_(d.c.state != 'preparing', a.c.submitted_at >= now - HOLD)),
                        sa.func.coalesce(c.c.route, '').not_in(NO_CALL_ROUTES)))
        if exclude:
            query = query.where(d.c.id.not_in(list(exclude)))
        return query

    def room(self, connection, values, now, *, exclude=()):
        """The trunk's calls in progress and new calls in the last second, against their limits."""
        if not trunk_in_use(values):
            return Room(0, None, 0, None)
        holds = self.holds(now, exclude=exclude).subquery()
        outgoing = connection.scalar(sa.select(sa.func.count(sa.distinct(holds.c.call))).where(holds.c.route == TRUNK))
        records = self.t['sip_call_records']
        incoming = connection.scalar(sa.select(sa.func.count()).select_from(records).where(
            records.c.direction == 'inbound', records.c.ended_at.is_(None), records.c.started_at >= now - HOLD))
        a, c, j = self.t['outbound_attempts'], self.t['delivery_attempt_costs'], self.t['fax_jobs']
        starts = connection.scalar(sa.select(sa.func.count()).select_from(
            a.join(j, j.c.id == a.c.job_id).outerjoin(c, c.c.id == a.c.id)).where(
            a.c.submitted_at > now - timedelta(seconds=1), a.c.submitted_at <= now,
            sa.func.coalesce(c.c.route, j.c.backend) == TRUNK))
        return Room(int(outgoing or 0) + int(incoming or 0), trunk_calls_at_once(values), int(starts or 0),
                    calls_per_second(values))

    def _busy(self, now):
        holds = self.holds(now).subquery()
        return (sa.select(holds.c.number, sa.func.count(sa.distinct(holds.c.call)).label('calls'))
                .group_by(holds.c.number).subquery())

    def _over_trunk(self, jobs, values):
        """Faxes the claim gate treats as going over the trunk: bound to it, and not delivered inside Faxbot."""
        own = sorted(_own_numbers(values))
        bound = jobs.c.backend == TRUNK
        if not own:
            return bound
        return sa.and_(bound, sa.or_(jobs.c.to_number.not_in(own), sa.func.coalesce(jobs.c.send_by_call, 0) == 1))

    # Admission --------------------------------------------------------------
    def next_ready(self, connection, values, now, *, waiting=None, exclude=()):
        """The delivery to claim next, or None: urgent first, then the sender served least recently, oldest first.

        Only faxes whose number has room are offered, and while the trunk is full
        (or has started its calls for this second) no fax going over it is.
        """
        d, j, dest = self.t['outbound_deliveries'], self.t['fax_jobs'], self.t['delivery_destinations']
        busy = self._busy(now)
        limit = sa.func.coalesce(dest.c.max_calls, DEFAULT_CALLS_TO_A_NUMBER)
        room = self.room(connection, values, now)
        resources, attempts, jobs2 = self.t['access_resources'], self.t['outbound_attempts'].alias(), j.alias()
        sender_of = (sa.select(resources.c.fax_job_id, resources.c.parent_id)
                     .where(resources.c.kind == 'outbound').subquery())
        sender_of2 = (sa.select(resources.c.fax_job_id, resources.c.parent_id)
                      .where(resources.c.kind == 'outbound').subquery())
        served = (sa.select(sa.func.coalesce(sender_of2.c.parent_id, '').label('sender'),
                            sa.func.max(attempts.c.created_at).label('last'))
                  .select_from(attempts.join(jobs2, jobs2.c.id == attempts.c.job_id)
                               .outerjoin(sender_of2, sender_of2.c.fax_job_id == attempts.c.job_id))
                  .where(attempts.c.created_at >= now - SERVED_WINDOW)
                  .group_by(sa.func.coalesce(sender_of2.c.parent_id, '')).subquery())
        query = (sa.select(d)
                 .select_from(d.join(j, j.c.id == d.c.id)
                              .outerjoin(dest, dest.c.phone_number == j.c.to_number)
                              .outerjoin(busy, busy.c.number == j.c.to_number)
                              .outerjoin(sender_of, sender_of.c.fax_job_id == d.c.id)
                              .outerjoin(served, served.c.sender == sa.func.coalesce(sender_of.c.parent_id, '')))
                 .where(d.c.state == 'ready', d.c.dispatch_mode == 'normal',
                        sa.or_(limit == 0, sa.func.coalesce(busy.c.calls, 0) < limit)))
        if waiting is not None:
            query = query.where(d.c.id.not_in(waiting))
        if exclude:
            query = query.where(d.c.id.not_in(list(exclude)))
        if room.trunk_full or room.rate_full:
            query = query.where(sa.not_(self._over_trunk(j, values)))
        query = query.order_by(sa.func.coalesce(j.c.urgent, 0).desc(), served.c.last.is_not(None), served.c.last,
                               d.c.created_at, d.c.id).limit(1)
        return connection.execute(query).mappings().one_or_none()

    def number_has_room(self, connection, number, now):
        busy, dest = self._busy(now), self.t['delivery_destinations']
        calls = connection.scalar(sa.select(busy.c.calls).where(busy.c.number == number)) or 0
        limit = connection.scalar(sa.select(dest.c.max_calls).where(dest.c.phone_number == number))
        limit = DEFAULT_CALLS_TO_A_NUMBER if limit is None else limit
        return limit == 0 or calls < limit

    def group_may_start(self, connection, values, rows, now):
        """Faxes sent together go in one call: their number needs room, and the trunk if they go over it."""
        if not rows:
            return False
        if not self.number_has_room(connection, rows[0]['phone_number'], now):
            return False
        j = self.t['fax_jobs']
        over = connection.scalar(sa.select(sa.func.count()).select_from(j).where(
            j.c.id == rows[0]['id'], self._over_trunk(j, values)))
        if over:
            room = self.room(connection, values, now)
            return not (room.trunk_full or room.rate_full)
        return True

    # What people see ----------------------------------------------------------
    def waiting_sentence(self, job_id, values, now):
        """One sentence while a fax waits for room, or holds a number with an unknown result; else None."""
        from .people_time import clock
        d, j, a = self.t['outbound_deliveries'], self.t['fax_jobs'], self.t['outbound_attempts']
        c = self.t['delivery_attempt_costs']
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(d.c.state, d.c.dispatch_mode, j.c.to_number, j.c.backend, j.c.send_by_call,
                          a.c.submitted_at, c.c.route)
                .select_from(d.join(j, j.c.id == d.c.id).outerjoin(a, a.c.id == d.c.attempt_id)
                             .outerjoin(c, c.c.id == d.c.attempt_id))
                .where(d.c.id == job_id)).mappings().one_or_none()
            if row is None:
                return None
            if row['state'] == 'reconciliation_required':
                if (row['submitted_at'] is None or row['submitted_at'] + HOLD <= now
                        or (row['route'] or '') in NO_CALL_ROUTES):
                    return None
                return ('Its result is unknown, so Faxbot keeps this number free of other faxes until the result '
                        f'is known or until {clock(row["submitted_at"] + HOLD)}.')
            if row['state'] != 'ready' or row['dispatch_mode'] != 'normal':
                return None
            if not self.number_has_room(connection, row['to_number'], now):
                holds = self.holds(now).subquery()
                blocking = connection.execute(sa.select(holds.c.state, holds.c.submitted_at).where(
                    holds.c.number == row['to_number'])).all()
                unknown = [submitted for state, submitted in blocking
                           if state == 'reconciliation_required' and submitted is not None]
                if unknown and len(unknown) == len(blocking):
                    return ('Waiting: an earlier fax to this number has an unknown result. Faxbot waits until it is '
                            f'known or until {clock(min(unknown) + HOLD)}.')
                return 'Waiting: another fax is calling this number.'
            over = connection.scalar(sa.select(sa.func.count()).select_from(j).where(
                j.c.id == job_id, self._over_trunk(j, values)))
            if over:
                room = self.room(connection, values, now)
                if room.trunk_full:
                    lines = room.trunk_limit
                    return f"Waiting for a free line: all {lines} {'line is' if lines == 1 else 'lines are'} in use."
                if room.rate_full:
                    return (f'Waiting a moment: Faxbot starts at most {room.rate_limit} new '
                            f"{'call' if room.rate_limit == 1 else 'calls'} each second on your phone line.")
        return None

    def waiting_for_line(self, values, now, *, waiting=None):
        """How many faxes are ready to go but wait for room (Overview)."""
        d, j, dest = self.t['outbound_deliveries'], self.t['fax_jobs'], self.t['delivery_destinations']
        busy = self._busy(now)
        limit = sa.func.coalesce(dest.c.max_calls, DEFAULT_CALLS_TO_A_NUMBER)
        with self.engine.connect() as connection:
            room = self.room(connection, values, now)
            blocked = sa.and_(limit != 0, sa.func.coalesce(busy.c.calls, 0) >= limit)
            if room.trunk_full or room.rate_full:
                blocked = sa.or_(blocked, self._over_trunk(j, values))
            query = (sa.select(sa.func.count()).select_from(
                d.join(j, j.c.id == d.c.id).outerjoin(dest, dest.c.phone_number == j.c.to_number)
                .outerjoin(busy, busy.c.number == j.c.to_number))
                .where(d.c.state == 'ready', d.c.dispatch_mode == 'normal', blocked))
            if waiting is not None:
                query = query.where(d.c.id.not_in(waiting))
            return int(connection.scalar(query) or 0)


_CACHE = {}


def for_engine(engine, connection=None):
    """One ``Capacity`` per engine, reflected once (through ``connection`` inside an open transaction)."""
    cached = _CACHE.get(id(engine))
    if cached is not None and cached.engine is engine:
        return cached
    capacity = Capacity(engine, connection)
    if len(_CACHE) > 16:
        _CACHE.clear()
    _CACHE[id(engine)] = capacity
    return capacity
