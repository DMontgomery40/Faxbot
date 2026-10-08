"""Scarce plan pages go to the waiting faxes where they avoid the most cost, with a reserve for later ones.

The pure solver is checked against every possible assignment on small synthetic
queues, and against the per-fax policy that runs today (``plan_budget.marginal``
and ``order_key`` in arrival order, the real functions). The database half runs
on SQLite and PostgreSQL with the shared predictor. All numbers, prices and
faxes are synthetic; expected money is worked out here, never read back from
the code under test.
"""
from datetime import date, datetime, timedelta
import itertools
import random

import pytest

from api.app.routing import envelope as envelopes, plan_allocation as allocation
from api.app.routing.costs import Money
from api.app.routing.plan_allocation import Claimant, Curve, Dim, Past, PlanRoom, choose, reserve_curve, solve
from api.app.routing.plan_budget import Budget, Period, Usage, _left, marginal, order_key
from api.app.routing.predict import Prediction
from api.tests.test_routing_http import ADMIN, client, scoped_key  # noqa: F401 (fixture)
from api.tests.test_rules_delivery import LISTED, Inner, accept, installation, publish, rule
from api.tests.test_schema import database  # noqa: F401 (fixture)


CENT = 10_000            # micros
DOLLAR = 1_000_000


# Exhaustive checks ---------------------------------------------------------------------------------------

def brute(claimants, plans):
    """The least total over every assignment, worked out directly: each fax on its other route or on a plan it may
    use, in arrival order; a hard limit refuses a candidate that would pass it, pages past an allowance cost its
    price. Forced faxes take their plan whatever it costs."""
    claimants = allocation.arrival(claimants)
    by_route = {plan.route: plan for plan in plans}
    choices = []
    for claimant in claimants:
        if claimant.forced:
            choices.append([claimant.forced])
        else:
            choices.append([None] + [route for route in claimant.weights if route in by_route])
    best = None
    for picks in itertools.product(*choices):
        loads = {plan.route: [0] * len(plan.dims) for plan in plans}
        total, valid = 0, True
        for claimant, route in zip(claimants, picks):
            if route is None:
                if claimant.alternative is None:
                    valid = False
                    break
                total += claimant.alternative
                continue
            plan = by_route[route]
            for d, dim in enumerate(plan.dims):
                before = loads[route][d]
                after = before + claimant.weights[route][d]
                if dim.overage is None:
                    if after > max(0, dim.room) and not claimant.forced:
                        valid = False
                else:
                    total += (max(0, after - max(0, dim.room)) - max(0, before - max(0, dim.room))) * dim.overage
                loads[route][d] = after
            if not valid:
                break
        if valid and (best is None or total < best):
            best = total
    return best


def per_fax(claimants, room, overage):
    """What today's per-fax policy pays: each fax in arrival order compares the plan's marginal price against its
    other route with ``plan_budget.marginal`` and ``order_key``, the functions the planner ranks by. The plan
    includes 1,000 pages a month and has ``room`` of them left."""
    plan_budget = efax(1000, overage)
    period = Period(datetime(2026, 10, 1), datetime(2026, 11, 1), date(2026, 10, 1), date(2026, 11, 1))
    used, faxes, total = 1000 - room, 0, 0
    for claimant in sorted(claimants, key=lambda item: (item.position, item.job_id)):
        pages = claimant.weights[plan_budget.route][0]
        left = _left(plan_budget, period, Usage(sent_faxes=faxes, sent_pages=used))
        on_plan = marginal(left, pages)
        other = marginal(None, pages, Prediction(None, 60.0, Money(claimant.alternative, 'USD'), 'Synthetic.', False))
        if claimant.forced or order_key(on_plan, 0) < order_key(other, 1):
            total += on_plan.cost.micros if on_plan.cost is not None else 0
            used, faxes = used + pages, faxes + 1
        else:
            total += claimant.alternative
    return total


def efax(included=100, overage=10 * CENT):
    """An allowance plan: ``included`` pages a month, then ``overage`` micros a page (eFax-like, synthetic)."""
    return Budget(route='efax', label='eFax', pages=None, faxes=None, day=1, included_pages=included,
                  page_overage_micros=overage, flat=True, source='set')


