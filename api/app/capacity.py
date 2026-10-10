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
go first, then faxes whose send-by time is within the hour (earliest first);
otherwise the sender served least recently goes next, oldest fax first, so one
sender's batch cannot hold up everyone else.

A fax that has room may still be held by its recipient's schedule
(``routing/schedule.py``: the hours the recipient takes faxes, and hours its
line is usually busy when a failed try may be charged). A held fax stays
``ready``, is never claimed until its time comes, and never fails for waiting;
the claim looks at it again within ``schedule.RECHECK``. A fax in a busy hour
whose failed tries are free is not held: it goes after the faxes that could
use the same room.
"""
from dataclasses import dataclass, replace
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
    # An analog line's gateway carries one call per line (sip_trunk.ANALOG_GATEWAYS).
    from .sip_trunk import PRESETS
    preset = PRESETS.get(getattr(values, 'sip_trunk_preset', '') or '')
    if preset is not None and preset.lines:
        return preset.lines
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


# -- several trunks and accounts (provider-rules design §3.6) ---------------------------------------------------
#
# Each trunk account has its own lines and calls a second: its account limits (``limits.at_once`` and
# ``limits.calls_per_second``), else its carrier's published limits, else the fax engine's lines. Another
# account with a "faxes at once" limit (a second Sinch account, say) holds that many faxes in progress. A fax's
# delivery holds the account its attempt was given (``delivery_attempt_costs.route``, an account key), else the
# account the fax was accepted with; a call coming in holds its trunk (``sip_call_records.trunk_key``; none is
# the first trunk). The limit per number is unchanged and counts every route that calls.

@dataclass(frozen=True)
class Limited:
    """One account whose room the claim gate watches: a trunk, or an account with a "faxes at once" limit."""
    key: str
    label: str
    trunk: bool
    at_once: int | None
    calls_per_second: int | None
    # The key fax_jobs.backend holds for a fax accepted with this account (the provider id of a first account).
    backend: str | None = None
    # A carrier account several trunks share (``carrier_groups``): the trunk keys whose calls add up against the
    # carrier's account-wide limits. Empty for an account's own room.
    members: tuple = ()


# -- trunks on one carrier account ----------------------------------------------------------------------------------
#
# A carrier's published limits (``CARRIERS``) hold for the whole carrier account, whatever its connections: two
# Telnyx trunk accounts on one Telnyx account share its 2 calls at once. Faxbot knows two trunks are one carrier
# account when they read it with the same API key, or sign in as the same user at the same server; it knows they
# are two when their API keys differ. Otherwise it keeps them apart and says so beside the trunks.

def _carrier_identity(trunk):
    """What identifies a trunk's carrier account, or None when Faxbot cannot tell (never the secret itself)."""
    import hashlib
    own = trunk.values
    preset = getattr(own, 'sip_trunk_preset', '') or ''
    key = getattr(own, 'telnyx_api_key', '') or '' if preset == 'telnyx' else ''
    if key:
        return 'key:' + hashlib.sha256(key.encode()).hexdigest()[:16]
    if getattr(own, 'sip_trunk_auth', '') == 'registration' and getattr(own, 'sip_trunk_username', ''):
        from .sip_trunk import PRESETS
        host = getattr(own, 'sip_trunk_host', '') or getattr(PRESETS.get(preset), 'host', '')
        return 'user:' + hashlib.sha256(f'{own.sip_trunk_username}@{host}'.encode()).hexdigest()[:16]
    return None


