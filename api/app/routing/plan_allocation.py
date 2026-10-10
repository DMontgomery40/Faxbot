"""Give a scarce plan's pages to the waiting faxes where they avoid the most cost, and keep a reserve for later ones.

``plan_budget.marginal`` prices one fax against the budget that is left, so the
first fax to come takes the plan. When the allowance is scarce (eFax's 200
pages, a fair-use budget near its end, a trunk's included minutes) that can
force a later, dearer fax onto a paid route. Two queued 100-page faxes and 100
included pages, one costing $0.50 elsewhere and the other $50: first come, first
served pays $50; giving the pages where they avoid the most pays $0.50.

What this module does
---------------------
1. **Waiting faxes** (``outbound_deliveries`` ready to send, or claimed with no
   route chosen yet) are sorted into three groups for each scarce plan:

   - *never on the plan*: their rules leave it out, they go to one of your own
     numbers or a partner, or another route is first even when the plan is free
     (a free toll-free call);
   - *on the plan whatever its price* (``forced``): their rules list the plan
     first in a fixed order, the number's preferred route is the plan, they
     have no sending rules decision (ranked by the rate card), or no other route
     they may use has a known price. Their pages are taken from the room first;
   - *candidates*: everything else. Each has a **paid cost it avoids** on the
     plan: the expected cost of its best other usable route (the shared
     predictor, through ``pricing``), capped by what the same plan charges past
     its allowance.

2. **The allocation** is a 0/1 knapsack over whole faxes (``solve``): pages,
   started minutes, or both pages and faxes for a normal-use budget, with an
   allowance's price per page or minute past it. It is solved exactly by
   dynamic programming over the room each plan has left (``MAX_STATES``); a
   larger problem uses a greedy order by saving per unit and reports its
   fractional upper bound, so the gap to the best possible answer is known
   (``Solution.bound_micros``). Ties go to the fax that came first.

3. **A reserve for faxes not yet queued** (``reserve_curve``): the time left
   until the plan renews is laid over earlier stretches of the same length
   (``LOOKBACK``, never after ``now``), and for each reserve size Faxbot works
   out what those stretches' faxes would have saved with it. The reserve is the
   size whose **lower** confidence bound (``Z``) saving most exceeds what the
   waiting faxes pay extra for it. Fewer than ``MIN_WINDOWS`` stretches with
   faxes means no reserve. As renewal nears, the stretches shorten and the
   reserve falls. A fax marked urgent or with a send-by time never gives up its
   pages to the reserve.

4. **The planner** reads it (``pricing.prices_for(..., job_id=...)``): a fax not
   given the plan ranks it at its marginal price after the pages held for the
   others (``after_hold``), and a fax that went another way records why
   (``record``), with the amounts, for Sent details.

The allocation only changes which route a fax takes. It never makes a fax wait,
so it cannot make one miss a send-by time, and it never changes a rule: a fax
whose rules allow only the plan, or whose other routes are over a cost cap,
turned off or unpriced, is forced onto the plan as before. Every route takes a
fax the same time to send (``schedule.call_time``), so a send-by time never
makes the plan feasible and another route not; it keeps the fax's pages from
the reserve. A fax its sending rules hold (approval, a window, no allowed
route) takes no room while it is held. A plan with no limit and no budget you
set (``state == 'no_limit'``) gets no allocation and no reserve; a committed
monthly spend is not allocated. Not handled: a fax a recipient's hours hold
past the plan's renewal still counts as waiting now, and every waiting fax is
worked out with the configuration of the fax being planned.

Contract for callers
--------------------
- ``hold_for(routes, values, job_id, keys, *, now=None, dial=None) -> {key: Hold}``:
  for each plan among ``keys``, what part of its room this fax may not use (held
  for faxes already on their way, faxes given the plan, and the reserve). A plan
  is left out when the fax may use all of it, it is not scarce, or the fax is
  forced onto it; storage errors leave every plan out (priced as before).
- ``after_hold(left, hold) -> BudgetLeft``: the plan's budget as this fax sees it.
- ``view(routes, values, now=None)``: the allocation screen and ``faxbot costs plans allocation``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import logging
import math
import threading


# Exact dynamic programming is used while the reachable room states of one layer stay under this, and the steps
# it takes in all under ``MAX_WORK`` (each fax tried on each room state); past either, the greedy order answers.
MAX_STATES = 20_000
MAX_WORK = 200_000
# Waiting faxes looked at, in the order the claim offers them (the fax being planned is always included).
MAX_WAITING = 60
# The reserve learns from at most this long before now, in stretches as long as the time left until renewal ...
LOOKBACK = timedelta(days=180)
MAX_WINDOWS = 12
# ... and needs at least this many stretches with faxes in them; fewer is not evidence.
MIN_WINDOWS = 3
# One-sided 95% lower confidence bound on the mean saving of a reserve size.
Z = 1.645
# Past faxes read for the reserve, newest first.
HISTORY_READ = 400
# Reserve sizes tried: this many steps of the room, plus the whole room.
RESERVE_STEPS = 10
# How long a waiting fax's routes and a plan's reserve are kept before being worked out again.
FEATURES_SECONDS = 120
RESERVE_SECONDS = 3600
GROUPS = ('never', 'forced', 'candidate')
KINDS = ('queue', 'reserve', 'both')


# The problem ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Dim:
    """One limited count of a plan: ``room`` units left; past it each unit costs ``overage`` micros, or is not
    allowed (``None``: a normal-use budget, or an allowance whose extra price is not known)."""
    name: str                    # 'pages', 'faxes' or 'minutes'
    room: int
    overage: int | None = None


@dataclass(frozen=True)
class PlanRoom:
    route: str
    dims: tuple                  # of Dim

    def __post_init__(self):
        if not self.dims:
            raise ValueError('A plan needs at least one limited count.')


@dataclass(frozen=True)
class Claimant:
    """One waiting fax. ``weights`` maps a plan route to its units on each of that plan's dims, in order.

    ``alternative`` is the cost in micros of its best other usable route; None means it has none with a known
    price, so it is ``forced`` onto its first plan whatever the plan's price.
    """
    job_id: str
    position: int
    weights: dict
    alternative: int | None = None
    forced: str | None = None    # the plan route it takes whatever its price
    protected: bool = False      # urgent or with a send-by time: never gives pages to the reserve

    @property
    def plans(self):
        return tuple(self.weights)


@dataclass(frozen=True)
class Solution:
    assigned: dict               # job id -> plan route, or None (its other route)
    cost_micros: int             # the waiting faxes' cost: other routes plus pages past each allowance
    baseline_micros: int         # the same with no plan pages for any candidate
    exact: bool
    bound_micros: int | None = None   # an upper bound on the saving (``baseline - cost``) when not exact
    loads: dict = field(default_factory=dict)   # plan route -> units used on each dim

    @property
    def saving_micros(self):
        return self.baseline_micros - self.cost_micros


def _dims(plans):
    """``[(plan index, dim index, Dim)]`` across all plans, in order."""
    return [(p, d, dim) for p, plan in enumerate(plans) for d, dim in enumerate(plan.dims)]


def _step(state, flat, plan_index, weights, forced):
    """``(new state, extra micros)`` for adding ``weights`` to plan ``plan_index``; None when it does not fit."""
    new, extra = list(state), 0
    for index, (p, d, dim) in enumerate(flat):
        if p != plan_index:
            continue
        weight = weights[d]
        load = state[index]
        room = max(0, dim.room)
        total = load + weight
        if dim.overage is None:
            if total > room and not forced:
                return None
            new[index] = min(total, room + 1)
        else:
            before = max(0, load - room)
            after = max(0, total - room)
            extra += (after - before) * dim.overage
            new[index] = min(total, room + 1)
    return tuple(new), extra


def _baseline(claimants, plans, flat):
    """Every candidate on its other route; forced faxes on their plan, in order."""
    index = {plan.route: p for p, plan in enumerate(plans)}
    state, cost = tuple(0 for _ in flat), 0
    for claimant in claimants:
        if claimant.forced is not None:
            state, extra = _step(state, flat, index[claimant.forced], claimant.weights[claimant.forced], True)
            cost += extra
        else:
            cost += claimant.alternative
    return cost


def solve(claimants, plans, *, max_states=MAX_STATES, keep=None):
    """The allocation of ``plans``' room among ``claimants`` that costs least in all (``Solution``).

    ``keep`` maps job ids to a plan route they must keep (the reserve never takes a protected fax's pages).
    Exact while each layer's reachable states stay under ``max_states``; otherwise greedy with a bound.
    """
    claimants = arrival(claimants)
    plans = tuple(plans)
    flat = _dims(plans)
    keep = keep or {}
    baseline = _baseline(claimants, plans, flat)
    found = _exact(claimants, plans, flat, max_states, keep)
    if found is not None:
        assigned, cost, loads = found
        return Solution(assigned, cost, baseline, True, None, loads)
    assigned, cost, loads = _greedy(claimants, plans, flat, keep)
    bound = _upper_bound(claimants, plans)
    return Solution(assigned, cost, baseline, False, bound, loads)


def arrival(claimants):
    """Forced faxes first (their pages go whatever the price), then the rest in the order the claim offers them."""
    return sorted(claimants, key=lambda item: (item.forced is None, item.position, item.job_id))


def _options(claimant, index, keep):
    """``[(choice, plan index or None, forced)]``; choice digits order ties: plans in order, then the other route."""
    if claimant.forced is not None:
        return [(index[claimant.forced], index[claimant.forced], True)]
    if claimant.job_id in keep:
        return [(index[keep[claimant.job_id]], index[keep[claimant.job_id]], False)]
    found = [(index[route], index[route], False) for route in claimant.weights if route in index]
    return sorted(found) + [(len(index), None, False)]


def _exact(claimants, plans, flat, max_states, keep):
    """Forward dynamic programming over capped loads. Each state keeps its least ``(cost, choices)``, the choices
    read as a number in base ``B`` (one more than the number of plans): the least cost and, among equal costs, the
    plan for the earlier fax. None when a layer passes ``max_states`` or the work passes ``MAX_WORK``."""
    index = {plan.route: p for p, plan in enumerate(plans)}
    base = len(plans) + 1
    n = len(claimants)
    states = {tuple(0 for _ in flat): ((0, 0), None)}   # state -> ((cost, choice number), (choice, earlier) chain)
    work = 0
    for position, claimant in enumerate(claimants):
        weight = base ** (n - 1 - position)
        options = _options(claimant, index, keep)
        work += len(states) * len(options)
        if work > MAX_WORK:
            return None
        layer = {}
        for state, ((cost, key), chain) in states.items():
            for digit, plan_index, forced in options:
                if plan_index is None:
                    if claimant.alternative is None:
                        continue
                    new, extra = state, claimant.alternative
                else:
                    step = _step(state, flat, plan_index, claimant.weights[plans[plan_index].route], forced)
                    if step is None:
                        continue
                    new, extra = step
                order = (cost + extra, key + digit * weight)
                held = layer.get(new)
                if held is None or order < held[0]:
                    layer[new] = (order, (plan_index, chain))
        if not layer or len(layer) > max_states:
            return None  # no fitting answer here, or too many room states: the greedy order answers
        states = layer
    best_state, ((cost, _), chain) = min(states.items(), key=lambda item: item[1][0])
    choices = []
    while chain is not None:
        choice, chain = chain
        choices.append(choice)
    choices.reverse()
    assigned = {claimant.job_id: (plans[choice].route if choice is not None else None)
                for claimant, choice in zip(claimants, choices)}
    return assigned, cost, _loads(plans, flat, best_state)


def _loads(plans, flat, state):
    found = {plan.route: [0] * len(plan.dims) for plan in plans}
    for (p, d, _), load in zip(flat, state):
        found[plans[p].route][d] = load
    return {route: tuple(loads) for route, loads in found.items()}


def _density(claimant, plan):
    """Saving per share of the plan's room: the other route's cost over the largest share of a dim it takes."""
    weights = claimant.weights[plan.route]
    share = max((weight / max(dim.room, 1)) for weight, dim in zip(weights, plan.dims))
    return (claimant.alternative or 0) / max(share, 1e-9)


def _greedy(claimants, plans, flat, keep):
    """Forced and kept faxes first; then candidates by saving per share of room, each on the plan where it saves
    most, while it fits a hard limit and saves more than the pages past an allowance cost."""
    index = {plan.route: p for p, plan in enumerate(plans)}
    state = tuple(0 for _ in flat)
    assigned, cost = {}, 0
    for claimant in claimants:
        route = claimant.forced or keep.get(claimant.job_id)
        if route is not None:
            state, extra = _step(state, flat, index[route], claimant.weights[route], claimant.forced is not None)
            assigned[claimant.job_id] = route
            cost += extra
    rest = [claimant for claimant in claimants if claimant.job_id not in assigned]
    ranked = sorted(rest, key=lambda item: (-max((_density(item, plans[index[route]]) for route in item.weights
                                                  if route in index), default=0), item.position, item.job_id))
    for claimant in ranked:
        best = None
        for route in claimant.weights:
            if route not in index:
                continue
            step = _step(state, flat, index[route], claimant.weights[route], False)
            if step is None:
                continue
            if claimant.alternative is not None and step[1] >= claimant.alternative:
                continue
            if best is None or step[1] < best[1][1]:
                best = (route, step)
        if best is None:
            assigned[claimant.job_id] = None
            cost += claimant.alternative
        else:
            route, (state, extra) = best
            assigned[claimant.job_id] = route
            cost += extra
    return assigned, cost, _loads(plans, flat, state)


def _upper_bound(claimants, plans):
    """An upper bound on any allocation's saving: per plan, the fractional knapsack of the candidates into its
    tightest room, plus what each could still save past an allowance; summed over plans."""
    total = 0
    for plan in plans:
        items = [claimant for claimant in claimants if claimant.forced is None and plan.route in claimant.weights]
        bounds = []
        for d, dim in enumerate(plan.dims):
            room = max(0, dim.room)
            ranked = sorted(items, key=lambda item: -(item.alternative or 0) / max(item.weights[plan.route][d], 1))
            value, left = 0, room
            for item in ranked:
                weight = max(item.weights[plan.route][d], 0)
                if weight <= left:
                    value, left = value + (item.alternative or 0), left - weight
                elif left > 0:
                    value, left = value + math.ceil((item.alternative or 0) * left / weight), 0
            if dim.overage is not None:
                value += sum(max(0, (item.alternative or 0) - dim.overage * item.weights[plan.route][d])
                             for item in items)
            bounds.append(value)
        total += min(bounds) if bounds else 0
    return total


# The reserve ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Past:
    """One earlier fax a plan could have carried: when, its units on the plan's first dim, and what it avoided."""
    at: datetime
    units: int
    value: int


@dataclass(frozen=True)
class Curve:
    """What a reserve of each size would have saved over earlier stretches as long as the time left."""
    sizes: tuple                 # reserve sizes, in units of the plan's first dim
    mean: tuple                  # micros
    lower: tuple                 # micros: the lower confidence bound, never below 0
    windows: int                 # stretches with faxes in them
    days: float                  # the length of each stretch, in days

    def lower_at(self, size):
        """The lower bound for a reserve of ``size`` units: the largest listed size not above it."""
        found = 0
        for listed, lower in zip(self.sizes, self.lower):
            if listed <= size:
                found = lower
        return found


def _best_value(items, room):
    """A feasible (never above the best) saving of ``room`` units from ``items``: greedy by saving per unit."""
    value, left = 0, room
    for item in items:
        if item.units <= left:
            value, left = value + item.value, left - item.units
    return value


def reserve_curve(history, now, renews, sizes, *, lookback=LOOKBACK, max_windows=MAX_WINDOWS,
                  min_windows=MIN_WINDOWS, z=Z):
    """The reserve's saving at each of ``sizes``, learned only from ``history`` before ``now``.

    ``renews`` is when the plan's counts start again. Stretches are laid back from ``now`` with the length of the
    time left; a stretch with no fax still counts (it saved nothing). Fewer than ``min_windows`` stretches, or none
    with a fax, gives a curve of zeros.
    """
    left = renews - now
    sizes = tuple(sorted(set(int(size) for size in sizes if size >= 0)))
    zeros = Curve(sizes, tuple(0 for _ in sizes), tuple(0 for _ in sizes), 0, left.total_seconds() / 86400)
    if left <= timedelta(0) or not sizes:
        return zeros
    count = min(max_windows, int(lookback / left))
    if count < min_windows:
        return zeros
    past = [item for item in history if item.at < now and item.value > 0 and item.units > 0]
    if not past:
        return zeros
    windows = []
    for k in range(1, count + 1):
        start, end = now - k * left, now - (k - 1) * left
        items = sorted((item for item in past if start <= item.at < end),
                       key=lambda item: (-item.value / item.units, item.at))
        windows.append(items)
    if sum(1 for items in windows if items) < min_windows:
        return zeros
    means, lowers = [], []
    for size in sizes:
        values = [_best_value(items, size) for items in windows]
        mean = sum(values) / len(values)
        spread = math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1)) if len(values) > 1 else 0
        means.append(int(mean))
        lowers.append(max(0, int(mean - z * spread / math.sqrt(len(values)))))
    return Curve(sizes, tuple(means), tuple(lowers), count, left.total_seconds() / 86400)