def test_codex_example_first_come_pays_fifty_dollars_and_the_allocation_pays_fifty_cents():
    # Two queued 100-page faxes, 100 included pages left; one costs $0.50 elsewhere, the other $50. The plan has no
    # extra-page price here (a hard allowance), as in the research example.
    cheap = Claimant('cheap', 0, {'efax': (100,)}, alternative=50 * CENT)
    dear = Claimant('dear', 1, {'efax': (100,)}, alternative=50 * DOLLAR)
    plans = (PlanRoom('efax', (Dim('pages', 100, None),)),)
    found = solve([cheap, dear], plans)
    assert found.exact and found.assigned == {'cheap': None, 'dear': 'efax'}
    assert (found.cost_micros, found.baseline_micros, found.saving_micros) == (50 * CENT, 50 * DOLLAR + 50 * CENT,
                                                                                50 * DOLLAR)
    assert per_fax([cheap, dear], 100, None) == 50 * DOLLAR
    # Arrival in the opposite order gives the same allocation.
    flipped = solve([replace_position(cheap, 1), replace_position(dear, 0)], plans)
    assert flipped.assigned == found.assigned and flipped.cost_micros == 50 * CENT


def replace_position(claimant, position):
    from dataclasses import replace
    return replace(claimant, position=position)


@pytest.mark.parametrize('seed', range(40))
def test_small_queues_match_every_possible_assignment_and_never_cost_more_than_first_come(seed):
    rng = random.Random(seed)
    count = rng.randint(1, 7)
    room = rng.choice([0, 30, 100, 150, 200])
    overage = rng.choice([None, 10 * CENT, 2 * CENT])
    claimants = [Claimant(f'fax{index}', index, {'efax': (rng.choice([1, 3, 10, 40, 60, 100, 120]),)},
                          alternative=rng.choice([0, 5 * CENT, 50 * CENT, 3 * DOLLAR, 12 * DOLLAR, 50 * DOLLAR]),
                          # A fax with no other route takes the plan whatever it costs; only past an allowance with
                          # a price is that a sum of money to compare.
                          forced='efax' if overage is not None and rng.random() < 0.15 else None)
                  for index in range(count)]
    plans = (PlanRoom('efax', (Dim('pages', room, overage),)),)
    found = solve(claimants, plans)
    assert found.exact
    assert found.cost_micros == brute(claimants, plans)
    first = per_fax(claimants, room, overage)
    assert found.cost_micros <= first
    # Whole faxes only: every fax is on the plan or on its other route, never split.
    assert set(found.assigned) == {claimant.job_id for claimant in claimants}
    assert set(found.assigned.values()) <= {'efax', None}
    # Opposite arrival order: the same least cost.
    flipped = [replace_position(claimant, count - 1 - claimant.position) for claimant in claimants]
    assert solve(flipped, plans).cost_micros == found.cost_micros


@pytest.mark.parametrize('seed', range(25))
def test_two_plans_and_a_normal_use_budget_in_pages_and_faxes_match_every_assignment(seed):
    rng = random.Random(1000 + seed)
    plans = (PlanRoom('efax', (Dim('pages', rng.choice([20, 60, 100]), 10 * CENT),)),
             PlanRoom('humblefax', (Dim('pages', rng.choice([10, 50, 90]), None), Dim('faxes', rng.choice([1, 2, 3]),
                                                                                       None))))
    claimants = []
    for index in range(rng.randint(2, 6)):
        pages = rng.choice([2, 5, 12, 30, 45])
        # HumbleFax counts the greater of the pages and the started minutes on the line.
        minutes = pages + rng.choice([0, 0, 1, 4])
        weights = {}
        if rng.random() < 0.8:
            weights['efax'] = (pages,)
        if rng.random() < 0.8:
            weights['humblefax'] = (max(pages, minutes), 1)
        if not weights:
            weights['efax'] = (pages,)
        claimants.append(Claimant(f'fax{index}', index, weights,
                                  alternative=rng.choice([1 * CENT, 40 * CENT, 2 * DOLLAR, 9 * DOLLAR])))
    found = solve(claimants, plans)
    assert found.exact and found.cost_micros == brute(claimants, plans)
    loads = found.loads['humblefax']
    assert loads[0] <= plans[1].dims[0].room and loads[1] <= plans[1].dims[1].room


def test_large_queues_use_the_greedy_order_and_report_a_true_bound():
    rng = random.Random(7)
    claimants = [Claimant(f'fax{index}', index, {'efax': (rng.randint(1, 60),)},
                          alternative=rng.randint(1, 400) * CENT) for index in range(40)]
    plans = (PlanRoom('efax', (Dim('pages', 400, None),)),)
    exact = solve(claimants, plans)
    rough = solve(claimants, plans, max_states=5)
    assert exact.exact and not rough.exact
    assert rough.saving_micros <= exact.saving_micros <= rough.bound_micros
    # The greedy order keeps within the room and saves most of what is possible here.
    assert rough.loads['efax'][0] <= 400
    assert rough.saving_micros >= 0.9 * exact.saving_micros