def carrier_groups(values):
    """([Limited for each carrier account two or more trunks share], [one sentence per pair Faxbot can't tell])."""
    from itertools import combinations
    from . import sip_trunk
    from .provider_labels import trunk_name
    by_preset = {}
    for trunk in sip_trunk.trunk_accounts(values):
        preset = getattr(trunk.values, 'sip_trunk_preset', '') or ''
        if preset in CARRIERS:
            by_preset.setdefault(preset, []).append((trunk, _carrier_identity(trunk)))
    groups, notes = [], []
    for preset, items in by_preset.items():
        if len(items) < 2:
            continue
        limits, carrier = CARRIERS[preset], trunk_name(preset)
        shared = {}
        for trunk, identity in items:
            if identity:
                shared.setdefault(identity, []).append(trunk)
        for identity, trunks in shared.items():
            if len(trunks) < 2:
                continue
            # The carrier's published limit, unless you set more lines at once on one of these trunks (an account
            # verified to a higher level): then that is the account's limit.
            chosen = [getattr(trunk.values, 'sip_trunk_max_calls', 0) or 0 for trunk in trunks]
            at_once = max(chosen) if any(chosen) else limits.calls_at_once
            groups.append(Limited(f'carrier:{preset}:{identity.split(":", 1)[1]}', f'your {carrier} account', True,
                                  at_once, limits.calls_per_second, None, tuple(trunk.key for trunk in trunks)))
        for (first, one), (second, other) in combinations(items, 2):
            if one is not None and one == other:
                continue
            if one and other and one.startswith('key:') and other.startswith('key:'):
                continue  # two API keys: two carrier accounts, each with its own limits
            notes.append(f"Faxbot can't tell whether {first.label} and {second.label} are one {carrier} account. If "
                         f"they are, their calls at once add up against {carrier}'s limit of {limits.calls_at_once}.")
    return groups, notes


def limited_accounts(values):
    """{key: Limited} for every account whose calls or faxes at once are limited, the first trunk first."""
    from . import sip_trunk
    found = {}
    default_trunk = None
    from .accounts import default_sending_key
    try:
        default_trunk = default_sending_key(values)
    except Exception:
        default_trunk = None
    if trunk_in_use(values):
        # Named only when there are several trunks (room_sentence); the first trunk's account name.
        from .accounts import account_named
        try:
            label = getattr(account_named(values, TRUNK), 'label', None) or 'your phone line'
        except Exception:
            label = 'your phone line'
        found[TRUNK] = Limited(TRUNK, label, True, trunk_calls_at_once(values), calls_per_second(values), TRUNK)
    for trunk in sip_trunk.extra_trunks(values):
        # A fax accepted while this trunk was the default sending account is stored with backend 'sip'.
        backend = TRUNK if default_trunk == trunk.key and TRUNK not in found else None
        found[trunk.key] = Limited(trunk.key, trunk.label, True, trunk_calls_at_once(trunk.values),
                                   calls_per_second(trunk.values), backend)
    from .accounts import all_accounts
    try:
        listed = all_accounts(values)
    except Exception:
        listed = ()
    for account in listed:
        if account.provider == 'sip' or not account.at_once or account.key in found:
            continue
        found[account.key] = Limited(account.key, account.label, False, account.at_once, None,
                                     account.key if account.primary else None)
    try:
        groups, _ = carrier_groups(values)
    except Exception:
        groups = []
    for group in groups:
        found[group.key] = group
    return found