def reserve_sizes(room):
    """The reserve sizes tried for ``room`` units: 0, ``RESERVE_STEPS`` steps, and the whole room."""
    if room <= 0:
        return (0,)
    step = max(1, room // RESERVE_STEPS)
    return tuple(sorted(set(range(0, room + 1, step)) | {room}))


def with_reserve(plans, route, size):
    """``plans`` with ``size`` units taken from ``route``'s first dim."""
    found = []
    for plan in plans:
        if plan.route == route and size:
            first = plan.dims[0]
            plan = replace(plan, dims=(replace(first, room=first.room - size),) + tuple(plan.dims[1:]))
        found.append(plan)
    return tuple(found)


def unit_value(claimant, plan):
    """What one unit of ``plan``'s first dim saves this fax: its other route's price, capped by the plan's price past
    the allowance, per unit."""
    weight = max(1, claimant.weights[plan.route][0])
    overage = plan.dims[0].overage
    value = claimant.alternative if claimant.alternative is not None else (overage or 0) * weight
    if overage is not None:
        value = min(value, overage * weight)
    return value / weight


def choose(claimants, plans, curves, *, max_states=MAX_STATES):
    """``(solution, reserves)``: the allocation with, per plan, the reserve whose lower-bound saving most exceeds
    what the waiting faxes pay extra for it. ``curves`` maps a plan route to its ``Curve`` (or None).

    The reserve takes units only from waiting faxes that save less per unit than the reserve's lower bound does: a
    fax waiting now never gives way to a later one worth the same or less. A protected fax (urgent or with a send-by
    time) keeps the plan it has with no reserve.
    """
    plain = solve(claimants, plans, max_states=max_states)
    reserves = {plan.route: 0 for plan in plans}
    if not any(curve is not None and any(curve.lower) for curve in curves.values()):
        return plain, reserves
    protected = {claimant.job_id: plain.assigned[claimant.job_id] for claimant in claimants
                 if claimant.protected and claimant.forced is None and plain.assigned.get(claimant.job_id)}
    current = plans
    best = plain
    for plan in plans:
        curve = curves.get(plan.route)
        if curve is None or not any(curve.lower):
            continue
        forced = sum(claimant.weights[plan.route][0] for claimant in claimants if claimant.forced == plan.route)
        chosen, chosen_total, chosen_solution = 0, best.cost_micros, best
        for size in reserve_sizes(max(0, plan.dims[0].room - forced)):
            lower = curve.lower_at(size)
            if not size or not lower:
                continue
            keep = dict(protected)
            keep.update({claimant.job_id: plan.route for claimant in claimants
                         if claimant.forced is None and best.assigned.get(claimant.job_id) == plan.route
                         and unit_value(claimant, plan) >= lower / size})
            kept = sum(claimant.weights[plan.route][0] for claimant in claimants
                       if keep.get(claimant.job_id) == plan.route)
            if kept + forced + size > plan.dims[0].room:
                continue
            trial = solve(claimants, with_reserve(current, plan.route, size), max_states=max_states, keep=keep)
            total = trial.cost_micros - lower
            if total < chosen_total:
                chosen, chosen_total, chosen_solution = size, total, trial
        if chosen:
            reserves[plan.route] = chosen
            current = with_reserve(current, plan.route, chosen)
            best = chosen_solution
    return best, reserves


def forced_cost(claimants, plans, job_id, route, *, max_states=MAX_STATES, keep=None):
    """The least cost with ``job_id`` given ``route`` anyway: what giving it the plan would cost in all."""
    claimants = [replace(claimant, forced=route) if claimant.job_id == job_id else claimant
                 for claimant in claimants]
    return solve(claimants, plans, max_states=max_states, keep=keep).cost_micros


# Plans with scarce room ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Scarce:
    """One plan whose room is worth allocating: its budget this period and its room after faxes on their way."""
    key: str
    left: object                 # plan_budget.BudgetLeft
    room: PlanRoom               # room after ``in_flight``
    in_flight: tuple             # units of faxes already on their way, on each dim (attempts not finished)

    @property
    def unit(self):
        return self.room.dims[0].name

    @property
    def currency(self):
        return self.left.budget.currency


def plan_dims(left):
    """The plan's limited counts as ``Dim``s, or () when nothing is worth allocating (no limit, or a commitment)."""
    budget = left.budget
    dims = []
    if budget.included_pages:
        dims.append(Dim('pages', max(0, left.allowance_left), budget.page_overage_micros))
        if budget.faxes is not None:
            dims.append(Dim('faxes', max(0, left.faxes_left), None))
    elif budget.limited:
        if budget.pages is not None:
            dims.append(Dim('pages', max(0, left.pages_left), None))
        if budget.faxes is not None:
            dims.append(Dim('faxes', max(0, left.faxes_left), None))
    elif budget.included_minutes and left.minutes_left is not None:
        dims.append(Dim('minutes', max(0, left.minutes_left), budget.per_minute_micros or None))
    return tuple(dims)


def _sending_keys(values):
    from ..accounts import all_accounts
    return [account.key for account in all_accounts(values) if account.sends]


def scarce_plans(routes, values, now):
    """``[Scarce]`` for each sending account with an allowance, a normal-use budget or included minutes."""
    from .plan_budget import budget_left
    found = []
    for key in _sending_keys(values):
        left = budget_left(key, now, engine=routes.engine, values=values)
        if left is None or left.state == 'no_limit':
            continue
        dims = plan_dims(left)
        if not dims:
            continue
        units, faxes = _in_flight(routes, values, key, left, dims, now)
        held = tuple(faxes if dim.name == 'faxes' else units for dim in dims)
        room = PlanRoom(key, tuple(replace(dim, room=dim.room - used) for dim, used in zip(dims, held)))
        found.append(Scarce(key, left, room, held))
    return found


# Reading the queue ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Waiting:
    job_id: str
    to_number: str
    pages: int
    urgent: bool
    send_by: datetime | None
    created_at: datetime
    backend: str | None
    alternate: str | None
    position: int = 0
    by_call: bool = False


_REFLECTED = {}
_LOCK = threading.Lock()


def _tables(engine):
    from .database import reflect
    with _LOCK:
        found = _REFLECTED.get(id(engine))
        if found is None or found[0] is not engine:
            names = ('outbound_deliveries', 'fax_jobs', 'delivery_attempt_costs')
            found = (engine, reflect(engine, names))
            _REFLECTED[id(engine)] = found
    return found[1]


def waiting(engine, *, include=None, limit=MAX_WAITING, now=None):
    """Faxes ready to send, and claimed faxes with no route chosen yet, about in the order the claim offers them
    (urgent first, then the nearest send-by time, then the oldest). A fax its sending rules hold (approval, a time
    window not open yet, no allowed route) is left out, as the claim leaves it out. ``include`` is always read."""
    import sqlalchemy as sa
    from . import envelope as envelopes
    from .database import read_connection, utcnow
    from .holds import blocking
    t = _tables(engine)
    d, j, c = t['outbound_deliveries'], t['fax_jobs'], t['delivery_attempt_costs']
    urgent = sa.func.coalesce(j.c.urgent, 0) if 'urgent' in j.c else sa.literal(0)
    send_by = j.c.send_by if 'send_by' in j.c else sa.null()
    alternate = d.c.alternate_number if 'alternate_number' in d.c else sa.null()
    by_call = sa.func.coalesce(j.c.send_by_call, 0) if 'send_by_call' in j.c else sa.literal(0)
    columns = (d.c.id, j.c.to_number, j.c.pages, urgent.label('urgent'), send_by.label('send_by'), j.c.created_at,
               j.c.backend, alternate.label('alternate'), by_call.label('by_call'))
    undecided = sa.and_(d.c.state == 'preparing', c.c.id.is_(None))
    base = (sa.select(*columns).select_from(d.join(j, j.c.id == d.c.id).outerjoin(c, c.c.id == d.c.attempt_id))
            .where(sa.or_(sa.and_(d.c.state == 'ready', d.c.dispatch_mode == 'normal'), undecided)))
    order = [urgent.desc(), sa.case((send_by.is_(None), 1), else_=0), send_by, j.c.created_at, d.c.id]
    with read_connection(engine) as connection:
        rules = envelopes.tables(connection)
        if rules is not None:
            base = base.where(d.c.id.not_in(blocking(rules, now or utcnow())))
        rows = list(connection.execute(base.order_by(*order).limit(limit)).all())
        if include and include not in {row.id for row in rows}:
            rows += connection.execute(base.where(d.c.id == include)).all()
    return [Waiting(row.id, row.to_number, max(1, int(row.pages or 1)), bool(row.urgent), row.send_by,
                    row.created_at, row.backend, row.alternate, position, bool(row.by_call))
            for position, row in enumerate(rows)]


def _in_flight(routes, values, key, left, dims, now):
    """``(units, faxes)`` of attempts on ``key`` this period whose outcome is not known yet: they hold room that
    ``plan_budget`` counts only once they finish."""
    import sqlalchemy as sa
    from .database import read_connection
    from .plan import ledger_key
    t = _tables(routes.engine)
    c, j = t['delivery_attempt_costs'], t['fax_jobs']
    # The pages each attempt really sends: its recorded selection (routing/selections.py, accounts compared on their
    # measured pages), else its page change (long or encoded pages), else the document's own pages.
    sent = _sent_pages(routes.engine)
    source = c.join(j, j.c.id == c.c.job_id)
    pages = j.c.pages
    if sent:
        for table in sent:
            source = source.outerjoin(table, table.c.attempt_id == c.c.id)
        pages = sa.func.coalesce(*(table.c.sent_pages for table in sent), j.c.pages)
    with read_connection(routes.engine) as connection:
        rows = connection.execute(sa.select(c.c.destination, pages.label('pages')).select_from(source)
                                  .where(c.c.route == ledger_key(key), c.c.outcome == 'pending',
                                         c.c.created_at >= left.period.start)).all()
    units = sum(_units(routes, values, key, left.budget, dims[0].name, row.destination, max(1, int(row.pages or 1)),
                       now) for row in rows)
    return units, len(rows)


def _sent_pages(engine):
    """The tables that say how many pages an attempt sent, newest kind first, each with ``attempt_id`` and
    ``sent_pages``; those missing (an older database) are left out."""
    from .database import DeliveryStoreError, reflect
    found = []
    for name in ('fax_route_selections', 'fax_page_changes'):
        try:
            found.append(reflect(engine, (name,))[name])
        except DeliveryStoreError:
            continue
    return found


_SECONDS = {}


def _seconds(routes, values, key, number, pages, now):
    """The predicted time on the line for ``pages`` pages to ``number`` on ``key`` (shared predictor), or None."""
    from .predict import Shape, predict_from
    from .predict_facts import facts_for
    memo = (id(routes.engine), key, number, pages, int(now.timestamp()) // FEATURES_SECONDS)
    found = _SECONDS.get(memo, _SECONDS)
    if found is _SECONDS:
        if len(_SECONDS) > 5000:
            _SECONDS.clear()
        facts = facts_for(key, number, now=now, engine=routes.engine, values=values)
        found = predict_from(facts, Shape(pages, None, 'standard', 'normal')).seconds
        _SECONDS[memo] = found
    return found


def carries(routes, values, key, number, pages, now):
    """Whether plan ``key`` can carry ``pages`` pages to ``number``: it prices that kind of number, does not refuse
    it, and takes that many pages in one fax (the shared predictor's facts)."""
    from .predict_facts import facts_for
    facts = facts_for(key, number, now=now, engine=routes.engine, values=values)
    terms = facts.terms
    return not (terms is None or facts.refused or (terms.max_pages_per_fax and pages > terms.max_pages_per_fax))


def _units(routes, values, key, budget, unit, number, pages, now):
    """A fax's units on the plan's first dim: pages (the greater of pages and started periods on the line for a
    plan that counts both, such as HumbleFax), or started minutes for a minute allowance."""
    if unit == 'minutes':
        seconds = _seconds(routes, values, key, number, pages, now)
        return math.ceil(seconds / 60) if seconds is not None else pages
    if budget.page_time_seconds:
        from .costs import greater_of_pages
        seconds = _seconds(routes, values, key, number, pages, now)
        counted = greater_of_pages(pages, seconds, budget.page_time_seconds)
        return counted if counted is not None else pages
    return pages


def _weights(routes, values, scarce, fax, now, pages=None):
    """A fax's weight on one plan; ``pages``: the pages it would really send on that plan's account (its measured
    best pages, ``routing.joint``), else the document's own."""
    units = _units(routes, values, scarce.key, scarce.left.budget, scarce.unit, fax.to_number,
                   pages if pages else fax.pages, now)
    return tuple(1 if dim.name == 'faxes' else units for dim in scarce.room.dims)


# What each waiting fax would do -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Feature:
    """How one waiting fax relates to the scarce plans: ``group``, the plans it competes for, its other route."""
    group: str                   # 'never', 'forced' or 'candidate'
    plans: tuple = ()            # plan keys it would take if they had room, in the planner's order
    alternative: int | None = None
    alternative_key: str | None = None


_FEATURES = {}


def _free(prices, keys, currency_of):
    """``prices`` with each plan in ``keys`` priced as if it had room: what it would add is nothing."""
    from .pricing import Price
    found = dict(prices or {})
    for key in keys:
        if key in found:
            found[key] = Price(key, 0, currency_of(key), in_plan=True, uses_budget=True)
    return found


def feature(routes, values, fax, plans, now, *, dial=None):
    """What ``fax`` would do, asked of the real planner twice: with each scarce plan free (does it take one?), and
    without them (its best other route it may use now, with today's prices)."""
    from . import envelope as envelopes
    from ..accounts import default_sending_key
    from .plan import RoutePlanner, ledger_key
    from .pricing import prices_for
    keys = tuple(plan.key for plan in plans)
    memo = (id(routes.engine), fax.job_id, keys, fax.alternate, int(now.timestamp()) // FEATURES_SECONDS)
    cached = _FEATURES.get(memo) if dial is None else None
    if cached is not None:
        return cached
    # A plan that cannot carry this fax (a kind of number it does not call, more pages than it takes) is no plan
    # for it: it is neither given its room nor freed of its price.
    plans = [plan for plan in plans if carries(routes, values, plan.key, fax.to_number, fax.pages, now)]
    keys = tuple(plan.key for plan in plans)
    if not keys:
        return Feature('never')
    try:
        pinned = envelopes.load(routes.engine, fax.job_id)
    except envelopes.UnreadableDecision:
        return Feature('never')  # dispatch gives such a fax no route at all
    if pinned is not None:
        bound = default_sending_key(values) or fax.backend
    else:
        bound = fax.backend or default_sending_key(values)
    dial = dial if dial is not None else {'alternate': fax.alternate, 'approval': None, 'refused': False}
    planner = RoutePlanner(routes, direct_ready=lambda: bool(getattr(values, 'direct_delivery_enabled', False)),
                           local_ready=lambda: bool(getattr(values, 'local_delivery_enabled', False)))
    currency = {plan.key: plan.currency for plan in plans}
    base = None
    if pinned is not None:
        base = prices_for(routes, values, fax.to_number, fax.pages, pinned=pinned, bound=bound, dial=dial, now=now)
    kwargs = dict(to_number=fax.to_number, bound=bound, values=values, pages=fax.pages, alternates=True, dial=dial,
                  tried=planner.tried(fax.job_id), pinned=pinned, current=values if pinned is not None else None,
                  job_id=fax.job_id, now=now, by_call=fax.by_call)
    free = planner.plan(prices=_free(base, keys, currency.get) if base is not None else None, **kwargs)
    competing = []
    for choice in free.choices:
        if choice.route.key not in keys:
            break
        competing.append(choice.route.key)
    if not competing:
        found = Feature('never')
    elif pinned is None or pinned.envelope.mode in ('one', 'ordered') or free.first.reason == 'preferred':
        # Ranked by the rate card, in a fixed order, or by the number's preferred route: the plan's price does
        # not decide, so the fax takes the plan whatever it costs.
        found = Feature('forced', (competing[0],))
    else:
        other = planner.plan(prices=base, exclude=keys, **kwargs)
        first = other.first if other.choices else None
        price = (base or {}).get(first.route.key) if first is not None else None
        same = price is None or price.currency in (None, currency[competing[0]])
        if first is None or ledger_key(first.route.key) in keys or first.route.key in keys or \
                first.estimated_cost_micros is None or not same:
            # No other route it may use has a known price in the plan's currency: unknown is never 0.
            found = Feature('forced', (competing[0],))
        else:
            found = Feature('candidate', tuple(competing), first.estimated_cost_micros, first.route.key)
    if len(_FEATURES) > 5000:
        _FEATURES.clear()
    _FEATURES[memo] = found
    return found


# What earlier faxes show ------------------------------------------------------------------------------------

_CURVES = {}


def history(routes, values, scarce, others, now):
    """``[Past]`` for ``scarce``'s plan from the faxes before ``now``. A fax sent another way is worth what its first
    attempt was estimated to cost there; a fax the plan carried is worth its cheapest other account's price today.
    Either is capped by the plan's price past its allowance, and a fax the plan cannot carry (a kind of number it
    does not call, more pages than it takes) is left out. Each fax the plan received is worth that price past the
    allowance: received faxes use it whatever happens."""
    import sqlalchemy as sa
    from .database import read_connection
    from .plan import ledger_key
    from .plan_budget import records
    from .predict_facts import facts_for
    from .pricing import prices_for
    t = _tables(routes.engine)
    c, j = t['delivery_attempt_costs'], t['fax_jobs']
    start = now - LOOKBACK
    budget = scarce.left.budget
    overage = scarce.room.dims[0].overage
    first = (sa.select(c.c.job_id, sa.func.min(c.c.created_at).label('at'))
             .where(c.c.created_at >= start, c.c.created_at < now, c.c.outcome != 'pending')
             .group_by(c.c.job_id).subquery())
    with read_connection(routes.engine) as connection:
        rows = connection.execute(
            sa.select(first.c.at, j.c.to_number, j.c.pages, c.c.route, c.c.estimated_cost_micros, c.c.currency)
            .select_from(first.join(c, sa.and_(c.c.job_id == first.c.job_id, c.c.created_at == first.c.at))
                         .join(j, j.c.id == first.c.job_id))
            .where(c.c.route.not_in(('local', 'direct')), sa.not_(c.c.route.like('relay.%')))
            .order_by(first.c.at.desc()).limit(HISTORY_READ)).all()
    carried, priced, found = {}, {}, []
    for row in rows:
        pages = max(1, int(row.pages or 1))
        if row.to_number not in carried:
            facts = facts_for(scarce.key, row.to_number, now=now, engine=routes.engine, values=values)
            carried[row.to_number] = None if facts.terms is None or facts.refused else facts.terms
        terms = carried[row.to_number]
        if terms is None or (terms.max_pages_per_fax and pages > terms.max_pages_per_fax):
            continue
        units = _units(routes, values, scarce.key, budget, scarce.unit, row.to_number, pages, now)
        if row.route != ledger_key(scarce.key) and row.estimated_cost_micros is not None \
                and row.currency == scarce.currency:
            value = int(row.estimated_cost_micros)
        else:
            key = (row.to_number, pages)
            if key not in priced:
                known = [price.micros for price in prices_for(routes, values, row.to_number, pages, keys=list(others),
                                                              now=now).values()
                         if price.micros is not None and price.currency == scarce.currency]
                priced[key] = min(known) if known else None
            value = priced[key]
        if overage is not None:
            value = overage * units if value is None else min(value, overage * units)
        if value is not None:
            found.append(Past(row.at, units, value))
    if overage is not None and scarce.unit == 'pages':
        for at, direction, pages, _ in records(routes.engine, scarce.key, start, now,
                                               page_time_seconds=budget.page_time_seconds):
            if direction == 'received':
                found.append(Past(at, pages, overage * pages))
    return found


def curve_for(routes, values, scarce, others, now):
    """The reserve curve for ``scarce``'s plan, worked out at most once an hour."""
    room = max(0, scarce.room.dims[0].room)
    memo = (id(routes.engine), scarce.key, room, scarce.left.period.end, int(now.timestamp()) // RESERVE_SECONDS)
    found = _CURVES.get(memo)
    if found is None:
        if len(_CURVES) > 200:
            _CURVES.clear()
        found = reserve_curve(history(routes, values, scarce, others, now), now, scarce.left.period.end,
                              reserve_sizes(room))
        _CURVES[memo] = found
    return found


# The allocation ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Hold:
    """What part of a plan's room one fax may not use, and why (``kind``: 'queue', 'reserve' or 'both')."""
    route: str
    units: tuple                 # held on each of the plan's dims: on their way, given to others, kept in reserve
    names: tuple                 # the dims' names ('pages', 'faxes', 'minutes')
    room: int                    # the plan's room on its first dim after faxes on their way
    given: bool                  # this fax was given the plan
    others: int                  # other waiting faxes given the plan
    reserve: int                 # units kept for faxes not yet queued
    kind: str
    saving_micros: int | None = None   # what giving this fax the plan instead would cost in all (estimate)
    currency: str = 'USD'
    renews: object = None        # the day the plan's counts start again
    label: str = ''


@dataclass(frozen=True)
class Allocation:
    plans: tuple = ()            # of Scarce
    claimants: tuple = ()        # of Claimant: faxes forced onto a plan, and candidates
    waiting: tuple = ()          # of Waiting
    features: dict = field(default_factory=dict)
    solution: Solution | None = None
    reserves: dict = field(default_factory=dict)
    curves: dict = field(default_factory=dict)
    first_come_micros: int | None = None

    def plan(self, key):
        return next((plan for plan in self.plans if plan.key == key), None)

    def rooms(self, reserves=None):
        reserves = self.reserves if reserves is None else reserves
        rooms = tuple(plan.room for plan in self.plans)
        for key, size in reserves.items():
            rooms = with_reserve(rooms, key, size)
        return rooms

    def given(self, key):
        if self.solution is None:
            return ()
        return tuple(claimant for claimant in self.claimants if self.solution.assigned.get(claimant.job_id) == key)

    def lower(self, reserves):
        return sum(self.curves[key].lower_at(size) for key, size in reserves.items() if size and key in self.curves)

    def saving(self, job_id, key):
        """Net, in micros: what giving ``job_id`` the plan ``key`` would cost in all more than this allocation."""
        if self.solution is None:
            return None
        keep = {claimant.job_id: self.solution.assigned[claimant.job_id] for claimant in self.claimants
                if claimant.protected and claimant.forced is None and self.solution.assigned.get(claimant.job_id)
                and claimant.job_id != job_id}
        chosen = self.solution.cost_micros - self.lower(self.reserves)
        kept = forced_cost(self.claimants, self.rooms(), job_id, key, keep=keep) - self.lower(self.reserves)
        dropped = {**self.reserves, key: 0}
        released = forced_cost(self.claimants, self.rooms(dropped), job_id, key, keep=keep) - self.lower(dropped)
        return max(0, min(kept, released) - chosen)

    def hold(self, job_id, key):
        """The ``Hold`` for one fax on plan ``key``; None when it may use the whole room as today."""
        plan = self.plan(key)
        claimant = next((item for item in self.claimants if item.job_id == job_id), None)
        if plan is None or claimant is None or claimant.forced is not None or key not in claimant.weights \
                or self.solution is None:
            return None
        others = [item for item in self.given(key) if item.job_id != job_id]
        reserve = self.reserves.get(key, 0)
        units = tuple(held + sum(item.weights[key][d] for item in others) + (reserve if d == 0 else 0)
                      for d, held in enumerate(plan.in_flight))
        if not any(units):
            return None
        given = self.solution.assigned.get(job_id) == key
        # Faxes that take the plan whatever it costs fill it like faxes on their way: only the room left after
        # them was shared, and only the faxes chosen for it count as "faxes they save more on".
        chosen = [item for item in others if item.forced is None]
        room = max(0, plan.room.dims[0].room - sum(item.weights[key][0] for item in others if item.forced))
        kind = 'reserve' if reserve and not chosen else 'both' if reserve else 'queue'
        return Hold(key, units, tuple(dim.name for dim in plan.room.dims), room, given, len(chosen), reserve, kind,
                    None if given else self.saving(job_id, key), plan.currency, plan.left.period.next_day,
                    plan.left.budget.label)


EMPTY = Allocation()


def reserve_allowed(plan):
    """Whether a plan may keep a reserve: an allowance (published or yours), or a normal-use budget you set. Faxbot's
    own cautious start for an unlimited plan is no promise from the plan, so no money is spent keeping it."""
    budget = plan.left.budget
    return bool(budget.included_pages or budget.included_minutes or budget.source == 'set')


def allocate(routes, values, now=None, *, include=None, dial=None, sent=None):
    """The allocation of every scarce plan's room among the faxes waiting now (``Allocation``).

    ``include`` is the fax being planned (read even past ``MAX_WAITING``); ``dial`` its kept number choice; ``sent``
    ``{plan key: pages}`` the pages it would really send on each plan's account once measured (brief 84, M4), so it
    takes the plan's room it would really use.
    """
    from .database import utcnow
    now = (now or utcnow()).replace(tzinfo=None, microsecond=0)
    plans = scarce_plans(routes, values, now)
    if not plans:
        return EMPTY
    keys = [plan.key for plan in plans]
    others = [key for key in _sending_keys(values) if key not in keys]
    curves = {plan.key: curve_for(routes, values, plan, others, now) for plan in plans if reserve_allowed(plan)}
    queue = waiting(routes.engine, include=include, now=now)
    reserve_possible = any(any(curve.lower) for curve in curves.values())
    if not reserve_possible and _fits(routes, values, plans, queue, now, include=include, sent=sent):
        # Every waiting fax fits in the room left: nothing to allocate, and each is priced exactly as before.
        return Allocation(tuple(plans), (), tuple(queue), {}, None, {}, curves)
    claimants, features = [], {}
    for fax in queue:
        found = feature(routes, values, fax, plans, now, dial=dial if fax.job_id == include else None)
        features[fax.job_id] = found
        if found.group == 'never':
            continue
        weights = {key: _weights(routes, values, next(plan for plan in plans if plan.key == key), fax, now,
                                 pages=(sent or {}).get(key) if fax.job_id == include else None)
                   for key in found.plans}
        claimants.append(Claimant(fax.job_id, fax.position, weights, alternative=found.alternative,
                                  forced=found.plans[0] if found.group == 'forced' else None,
                                  protected=fax.urgent or fax.send_by is not None))
    rooms = tuple(plan.room for plan in plans)
    solution, reserves = choose(claimants, rooms, curves)
    return Allocation(tuple(plans), tuple(claimants), tuple(queue), features, solution, reserves, curves,
                      in_turn(claimants, rooms))


def _fits(routes, values, plans, queue, now, *, include=None, sent=None):
    """Whether every waiting fax fits in every plan's room at once, counted generously (no rules read)."""
    for plan in plans:
        totals = [0] * len(plan.room.dims)
        for fax in queue:
            for d, weight in enumerate(_weights(routes, values, plan, fax, now, pages=(sent or {}).get(
                    plan.key) if fax.job_id == include else None)):
                totals[d] += weight
        if any(total > dim.room for total, dim in zip(totals, plan.room.dims)):
            return False
    return True


def in_turn(claimants, plans):
    """What the waiting faxes would pay taking the plan in turn (first come, first served), for the screen: each fax,
    in the claim's order, takes a plan while what the plan adds for it is below its other route's price."""
    plans = tuple(plans)
    flat = _dims(plans)
    index = {plan.route: p for p, plan in enumerate(plans)}
    state, cost = tuple(0 for _ in flat), 0
    for claimant in sorted(claimants, key=lambda item: (item.position, item.job_id)):
        best = None
        for route in ([claimant.forced] if claimant.forced else claimant.weights):
            step = _step(state, flat, index[route], claimant.weights[route], claimant.forced is not None)
            if step is None or (claimant.forced is None and step[1] >= claimant.alternative):
                continue
            if best is None or step[1] < best[1]:
                best = step
        if best is None:
            cost += claimant.alternative
        else:
            state, extra = best
            cost += extra
    return cost


def hold_for(routes, values, job_id, keys, *, now=None, dial=None, sent=None):
    """``{plan key: Hold}`` for one fax among ``keys`` (the accounts it is priced on); empty when nothing is held.

    Storage problems are logged and leave the fax priced as before (``DeliveryStoreError``); anything else raises.
    """
    from .database import DeliveryStoreError
    try:
        found = allocate(routes, values, now, include=job_id, dial=dial, sent=sent)
    except DeliveryStoreError:
        logging.getLogger(__name__).warning('The plan allocation could not be read; this fax is priced as before.',
                                            exc_info=True)
        return {}
    holds = {}
    for key in keys:
        hold = found.hold(job_id, key)
        if hold is not None:
            holds[key] = hold
    return holds


def after_hold(left, hold):
    """The plan's ``BudgetLeft`` as one fax sees it: the held units count as already used."""
    from .plan_budget import _left
    if hold is None or left is None:
        return left
    held = dict(zip(hold.names, hold.units))
    used = left.used
    used = replace(used, sent_pages=used.sent_pages + held.get('pages', 0),
                   sent_faxes=used.sent_faxes + held.get('faxes', 0),
                   minutes=(used.minutes + held['minutes']) if 'minutes' in held and used.minutes is not None
                   else used.minutes)
    return _left(left.budget, left.period, used)


# Sentences ------------------------------------------------------------------------------------------------------

def _units_text(count, unit, label):
    if unit == 'minutes':
        return f'your last included {label} minute' if count == 1 else f'your last {count:,} included {label} minutes'
    return f'your last {label} page' if count == 1 else f'your last {count:,} {label} pages'


def _day(day):
    return f'{day.day} {day:%B}' if day is not None else 'the plan starts again'


def reason_sentence(*, route_label, cost_micros, currency, plan_label, kind, room, unit, others, saving_micros,
                    renews):
    """Why a fax went another way than its plan, with the amounts kept when it was sent; every amount an estimate."""
    from .delivered import short_money_text
    cost = f' for about {short_money_text(cost_micros, currency)}' if cost_micros is not None else ''
    pages = _units_text(room, unit, plan_label)
    verb = 'goes' if room == 1 else 'go'
    faxes = 'the waiting fax' if others == 1 else f'{others:,} waiting faxes'
    stay = 'stays' if room == 1 else 'stay'
    if kind == 'reserve':
        where = (f'{pages} this month {stay} free for faxes like the ones you usually send before '
                 f'{_day(renews)}')
    elif kind == 'both':
        where = (f'{pages} this month {verb} to {faxes} they save more on and to faxes like the ones you usually '
                 f'send before {_day(renews)}')
    else:
        where = f'{pages} this month {verb} to {faxes} they save more on'
    saving = (f', saving about {short_money_text(saving_micros, currency)} in all' if saving_micros else '')
    return f'Sent by {route_label}{cost} so {where}{saving} (estimate).'


# The record kept with a sent fax ------------------------------------------------------------------------------

EVENT = 'plan_allocation'


def _events():
    import sqlalchemy as sa
    return sa.table('outbound_events', sa.column('id'), sa.column('job_id'), sa.column('attempt_id'),
                    sa.column('kind'), sa.column('dedupe_key'), sa.column('details'), sa.column('created_at'))


def record(engine, *, attempt_id, job_id, held, now=None):
    """Keep why this attempt did not use its plan, with the amounts as they were (``RoutePlan.held``), in the fax's
    own event history. Written once per attempt; history is never changed."""
    import sqlalchemy as sa
    from .database import utcnow, write_transaction
    from ..outbound_store import _event
    hold, route, cost = held
    details = {'plan': hold.route, 'plan_label': hold.label, 'route': route, 'kind': hold.kind, 'room': hold.room,
               'unit': hold.names[0], 'others': hold.others, 'reserve': hold.reserve, 'cost_micros': cost,
               'saving_micros': hold.saving_micros, 'currency': hold.currency,
               'renews': hold.renews.isoformat() if hold.renews is not None else None}
    events = _events()
    dedupe = f'{EVENT}:{attempt_id}'[:64]
    with write_transaction(engine) as connection:
        if connection.execute(sa.select(events.c.id).where(events.c.job_id == job_id,
                                                           events.c.dedupe_key == dedupe)).first():
            return False
        _event(connection, events, job_id, EVENT, now or utcnow(), attempt_id=attempt_id, details=details,
               dedupe_key=dedupe)
    return True


def explanation(engine, job_id):
    """The sentence for Sent details when the fax's latest attempt went another way than its plan, else None."""
    import json
    import sqlalchemy as sa
    from datetime import date
    from .database import read_connection
    from .plan import route_label
    t = _tables(engine)
    costs, events = t['delivery_attempt_costs'], _events()
    with read_connection(engine) as connection:
        latest = connection.execute(sa.select(costs.c.id).where(costs.c.job_id == job_id)
                                    .order_by(costs.c.created_at.desc(), costs.c.id.desc()).limit(1)).scalar()
        if latest is None:
            return None
        found = connection.execute(sa.select(events.c.details).where(
            events.c.job_id == job_id, events.c.kind == EVENT, events.c.attempt_id == latest)).scalar()
    if found is None:
        return None
    try:
        details = json.loads(found)
        renews = date.fromisoformat(details['renews']) if details.get('renews') else None
        return reason_sentence(route_label=route_label(details['route']), cost_micros=details.get('cost_micros'),
                               currency=details.get('currency') or 'USD', plan_label=details['plan_label'],
                               kind=details['kind'], room=int(details['room']), unit=details['unit'],
                               others=int(details.get('others') or 0), saving_micros=details.get('saving_micros'),
                               renews=renews)
    except (TypeError, ValueError, KeyError):
        return None


# The screen and the command line ---------------------------------------------------------------------------------

def _money(micros, currency):
    from .costs import format_amount
    return [] if micros is None else [{'currency': currency, 'amount': format_amount(micros)}]


def _plural(count, one, many=None):
    return f"{count:,} {one if count == 1 else (many or one + 's')}"


def _unit_word(unit, count):
    return {'pages': 'page', 'minutes': 'included minute', 'faxes': 'fax'}[unit] + ('' if count == 1 else (
        'es' if unit == 'faxes' else 's'))


def _reserve_sentence(plan, size, curve):
    from .delivered import short_money_text
    renews = _day(plan.left.period.next_day)
    if not reserve_allowed(plan):
        return ("Faxbot keeps nothing back for faxes not sent yet, because this is Faxbot's starting budget for "
                f'{plan.left.budget.label}, not one you set.')
    if size:
        days = max(1, round(curve.days))
        return (f'Faxbot keeps {size:,} {_unit_word(plan.unit, size)} for faxes like the ones you usually send before '
                f'{renews}: in {curve.windows} earlier stretches of {_plural(days, "day")}, faxes like them would have '
                f'saved at least about {short_money_text(curve.lower_at(size), plan.currency)} with them (estimate).')
    if curve is None or curve.windows < MIN_WINDOWS or not any(curve.mean):
        return (f'Faxbot keeps nothing back: your earlier faxes do not show dearer ones coming before {renews}.')
    return ('Faxbot keeps nothing back: your earlier faxes show that keeping some for later would not save more than '
            'the waiting faxes would pay for it.')


def view(routes, values, now=None):
    """Each scarce plan's room, who gets it, and what is kept for later: Costs → Prices & plans, and
    ``faxbot costs plans allocation``. Every amount is an estimate."""
    from .database import utcnow
    from .delivered import short_money_text
    from .plan import route_label
    now = (now or utcnow()).replace(tzinfo=None, microsecond=0)
    found = allocate(routes, values, now)
    plans = []
    by_job = {fax.job_id: fax for fax in found.waiting}
    for plan in found.plans:
        label, unit, currency = plan.left.budget.label, plan.unit, plan.currency
        room = max(0, plan.room.dims[0].room)
        renews = _day(plan.left.period.next_day)
        reserve = found.reserves.get(plan.key, 0)
        faxes = []
        given_units = 0
        for claimant in found.claimants:
            if plan.key not in claimant.weights:
                continue
            fax = by_job[claimant.job_id]
            units = claimant.weights[plan.key][0]
            assigned = found.solution.assigned.get(claimant.job_id) if found.solution is not None else None
            feature_ = found.features.get(claimant.job_id)
            other = feature_.alternative_key if feature_ is not None else None
            if claimant.forced == plan.key:
                outcome = 'forced'
                sentence = ('Takes the plan whatever it costs: its sending rules put the plan first, or no other '
                            'route it may use has a known price.')
            elif assigned == plan.key:
                outcome = 'plan'
                sentence = (f'Gets {_plural(units, _unit_word(unit, 1))} of the plan; by '
                            f'{route_label(other)} it would cost about '
                            f'{short_money_text(claimant.alternative, currency)}.')
            elif assigned is not None:
                continue  # it got another plan; shown there
            else:
                outcome = 'other'
                sentence = (f'Goes by {route_label(other)} for about '
                            f"{short_money_text(claimant.alternative, currency)}, so the plan's {unit} go where they "
                            'save more.')
            if outcome in ('plan', 'forced'):
                given_units += units
            faxes.append({'job_id': claimant.job_id, 'to': fax.to_number, 'pages': fax.pages, 'units': units,
                          'queued_at': fax.created_at.isoformat(), 'urgent': fax.urgent,
                          'send_by': fax.send_by.isoformat() if fax.send_by is not None else None,
                          'outcome': outcome, 'route': other if outcome == 'other' else plan.key,
                          'route_label': route_label(other) if outcome == 'other' else label,
                          'cost': _money(claimant.alternative if outcome == 'other' else None, currency),
                          'sentence': sentence})
        left_words = f'{room:,} {"included " if unit == "pages" and plan.left.budget.included_pages else ""}' \
                     f'{_unit_word(unit, room)}'
        if not found.waiting and not reserve:
            sentence = f'{label} has {left_words} left until {renews}, and no fax is waiting.'
        elif found.solution is None and not reserve:
            sentence = (f'{label} has {left_words} left until {renews}, and every waiting fax that could use them '
                        'fits.')
        elif not faxes and not reserve:
            sentence = f'{label} has {left_words} left until {renews}, and no waiting fax needs them.'
        else:
            parts = []
            if given_units:
                count = sum(1 for item in faxes if item['outcome'] in ('plan', 'forced'))
                parts.append(f'{given_units:,} go to {_plural(count, "waiting fax", "waiting faxes")}')
            if reserve:
                parts.append(f'{reserve:,} are kept for faxes not sent yet')
            sentence = (f'{label} has {left_words} left until {renews}' + (': ' + ' and '.join(parts) if parts
                                                                          else '') + '.')
        saving = None
        if found.solution is not None and found.first_come_micros is not None:
            difference = found.first_come_micros - found.solution.cost_micros
            saving = difference if difference > 0 else None
        bound = None
        if found.solution is not None and not found.solution.exact and found.solution.bound_micros is not None:
            gap = max(0, found.solution.bound_micros - found.solution.saving_micros)
            bound = (f'With this many waiting faxes Faxbot shares the {unit} by what each saves per {unit[:-1]}; '
                     f'the best possible sharing would save at most about {short_money_text(gap, currency)} more.')
        plans.append({
            'route': plan.key, 'name': label, 'unit': unit, 'room': room, 'on_their_way': plan.in_flight[0],
            'renews_on': plan.left.period.next_day.isoformat(), 'reserve': reserve,
            'reserve_sentence': _reserve_sentence(plan, reserve, found.curves.get(plan.key)),
            'sentence': sentence, 'left_sentence': plan.left.sentence,
            'saving': _money(saving, currency),
            'saving_sentence': (f'Sharing the {unit} this way saves about {short_money_text(saving, currency)} against '
                                'giving them to the waiting faxes in turn (estimate).') if saving else None,
            'bound_sentence': bound, 'faxes': faxes})
    return {'plans': plans, 'estimate': True,
            'empty_sentence': None if plans else ('None of your plans has a limited allowance or a normal-use budget '
                                                  'this month, so there is nothing to share out.')}