def test_a_forced_fax_takes_its_pages_first_and_ties_go_to_the_fax_that_came_first():
    plans = (PlanRoom('efax', (Dim('pages', 100, None),)),)
    forced = Claimant('only-efax', 2, {'efax': (60,)}, alternative=None, forced='efax')
    first = Claimant('first', 0, {'efax': (40,)}, alternative=3 * DOLLAR)
    second = Claimant('second', 1, {'efax': (40,)}, alternative=3 * DOLLAR)
    found = solve([forced, first, second], plans)
    assert found.assigned == {'only-efax': 'efax', 'first': 'efax', 'second': None}


# The reserve ---------------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 20, 12)
RENEWS = datetime(2026, 11, 1)


def weekly(value, units=100, weeks=12, *, every=1):
    """One fax of ``units`` pages worth ``value`` micros in each stretch of 12 days back from NOW (``every``th)."""
    left = RENEWS - NOW
    return [Past(NOW - k * left + timedelta(hours=1), units, value) for k in range(1, weeks + 1) if k % every == 0]


def test_no_history_or_a_single_expensive_fax_holds_nothing_back():
    sizes = allocation.reserve_sizes(100)
    assert not any(reserve_curve([], NOW, RENEWS, sizes).lower)
    # One expensive fax months ago is not evidence that another is coming.
    once = [Past(NOW - timedelta(days=40), 100, 50 * DOLLAR)]
    curve = reserve_curve(once, NOW, RENEWS, sizes)
    assert not any(curve.lower)


def test_regular_expensive_faxes_earn_a_reserve_that_falls_as_renewal_nears():
    sizes = allocation.reserve_sizes(100)
    history = weekly(20 * DOLLAR)
    curve = reserve_curve(history, NOW, RENEWS, sizes)
    assert curve.lower_at(100) > 0 and curve.lower_at(99) == 0  # a 100-page fax needs the whole 100 pages
    # Two days before renewal the stretches are two days long, and most of them hold no such fax.
    late = NOW + timedelta(days=10)
    late_curve = reserve_curve(history, late, RENEWS, sizes)
    assert late_curve.lower_at(100) < curve.lower_at(100)


def test_the_reserve_never_reads_faxes_after_now():
    sizes = allocation.reserve_sizes(100)
    history = weekly(20 * DOLLAR)
    future = history + [Past(NOW + timedelta(days=k), 100, 500 * DOLLAR) for k in range(1, 10)]
    assert reserve_curve(future, NOW, RENEWS, sizes) == reserve_curve(history, NOW, RENEWS, sizes)


def test_the_reserve_holds_back_only_what_the_lower_bound_saving_pays_for():
    plans = (PlanRoom('efax', (Dim('pages', 100, None),)),)
    cheap = Claimant('cheap', 0, {'efax': (100,)}, alternative=50 * CENT)
    curve = reserve_curve(weekly(20 * DOLLAR), NOW, RENEWS, allocation.reserve_sizes(100))
    found, reserves = choose([cheap], plans, {'efax': curve})
    assert reserves == {'efax': 100} and found.assigned == {'cheap': None}
    # A fax that saves more than the reserve's lower bound keeps the pages.
    dear = Claimant('dear', 0, {'efax': (100,)}, alternative=200 * DOLLAR)
    found, reserves = choose([dear], plans, {'efax': curve})
    assert reserves == {'efax': 0} and found.assigned == {'dear': 'efax'}
    # An urgent fax, or one with a send-by time, never gives its pages to the reserve.
    urgent = Claimant('urgent', 0, {'efax': (100,)}, alternative=50 * CENT, protected=True)
    found, reserves = choose([urgent], plans, {'efax': curve})
    assert reserves == {'efax': 0} and found.assigned == {'urgent': 'efax'}


def test_an_expensive_fax_that_never_arrives_costs_no_more_than_the_reserve_was_sure_it_would_save():
    # Expensive faxes came in only one stretch in three: the lower bound is small, so the reserve is too, and what
    # the cheap fax pays extra for it stays under that bound.
    plans = (PlanRoom('efax', (Dim('pages', 100, 5 * CENT),)),)
    cheap = Claimant('cheap', 0, {'efax': (60,)}, alternative=1 * DOLLAR)
    curve = reserve_curve(weekly(20 * DOLLAR, units=60, every=3), NOW, RENEWS, allocation.reserve_sizes(100))
    found, reserves = choose([cheap], plans, {'efax': curve})
    plain = solve([cheap], plans)
    extra = found.cost_micros - plain.cost_micros
    assert extra <= curve.lower_at(reserves['efax'])
    assert isinstance(curve, Curve)


# A synthetic month, replayed day by day -------------------------------------------------------------------