@dataclass(frozen=True)
class Room:
    """What is full right now on one account; ``trunk_full`` and ``rate_full`` apply only to faxes using it."""
    trunk_calls: int
    trunk_limit: int | None
    recent_starts: int
    rate_limit: int | None
    # The account (a trunk, or another account with a limit) and its name for the waiting sentence.
    key: str = TRUNK
    label: str = 'your phone line'
    trunk: bool = True

    @property
    def trunk_full(self):
        return self.trunk_limit is not None and self.trunk_calls >= self.trunk_limit

    @property
    def rate_full(self):
        return self.rate_limit is not None and self.recent_starts >= self.rate_limit

    @property
    def full(self):
        return self.trunk_full or self.rate_full


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
        # Faxes the recipient's schedule holds, and until when the claim leaves each alone (naive UTC).
        self.held = {}

    # The recipient's schedule (routing/schedule.py) --------------------------------------------
    def _scheduler(self, connection=None):
        """The schedule reader, or None when it cannot be read: the claim then ignores schedules."""
        try:
            from .routing.schedule import for_engine
            return for_engine(self.engine, connection)
        except Exception:
            return None

    def decision(self, connection, values, job_ids, now, memo=None):
        """(``schedule.Decision``, ``schedule.Settings``) for faxes going in one call, or (None, None).

        ``memo`` keeps each number's settings and learned busy hours for the rest of one claim, so a queue of
        faxes to one number reads its history once.
        """
        scheduler = self._scheduler(connection)
        if scheduler is None:
            return None, None
        try:
            fax = scheduler.fax_on(connection, job_ids, values)
            if fax is None:
                return None, None
            return scheduler.decide_on(connection, fax, values, now, memo)
        except Exception:
            # A schedule that cannot be read never stops a fax.
            return None, None

    def forget_holds(self):
        """Look at every held fax again at the next claim (after a recipient's schedule changed)."""
        self.held.clear()

    def _held(self, now):
        self.held = {job: until for job, until in self.held.items() if until > now}
        return list(self.held)

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

    def room(self, connection, values, now, *, exclude=(), trunk=None, limited=None, lines=1):
        """One account's calls (or faxes) in progress and new calls in the last second, against its limits.

        ``trunk`` is the account's key: a trunk (``sip``, the default, is the first trunk), or another account
        with a "faxes at once" limit. An account with no limit has unlimited room. ``limited`` is
        ``limited_accounts(values)`` when the caller already has it. ``lines``: the lines the next call needs at
        once (two for a call to one of the trunk's own numbers, which comes back in on it): the room counts the
        extra lines as taken, on the trunk and on a carrier account it shares.
        """
        key = trunk or TRUNK
        limited = limited if limited is not None else limited_accounts(values)
        found = limited.get(key)
        if found is None:
            return Room(0, None, 0, None, key=key, label=key, trunk=False)
        extra = max(int(lines or 1), 1) - 1
        mine = self._room(connection, now, found, exclude)
        mine = replace(mine, trunk_calls=mine.trunk_calls + extra) if extra else mine
        if mine.full or found.members:
            return mine
        # A trunk on a carrier account it shares with other trunks: that account's limits hold for all of them.
        for group in limited.values():
            if key in group.members:
                shared = self._room(connection, now, group, exclude)
                shared = replace(shared, trunk_calls=shared.trunk_calls + extra) if extra else shared
                if shared.full:
                    return shared
        return mine

    def _room(self, connection, now, found, exclude=()):
        """The calls (or faxes) one Limited holds now, against its own limits."""
        key = found.key
        routes = sorted(set(found.members) | ({key, found.backend} - {None})) if found.members else \
            sorted({key, found.backend} - {None})
        trunks = list(found.members) if found.members else [key]
        holds = self.holds(now, exclude=exclude).subquery()
        outgoing = connection.scalar(sa.select(sa.func.count(sa.distinct(holds.c.call))).where(
            holds.c.route.in_(routes)))
        incoming = 0
        if found.trunk:
            records = self.t['sip_call_records']
            # A call coming in holds its trunk; one that names no trunk came in on the first trunk.
            on_trunk = (sa.func.coalesce(records.c.trunk_key, TRUNK).in_(trunks) if 'trunk_key' in records.c
                        else sa.true() if TRUNK in trunks else sa.false())
            incoming = connection.scalar(sa.select(sa.func.count()).select_from(records).where(
                records.c.direction == 'inbound', records.c.ended_at.is_(None), records.c.started_at >= now - HOLD,
                on_trunk))
        starts = 0
        if found.calls_per_second:
            a, c, j = self.t['outbound_attempts'], self.t['delivery_attempt_costs'], self.t['fax_jobs']
            starts = connection.scalar(sa.select(sa.func.count()).select_from(
                a.join(j, j.c.id == a.c.job_id).outerjoin(c, c.c.id == a.c.id)).where(
                a.c.submitted_at > now - timedelta(seconds=1), a.c.submitted_at <= now,
                sa.func.coalesce(c.c.route, j.c.backend).in_(routes)))
        return Room(int(outgoing or 0) + int(incoming or 0), found.at_once, int(starts or 0),
                    found.calls_per_second, key=key, label=found.label, trunk=found.trunk)

    def rooms(self, connection, values, now, *, exclude=(), limited=None):
        """{account key: Room} for every account whose calls or faxes at once are limited: its own room, or the
        carrier account it shares with other trunks when that is the one that is full."""
        limited = limited if limited is not None else limited_accounts(values)
        return {key: self.room(connection, values, now, exclude=exclude, trunk=key, limited=limited)
                for key, item in limited.items() if not item.members}

    def _busy(self, now):
        holds = self.holds(now).subquery()
        return (sa.select(holds.c.number, sa.func.count(sa.distinct(holds.c.call)).label('calls'))
                .group_by(holds.c.number).subquery())

    def _not_local(self, jobs, values, condition):
        """``condition``, for faxes that are not delivered inside Faxbot (an own number places no call)."""
        own = sorted(_own_numbers(values))
        if not own:
            return condition
        return sa.and_(condition, sa.or_(jobs.c.to_number.not_in(own),
                                         sa.func.coalesce(jobs.c.send_by_call, 0) == 1))

    def _over_trunk(self, jobs, values):
        """Faxes the claim gate treats as going over the trunk: bound to it, and not delivered inside Faxbot."""
        return self._not_local(jobs, values, jobs.c.backend == TRUNK)

    def _decisions(self, connection):
        """The sending rules' decisions table (fax_job_rule_decisions), or None before it exists."""
        found = getattr(self, '_decision_table', False)
        if found is False:
            try:
                found = sa.Table('fax_job_rule_decisions', sa.MetaData(), autoload_with=connection)
            except sa.exc.SQLAlchemyError:
                found = None
            self._decision_table = found
        return found

    def _bound_to_full(self, connection, jobs, values, full, limited):
        """Faxes without a rule decision whose own account is full: they wait at the claim gate, as before rules."""
        backends = sorted({limited[key].backend for key in full if limited[key].backend})
        if not backends:
            return sa.false()
        condition = self._not_local(jobs, values, jobs.c.backend.in_(backends))
        decisions = self._decisions(connection)
        if decisions is not None:
            # A fax with a rule decision is checked against its envelope instead (``_waits_for_room``).
            condition = sa.and_(condition, ~sa.exists().where(decisions.c.job_id == jobs.c.id))
        return condition

    def _pinned(self, connection, job_id):
        """(Decision, Facts) of the fax's current rule decision, None without one, 'unreadable' when it can't be read."""
        decisions = self._decisions(connection)
        if decisions is None:
            return None
        row = connection.execute(sa.select(decisions.c.decision, decisions.c.facts).where(
            decisions.c.job_id == job_id).order_by(decisions.c.sequence.desc()).limit(1)).first()
        if row is None:
            return None
        from .rules import model
        try:
            return model.Decision.from_json(row.decision), model.Facts.from_json(row.facts)
        except (TypeError, ValueError, KeyError):
            return 'unreadable'

    def _waits_for_room(self, connection, job_id, values, full, limited):
        """The full account a fax with a rule decision waits for, or None when it may be offered now.

        ``when_busy: next``: it waits only when every calling account its envelope allows is full, so a full
        first account never holds it while the next one has room. ``wait`` (the default): it waits when its
        envelope's first account is full and the rule keeps that order (``use``, ``try_in_order``, a site's
        accounts in order), or when every allowed calling account is full. A fax its rules send inside Faxbot
        or straight to a verified partner places no call and never waits here. Faxes stay ``ready``.
        """
        pinned = self._pinned(connection, job_id)
        if not isinstance(pinned, tuple):
            return None  # no decision (the query already checked it) or unreadable (dispatch holds it in Sent)
        decision, facts = pinned
        if decision.outcome == 'blocked':
            return None
        envelope = decision.envelope
        jobs = self.t['fax_jobs']
        job = connection.execute(sa.select(jobs.c.backend, jobs.c.to_number, jobs.c.send_by_call).where(
            jobs.c.id == job_id)).first()
        if job is None:
            return None
        local = envelope.local and not job.send_by_call and job.to_number in _own_numbers(values)
        if local or (envelope.direct and facts.partner):
            return None
        from .rules import model
        if envelope.mode == 'automatic':
            bound = next((key for key, item in limited.items() if item.backend == job.backend), None)
            calling = [bound] if bound else []
        else:
            calling = [key for key in envelope.accounts if not model.is_relay(key)]
        if not calling:
            return None
        if all(key in full for key in calling):
            return calling[0]
        if envelope.when_busy != 'next' and envelope.mode in ('one', 'ordered', 'automatic') and calling[0] in full:
            return calling[0]
        return None

    # Admission --------------------------------------------------------------
    def next_ready(self, connection, values, now, *, waiting=None, exclude=()):
        """The delivery to claim next, or None.

        Order: urgent first; then faxes whose send-by time is within the hour,
        earliest first; then the sender served least recently, oldest first.
        Only faxes whose number has room are offered, and while the trunk is full
        (or has started its calls for this second) no fax going over it is. Of
        those, a fax its recipient's schedule holds is skipped (and left alone
        until ``schedule.RECHECK`` has passed), and a fax in a busy hour whose
        failed tries are free goes only when no other offered fax may.
        """
        d, j, dest = self.t['outbound_deliveries'], self.t['fax_jobs'], self.t['delivery_destinations']
        busy = self._busy(now)
        limit = sa.func.coalesce(dest.c.max_calls, DEFAULT_CALLS_TO_A_NUMBER)
        limited = limited_accounts(values)
        full = {key for key, room in self.rooms(connection, values, now, limited=limited).items() if room.full}
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
        query = (sa.select(d.c.id)
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
        if full:
            # A fax accepted without a rule decision waits for its own account, as before rules existed.
            query = query.where(sa.not_(self._bound_to_full(connection, j, values, full, limited)))
        order = [sa.func.coalesce(j.c.urgent, 0).desc()]
        if 'send_by' in j.c:
            from .routing.schedule import DEADLINE_FIRST
            soon = sa.and_(j.c.send_by.is_not(None), j.c.send_by <= now + DEADLINE_FIRST)
            order += [sa.case((soon, 0), else_=1), sa.case((soon, j.c.send_by), else_=None)]
        query = query.order_by(*order, served.c.last.is_not(None), served.c.last, d.c.created_at, d.c.id)
        return self._first_due(connection, values, now, query, d, full=full, limited=limited)

    # At most this many offered faxes are looked at per page, and this many pages per claim.
    CANDIDATES, PAGES = 25, 4

    def _first_due(self, connection, values, now, query, deliveries, *, full=(), limited=None):
        """The first offered delivery its recipient's schedule lets start now (see ``next_ready``).

        While an account is full, a fax with a rule decision is offered only when its envelope lets it go
        another way (``_waits_for_room``); one that must wait stays ``ready`` and is looked at again next claim.
        """
        from .routing.schedule import RECHECK
        skip, later, memo = self._held(now), None, {}
        for _ in range(self.PAGES):
            page = query.where(deliveries.c.id.not_in(skip)) if skip else query
            ids = connection.execute(page.limit(self.CANDIDATES)).scalars().all()
            for job_id in ids:
                if full and self._waits_for_room(connection, job_id, values, full, limited) is not None:
                    skip.append(job_id)
                    continue
                decision, _ = self.decision(connection, values, [job_id], now, memo)
                if decision is not None and decision.hold_until is not None:
                    self.held[job_id] = min(decision.hold_until, now + RECHECK)
                elif decision is not None and decision.later:
                    later = later or job_id
                else:
                    return self._delivery(connection, deliveries, job_id)
                skip.append(job_id)
            if len(ids) < self.CANDIDATES:
                break
        return self._delivery(connection, deliveries, later) if later else None

    @staticmethod
    def _delivery(connection, deliveries, job_id):
        return connection.execute(sa.select(deliveries).where(deliveries.c.id == job_id)).mappings().one_or_none()

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
        if self.waits_for(connection, values, rows[0]['id'], now) is not None:
            return False
        # The recipient's schedule holds the whole call, which waits still together.
        ids = [row['id'] for row in rows]
        if set(ids) & set(self._held(now)):
            return False
        decision, _ = self.decision(connection, values, ids, now)
        if decision is not None and decision.hold_until is not None:
            from .routing.schedule import RECHECK
            until = min(decision.hold_until, now + RECHECK)
            self.held.update({job_id: until for job_id in ids})
            return False
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
            held = self.schedule_sentence(connection, values, job_id, row, now)
            if held:
                return held
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
            room = self.waits_for(connection, values, job_id, now)
            if room is not None:
                return room_sentence(room, several=sum(item.trunk for item in limited_accounts(values).values()) > 1)
        return None

    def waits_for(self, connection, values, job_id, now):
        """The ``Room`` of the full account this fax waits for at the claim gate, or None."""
        limited = limited_accounts(values)
        if not limited:
            return None
        rooms = self.rooms(connection, values, now, limited=limited)
        full = {key for key, room in rooms.items() if room.full}
        if not full:
            return None
        j = self.t['fax_jobs']
        bound = connection.execute(sa.select(j.c.id).where(
            j.c.id == job_id, self._bound_to_full(connection, j, values, full, limited))).first()
        if bound is not None:
            backend = connection.scalar(sa.select(j.c.backend).where(j.c.id == job_id))
            key = next(key for key in full if limited[key].backend == backend)
            return rooms[key]
        key = self._waits_for_room(connection, job_id, values, full, limited)
        return rooms[key] if key is not None else None

    def schedule_sentence(self, connection, values, job_id, row, now):
        """Why the recipient's schedule holds this fax, in one sentence with what a failed try may cost; or None."""
        decision, settings = self.decision(connection, values, [job_id], now)
        if decision is None or decision.hold_until is None:
            return None
        from .provider_labels import provider_label
        from .routing.schedule import attempt_price, reason
        route = row['backend'] or ''
        price = None
        if decision.why == 'busy':
            pages = connection.scalar(sa.select(self.t['fax_jobs'].c.pages).where(self.t['fax_jobs'].c.id == job_id))
            price = attempt_price(route, row['to_number'], pages, now=now)
        return reason(decision, settings, route_label=provider_label(route), price_text=price, now=now)

    def waiting_for_line(self, values, now, *, waiting=None):
        """How many faxes are ready to go but wait for room (Overview)."""
        d, j, dest = self.t['outbound_deliveries'], self.t['fax_jobs'], self.t['delivery_destinations']
        busy = self._busy(now)
        limit = sa.func.coalesce(dest.c.max_calls, DEFAULT_CALLS_TO_A_NUMBER)
        with self.engine.connect() as connection:
            limited = limited_accounts(values)
            full = {key for key, room in self.rooms(connection, values, now, limited=limited).items() if room.full}
            blocked = sa.and_(limit != 0, sa.func.coalesce(busy.c.calls, 0) >= limit)
            if full:
                blocked = sa.or_(blocked, self._bound_to_full(connection, j, values, full, limited))
            query = (sa.select(sa.func.count()).select_from(
                d.join(j, j.c.id == d.c.id).outerjoin(dest, dest.c.phone_number == j.c.to_number)
                .outerjoin(busy, busy.c.number == j.c.to_number))
                .where(d.c.state == 'ready', d.c.dispatch_mode == 'normal', blocked))
            if waiting is not None:
                query = query.where(d.c.id.not_in(waiting))
            count = int(connection.scalar(query) or 0)
            decisions = self._decisions(connection) if full else None
            if decisions is not None:
                # Faxes with a rule decision wait only when their envelope allows nothing with room.
                decided = (sa.select(d.c.id).select_from(d.join(j, j.c.id == d.c.id)).where(
                    d.c.state == 'ready', d.c.dispatch_mode == 'normal',
                    sa.exists().where(decisions.c.job_id == d.c.id)).limit(200))
                if waiting is not None:
                    decided = decided.where(d.c.id.not_in(waiting))
                count += sum(self._waits_for_room(connection, job_id, values, full, limited) is not None
                             for job_id in connection.execute(decided).scalars())
            return count


def room_sentence(room, *, several=False):
    """One sentence for a fax waiting for room on ``room``'s account.

    With one trunk the sentences are the ones Faxbot always showed; with several, they name the trunk.
    """
    if room.trunk_full:
        lines = room.trunk_limit
        if room.trunk and not several and room.key == TRUNK:
            return f"Waiting for a free line: all {lines} {'line is' if lines == 1 else 'lines are'} in use."
        if room.trunk:
            return (f"Waiting for a free line on {room.label}: all {lines} "
                    f"{'line is' if lines == 1 else 'lines are'} in use.")
        return (f"Waiting: {room.label} already has {lines} {'fax' if lines == 1 else 'faxes'} in progress, its "
                'limit at once.')
    if room.rate_full:
        calls = 'call' if room.rate_limit == 1 else 'calls'
        where = 'your phone line' if not several and room.key == TRUNK else room.label
        return f'Waiting a moment: Faxbot starts at most {room.rate_limit} new {calls} each second on {where}.'
    return None


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