def month_of_faxes(seed, *, dear_every=7, days=62):
    """``[(day, pages, other route's price)]``: ordinary faxes every day, and a dear 80-page fax every ``dear_every``
    days (a fax to a number only an expensive account reaches)."""
    rng = random.Random(seed)
    found = []
    for day in range(days):
        for _ in range(rng.randint(1, 3)):
            pages = rng.choice([2, 5, 10, 20])
            found.append((day, pages, pages * rng.choice([1, 2]) * CENT // 10))
        if dear_every and day % dear_every == 3:
            found.append((day, 80, 80 * 5 * CENT))
    return found


def _day_queue(arrivals, day):
    return [Claimant(f'{day}-{index}', index, {'efax': (pages,)}, alternative=price)
            for index, (_, pages, price) in enumerate(item for item in arrivals if item[0] == day)]


def replay(arrivals, *, room=150, with_reserve=True, start=datetime(2026, 9, 1)):
    """Each day the faxes that arrived wait together; the allocation (and the reserve, learned only from earlier days)
    decides. The allowance starts again on the 1st. Returns (total cost, decisions by day, cost by day)."""
    used, period, history, total, decisions, costs = 0, None, [], 0, {}, {}
    for day in sorted({day for day, _, _ in arrivals}):
        now = start + timedelta(days=day, hours=12)
        first = datetime(now.year, now.month, 1)
        renews = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1)
        if period != first:
            period, used = first, 0
        queue = _day_queue(arrivals, day)
        plans = (PlanRoom('efax', (Dim('pages', room - used, 10 * CENT),)),)
        curve = (reserve_curve(history, now, renews, allocation.reserve_sizes(max(0, room - used)))
                 if with_reserve else None)
        found, reserves = choose(queue, plans, {'efax': curve})
        decisions[day] = (found.assigned, reserves['efax'])
        costs[day] = found.cost_micros
        total += found.cost_micros
        used += sum(claimant.weights['efax'][0] for claimant in queue if found.assigned[claimant.job_id] == 'efax')
        history += [Past(now, claimant.weights['efax'][0],
                         min(claimant.alternative, 10 * CENT * claimant.weights['efax'][0])) for claimant in queue]
    return total, decisions, costs


def replay_first_come(arrivals, *, room=150, start=datetime(2026, 9, 1)):
    """The same days with today's per-fax policy (``marginal`` and ``order_key``, as ``per_fax``), each day's faxes
    in arrival order against what is left."""
    total, used, period = 0, 0, None
    for day in sorted({day for day, _, _ in arrivals}):
        now = start + timedelta(days=day, hours=12)
        first = datetime(now.year, now.month, 1)
        if period != first:
            period, used = first, 0
        queue = _day_queue(arrivals, day)
        total += per_fax(queue, room - used, 10 * CENT)
        for claimant in queue:  # what the plan carried that day, as the per-fax policy chose
            pages = claimant.weights['efax'][0]
            if max(0, pages - max(0, room - used)) * 10 * CENT < claimant.alternative:
                used += pages
    return total


@pytest.mark.parametrize('seed', range(4))
def test_five_replayed_months_cost_no_more_than_first_come_and_never_read_the_future(seed):
    arrivals = month_of_faxes(seed, days=150)
    total, decisions, _ = replay(arrivals)
    plain, _, _ = replay(arrivals, with_reserve=False)
    first_come = replay_first_come(arrivals)
    # Queued faxes alone already beat first come; the reserve, learned as the months go by, saves more again.
    assert total <= plain <= first_come
    assert sum(1 for _, reserve in decisions.values() if reserve) > 0
    # Changing everything after day 100 changes nothing decided on or before it.
    later = [item for item in arrivals if item[0] <= 100] + [(day, 120, 900 * DOLLAR) for day in range(101, 150)]
    _, changed, _ = replay(later)
    assert {day: found for day, found in changed.items() if day <= 100} == {
        day: found for day, found in decisions.items() if day <= 100}


@pytest.mark.parametrize('seed', range(4))
def test_when_the_dear_faxes_stop_coming_the_reserve_costs_little_and_less_than_it_saved(seed):
    arrivals = [item for item in month_of_faxes(seed, days=180) if not (item[1] == 80 and item[0] >= 120)]
    total, _, costs = replay(arrivals)
    plain, _, plain_costs = replay(arrivals, with_reserve=False)
    late = sum(costs[day] - plain_costs[day] for day in costs if day >= 120)
    early = sum(plain_costs[day] - costs[day] for day in costs if day < 120)
    assert late <= early and total <= plain
    assert late <= 1 * DOLLAR  # cheap faxes paid a little more while the reserve waited for faxes that never came


def test_in_turn_on_the_screen_matches_the_per_fax_policy():
    for seed in range(30):
        rng = random.Random(500 + seed)
        room, overage = rng.choice([0, 40, 100]), rng.choice([None, 10 * CENT])
        claimants = [Claimant(f'fax{index}', index, {'efax': (rng.choice([5, 30, 60, 100]),)},
                              alternative=rng.choice([1, 50, 300, 1200]) * CENT) for index in range(rng.randint(1, 6))]
        plans = (PlanRoom('efax', (Dim('pages', room, overage),)),)
        assert allocation.in_turn(claimants, plans) == per_fax(claimants, room, overage), seed


# Through the planner, the real predictor and the database (SQLite and PostgreSQL) ------------------------------

# Phaxio is given an allowance of 100 pages a month, then $0.10 a page; SignalWire, the other account the automatic
# choice may use, is $0.0095 a minute. Both are synthetic settings on the shared rules-delivery installation.
ALLOWANCE = {**LISTED, 'FAX_PLAN_BUDGETS': 'phaxio:included_pages=100,page_overage=0.10,day=1'}
SMALL, LARGE = '+12025550141', '+12025550142'


@pytest.fixture
def allowance(database, tmp_path):  # noqa: F811
    allocation._FEATURES.clear()
    allocation._CURVES.clear()
    allocation._SECONDS.clear()
    return installation(database, tmp_path, ALLOWANCE)


def signalwire_cost(pages):
    """SignalWire's price for ``pages`` typical pages, worked out here from the predictor's published constants: 11 s
    of setup, then for each page 103,000 bits at 2-D coding (x1.35) over 14,400 bit/s plus 2.6 s of handshake; whole
    minutes at $0.0095."""
    seconds = 11 + pages * (103_000 * 1.35 / 14_400 + 2.6)
    minutes = -(-int(seconds * 1000) // 60_000)
    return minutes * 9_500


def values_of(env):
    return env.snapshot.active.values


@pytest.mark.asyncio
@pytest.mark.parametrize('order', ['small_first', 'large_first'])
async def test_the_larger_waiting_fax_gets_the_last_pages_in_either_order(allowance, order):  # noqa: F811
    from api.app.outbound_worker import OutboundWorker
    from api.app.routing.transport import RoutedTransport
    env = allowance
    pages = {SMALL: 60, LARGE: 100}
    numbers = (SMALL, LARGE) if order == 'small_first' else (LARGE, SMALL)
    jobs = {number: accept(env, to=number, pages=pages[number]) for number in numbers}
    found = allocation.allocate(env.routes, values_of(env))
    assert found.solution.exact and found.solution.assigned == {jobs[SMALL]: None, jobs[LARGE]: 'phaxio'}
    assert found.solution.cost_micros == signalwire_cost(60)
    # First come, first served: the 60-page fax takes 60 pages and the 100-page fax goes by SignalWire.
    assert found.first_come_micros == (signalwire_cost(100) if order == 'small_first' else signalwire_cost(60))
    inner = Inner(env.delivery)
    worker = OutboundWorker(env.delivery, RoutedTransport(inner, direct=None))
    assert await worker.step() is True and await worker.step() is True
    assert dict(zip(numbers, inner.used)) == {SMALL: 'signalwire', LARGE: 'phaxio'}
    sentence = allocation.explanation(env.engine, jobs[SMALL])
    if order == 'small_first':
        saving = signalwire_cost(100) - signalwire_cost(60)
        from api.app.routing.delivered import short_money_text
        assert sentence == (f'Sent by SignalWire for about {short_money_text(signalwire_cost(60), "USD")} so your '
                            'last 100 Phaxio pages this month go to the waiting fax they save more on, saving about '
                            f'{short_money_text(saving, "USD")} in all (estimate).')
    else:
        assert sentence is None  # the 100-page fax was already on its way: Phaxio was simply full
    left = allocation.scarce_plans(env.routes, values_of(env), datetime.utcnow())
    assert sum(left[0].in_flight) + left[0].left.used.pages <= 100 + 1  # pages plus the one fax on its way


def test_two_workers_planning_at_once_agree_on_who_gets_the_pages(allowance):  # noqa: F811
    from api.app.routing.transport import RoutedTransport
    env = allowance
    small, large = accept(env, to=SMALL, pages=60), accept(env, to=LARGE, pages=100)
    first, second = env.delivery.claim('worker-a'), env.delivery.claim('worker-b')
    assert {first.job_id, second.job_id} == {small, large}
    transport = RoutedTransport(Inner(env.delivery), direct=None)
    plans = {claim.job_id: transport._plan(claim)[0] for claim in (second, first)}
    assert (plans[small].first.route.key, plans[small].first.reason) == ('signalwire', 'plan_reserved')
    assert plans[large].first.route.key == 'phaxio' and plans[large].first.reason == 'included'
    # The 100-page fax's decision is recorded first: the 60-page fax still goes by SignalWire, now because the
    # pages are on their way.
    transport._record(first if first.job_id == large else second, plans[large], plans[large].first)
    again = transport._plan(first if first.job_id == small else second)[0]
    assert again.first.route.key == 'signalwire'


def test_with_room_for_every_waiting_fax_prices_are_exactly_as_before(allowance):  # noqa: F811
    from api.app.routing.pricing import prices_for
    env = allowance
    job = accept(env, to=SMALL, pages=30)
    accept(env, to=LARGE, pages=40)
    pinned = envelopes.load(env.engine, job)
    values = values_of(env)
    with_job = prices_for(env.routes, values, SMALL, 30, pinned=pinned, bound='phaxio', job_id=job)
    without = prices_for(env.routes, values, SMALL, 30, pinned=pinned, bound='phaxio')
    assert with_job == without and with_job['phaxio'].in_plan and with_job['phaxio'].held is None


def test_a_cost_cap_never_turns_a_held_plan_into_a_held_fax(allowance):  # noqa: F811
    from api.app.routing.transport import RoutedTransport
    env = allowance
    publish(env, {'format': 1, 'limits': [rule('l-cap', {'cap_cost': {'currency': 'USD', 'amount': '0.50'}})],
                  'routes': []})
    small, large = accept(env, to=SMALL, pages=60), accept(env, to=LARGE, pages=100)
    claim = env.delivery.claim('worker-a')
    assert claim.job_id == small
    plan, _, _ = RoutedTransport(Inner(env.delivery), direct=None)._plan(claim)
    # Phaxio at $6 for this fax is over the cap and skipped; SignalWire is under it: the fax goes, never held.
    assert plan.first.route.key == 'signalwire' and ('phaxio', 'over_cap') in plan.skipped
    assert large


def test_a_fax_whose_rule_uses_the_plan_takes_its_pages_first(allowance):  # noqa: F811
    env = allowance
    publish(env, {'format': 1, 'routes': [rule('r-small', {'use': 'phaxio'}, {'destination': {'numbers': [SMALL]}})]})
    small, large = accept(env, to=SMALL, pages=60), accept(env, to=LARGE, pages=100)
    found = allocation.allocate(env.routes, values_of(env))
    assert found.features[small].group == 'forced' and found.features[large].group == 'candidate'
    assert found.solution.assigned == {small: 'phaxio', large: None}
    # The forced fax fills the plan like a fax on its way: the 100-page fax goes by SignalWire because Phaxio is
    # full, not because its pages went to a fax they save more on.
    hold = found.hold(large, 'phaxio')
    assert (hold.others, hold.room, hold.kind) == (0, 40, 'queue')
    from api.app.routing.plan import RoutePlanner
    from api.app.routing.pricing import prices_for
    pinned = envelopes.load(env.engine, large)
    prices = prices_for(env.routes, values_of(env), LARGE, 100, pinned=pinned, bound='phaxio', job_id=large)
    plan = RoutePlanner(env.routes).plan(to_number=LARGE, bound='phaxio', values=values_of(env), pages=100,
                                         alternates=True, pinned=pinned, prices=prices, job_id=large)
    assert plan.first.route.key == 'signalwire' and plan.first.reason != 'plan_reserved' and plan.held is None


def test_unreadable_prices_leave_an_account_out_and_a_bug_in_the_allocation_raises(allowance, monkeypatch,  # noqa: F811
                                                                                       caplog):
    """prices_for leaves out only an account whose prices or plan cannot be read, and logs why; a bug in
    after_hold (the plan's room after what is held for other faxes) raises instead of quietly pricing it out."""
    from api.app.routing import pricing
    from api.app.routing.database import DeliveryStoreError
    env = allowance
    # The 60-page fax's rule uses the plan, so the room held for it reaches the 100-page fax's price (after_hold).
    publish(env, {'format': 1, 'routes': [rule('r-small', {'use': 'phaxio'}, {'destination': {'numbers': [SMALL]}})]})
    small, large = accept(env, to=SMALL, pages=60), accept(env, to=LARGE, pages=100)
    pinned = envelopes.load(env.engine, large)
    ask = dict(pinned=pinned, bound='phaxio', job_id=large)
    assert allocation.allocate(env.routes, values_of(env)).hold(large, 'phaxio') is not None
    assert 'phaxio' in pricing.prices_for(env.routes, values_of(env), LARGE, 100, **ask)
    real = pricing.price

    def unreadable(routes, values, key, *args, **kwargs):
        if key == 'phaxio':
            raise DeliveryStoreError('Delivery storage is unavailable.')
        return real(routes, values, key, *args, **kwargs)
    monkeypatch.setattr(pricing, 'price', unreadable)
    with caplog.at_level('WARNING'):
        left = pricing.prices_for(env.routes, values_of(env), LARGE, 100, **ask)
    assert 'phaxio' not in left and 'signalwire' in left and 'Account phaxio could not be priced' in caplog.text
    monkeypatch.setattr(pricing, 'price', real)

    def broken(left, hold):
        raise TypeError('a bug in after_hold')
    monkeypatch.setattr(allocation, 'after_hold', broken)
    with pytest.raises(TypeError, match='a bug in after_hold'):
        pricing.prices_for(env.routes, values_of(env), LARGE, 100, **ask)


def test_sent_details_show_the_kept_sentence_over_the_reason_code():
    from api.app.routing.http import _cost_view
    sentence = 'Sent by SignalWire for about $0.12 so your last 100 Phaxio pages this month go to the waiting fax ' \
               'they save more on, saving about $0.076 in all (estimate).'
    kept = _cost_view({'route': 'signalwire', 'route_reason': 'plan_reserved', 'reported_cost': {},
                       'plan_allocation': sentence})
    assert kept['route_explanation'] == sentence and 'plan_allocation' not in kept
    plain = _cost_view({'route': 'signalwire', 'route_reason': 'plan_reserved', 'reported_cost': {},
                        'plan_allocation': None})
    assert plain['route_explanation'] == ("Your plan's last included pages or minutes went to faxes they saved more "
                                          'on, so Faxbot sent this one by the next cheapest route.')


def test_faxbots_starting_budget_for_an_unlimited_plan_keeps_no_reserve():
    from types import SimpleNamespace
    start = Budget(route='humblefax', label='HumbleFax', pages=200, faxes=50, day=1, flat=True, source='default')
    assert not allocation.reserve_allowed(SimpleNamespace(left=SimpleNamespace(budget=start)))
    from dataclasses import replace
    assert allocation.reserve_allowed(SimpleNamespace(left=SimpleNamespace(budget=replace(start, source='set'))))
    assert allocation.reserve_allowed(SimpleNamespace(left=SimpleNamespace(budget=efax())))


@pytest.fixture
def allocation_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for served in _serve(monkeypatch, tmp_path, FAX_OUTBOUND_ROUTES='humblefax'):
        yield Cli(served)


def test_faxbot_costs_plans_allocation_is_its_own_command(allocation_cli):
    cli = allocation_cli
    shown = cli('costs', 'plans', 'allocation')
    assert shown.exit_code == 0, (shown.stdout, shown.stderr)
    view = cli.json('costs', 'plans', 'allocation')
    assert view['estimate'] is True and 'plans' in view
    for plan in view['plans']:
        assert plan['sentence'] in shown.stdout.replace('\n', ' ') or plan['name'] in shown.stdout


def test_the_screen_and_the_reserve_read_only_earlier_faxes(allowance):  # noqa: F811
    env = allowance
    small, large = accept(env, to=SMALL, pages=60), accept(env, to=LARGE, pages=100)
    page = allocation.view(env.routes, values_of(env))
    plan = page['plans'][0]
    assert (plan['route'], plan['name'], plan['unit'], plan['room'], plan['reserve']) == ('phaxio', 'Phaxio', 'pages',
                                                                                        100, 0)
    assert [(item['to'], item['outcome'], item['route_label']) for item in plan['faxes']] == [
        (SMALL, 'other', 'SignalWire'), (LARGE, 'plan', 'Phaxio')]
    assert plan['sentence'].startswith('Phaxio has 100 included pages left until 1 ')
    assert plan['sentence'].endswith(': 100 go to 1 waiting fax.')
    assert plan['reserve_sentence'].startswith('Faxbot keeps nothing back: your earlier faxes do not show dearer')
    assert plan['saving_sentence'].startswith('Sharing the pages this way saves about ')
    scarce = allocation.scarce_plans(env.routes, values_of(env), datetime.utcnow())[0]
    assert allocation.history(env.routes, values_of(env), scarce, ['signalwire'], datetime.utcnow()) == []
    assert small and large


def test_a_fax_its_rules_hold_for_approval_takes_no_pages_while_it_waits(allowance):  # noqa: F811
    env = allowance
    publish(env, {'format': 1, 'limits': [rule('l-big', {'hold_for_approval': {'separate_approver': True}},
                                                {'document': {'pages_over': 80}})], 'routes': []})
    small, large = accept(env, to=SMALL, pages=60), accept(env, to=LARGE, pages=100)
    assert [fax.job_id for fax in allocation.waiting(env.engine)] == [small]
    found = allocation.allocate(env.routes, values_of(env))
    assert found.solution is None and found.hold(small, 'phaxio') is None  # it fits: priced as before
    assert large


def sent_before(env, number, pages, when, cost=None):
    """One fax already sent by SignalWire at ``when``, with its estimated cost (synthetic records; nothing waits)."""
    import sqlalchemy as sa
    job = accept(env, to=number, pages=pages)
    deliveries = sa.Table('outbound_deliveries', sa.MetaData(), autoload_with=env.engine)
    with env.engine.begin() as connection:
        connection.execute(deliveries.update().where(deliveries.c.id == job).values(state='success'))
        connection.execute(env.routes.attempts.insert().values(id=job, job_id=job, sequence=1, phase='success',
                                                               created_at=when, submitted_at=when, completed_at=when))
        connection.execute(env.routes.costs.insert().values(
            id=job, job_id=job, destination=number, route='signalwire', route_reason='cheapest',
            provider_id='signalwire', outcome='success', billing_checks=0, created_at=when, updated_at=when,
            estimated_cost_micros=cost, currency='USD' if cost is not None else None))
    return job


def test_the_reserve_learns_only_from_faxes_sent_before_now(allowance):  # noqa: F811
    env = allowance
    now = datetime.utcnow().replace(microsecond=0)
    for days in (3, 10):
        sent_before(env, SMALL, 20, now - timedelta(days=days))
    # Recorded at $5: worth that, capped by what Phaxio charges for 20 pages past its allowance ($2).
    sent_before(env, SMALL, 20, now - timedelta(days=17), cost=5 * DOLLAR)
    sent_before(env, LARGE, 50, now + timedelta(days=1))  # after now: never read
    scarce = allocation.scarce_plans(env.routes, values_of(env), now)[0]
    found = sorted(allocation.history(env.routes, values_of(env), scarce, ['signalwire'], now),
                   key=lambda item: item.at)
    assert [(item.units, item.value) for item in found] == [(20, 2 * DOLLAR), (20, signalwire_cost(20)),
                                                            (20, signalwire_cost(20))]
    assert all(item.at < now for item in found)
    assert allocation.view(env.routes, values_of(env), now)['plans'][0]['sentence'].endswith(
        'and no fax is waiting.')


def test_the_allocation_over_http_and_its_permission(client):  # noqa: F811
    view = client.get('/routing/plans/allocation', headers=ADMIN)
    assert view.status_code == 200, view.text
    assert view.json()['estimate'] is True
    reader = scoped_key(client, ['fax:send'])
    assert client.get('/routing/plans/allocation', headers=reader).status_code == 403


class Recorder:
    def __init__(self):
        self.lines, self.tables = [], []

    def line(self, text):
        self.lines.append(text)

    def table(self, columns, rows, *, title=None, empty=''):
        self.tables.append((title, columns, rows))


def test_the_command_line_prints_each_plan_its_waiting_faxes_and_the_reserve(monkeypatch):
    from api.app.cli import output
    from api.app.cli.commands.delivery import show_allocation
    monkeypatch.setattr(output, 'home_currency', lambda: 'USD')
    out = Recorder()
    show_allocation(out, {'plans': [], 'empty_sentence': 'Nothing to share out.'})
    assert out.lines == ['Nothing to share out.']
    out = Recorder()
    show_allocation(out, {'plans': [{
        'route': 'efax', 'name': 'eFax', 'unit': 'pages', 'room': 100, 'reserve': 0,
        'sentence': 'eFax has 100 included pages left until 1 November: 100 go to 1 waiting fax.',
        'saving_sentence': 'Sharing the pages this way saves about $49.50 against giving them to the waiting faxes '
                           'in turn (estimate).',
        'reserve_sentence': 'Faxbot keeps nothing back: your earlier faxes do not show dearer ones coming before 1 '
                            'November.',
        'bound_sentence': None, 'left_sentence': 'eFax has used 100 of the 200 pages your plan includes.',
        'faxes': [{'to': SMALL, 'pages': 60, 'units': 60, 'queued_at': '2026-10-20T15:00:00', 'outcome': 'other',
                   'route_label': 'Telnyx', 'cost': [{'currency': 'USD', 'amount': '0.50'}]},
                  {'to': LARGE, 'pages': 100, 'units': 100, 'queued_at': '2026-10-20T15:01:00', 'outcome': 'plan',
                   'route_label': 'eFax', 'cost': []}]}]})
    assert out.lines[0] == 'eFax has 100 included pages left until 1 November: 100 go to 1 waiting fax.'
    (title, columns, rows), = out.tables
    assert title == 'eFax, waiting faxes' and columns[-2:] == ['Route', 'Cost (estimate)']
    assert [row[4:] for row in rows] == [['Goes another way', 'Telnyx', '$0.50'], ['Gets the plan', 'eFax', '-']]
    assert out.lines[1].startswith('Sharing the pages this way saves about $49.50')
