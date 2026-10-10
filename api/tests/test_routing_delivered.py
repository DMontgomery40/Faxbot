"""Cost per delivered fax, and route choice by it: arithmetic, policy, the send path and recommendations."""
from datetime import datetime

import pytest

from api.app.routing.costs import RateCard, parse_amount
from api.app.routing.delivered import (MIN_DELIVERED, Attempt, DeliveredCost, attempt_figure, delivered_costs,
                                       short_money_text)
from api.app.routing.plan import REASON_TEXT, explain, route_label
from api.app.routing.policy import RouteCandidate, RouteChoice, RoutePolicy, RouteStats
from api.app.routing.recommendations import basis_text, cost_text


CAPTURED = datetime(2026, 10, 3)


def card(provider, *, minute='0', page='0', call='0', increment=60, minimum=0, monthly=None, currency='USD'):
    return RateCard(None, provider, 'outbound', provider.title(), currency, parse_amount(minute), parse_amount(page),
                    parse_amount(call), increment, minimum, None, CAPTURED,
                    None if monthly is None else parse_amount(monthly))


def charged(route, outcome, amount, *, pages=1, seconds=None, currency='USD'):
    return Attempt(route, route, outcome, reported_micros=parse_amount(amount), reported_currency=currency,
                   pages=pages, seconds=seconds)


def estimated(route, outcome, amount, *, pages=1):
    return Attempt(route, route, outcome, estimated_micros=parse_amount(amount), currency='USD', pages=pages)


def figure(route, delivered, failed=0, *, each='0.005', estimate=False):
    """A route with ``delivered`` delivered and ``failed`` failed attempts, each costing ``each``."""
    make = estimated if estimate else charged
    attempts = [make(route, 'success', each) for _ in range(delivered)] + [make(route, 'failed', each)
                                                                           for _ in range(failed)]
    return delivered_costs(attempts)[route]


# -- the arithmetic ---------------------------------------------------------------------

def test_every_attempt_counts_toward_the_cost_and_only_delivered_faxes_divide_it():
    attempts = [charged('sip', 'success', '0.005', pages=2, seconds=50), charged('sip', 'success', '0.005', pages=1,
                seconds=40), charged('sip', 'success', '0.010', pages=3), charged('sip', 'failed', '0.010'),
                estimated('sip', 'uncertain', '0.005'), Attempt('sip', 'sip', 'pending', reported_micros=999_000,
                                                                reported_currency='USD')]
    route = delivered_costs(attempts)['sip']
    # The pending attempt is not counted; the failed and the uncertain ones are.
    assert (route.attempts, route.delivered, route.failed, route.uncertain) == (5, 3, 1, 1)
    assert route.cost_micros == parse_amount('0.035') and route.currency == 'USD'
    # $0.035 over 3 delivered faxes is 0.011666..., rounded up like an invoice.
    assert route.per_delivered_micros == 11_667
    assert route.delivered_percent == 60 and route.average_pages == 2.0
    # Connected time is averaged over the delivered faxes whose call was measured.
    assert route.average_seconds == 45 and route.timed == 2
    assert (route.reported, route.estimated, route.unpriced) == (4, 1, 0) and route.estimate is True
    assert route.state == 'priced' and route.comparable()


def test_attempts_without_a_charge_or_estimate_are_priced_from_the_rate_card_with_its_minimum():
    trunk = card('sip', minute='0.005', increment=60, minimum=60)
    # A 5-second failed call still bills the 60-second minimum; a 61-second fax bills two minutes.
    failed = Attempt('sip', 'sip', 'failed', pages=2, seconds=5)
    delivered = Attempt('sip', 'sip', 'success', pages=2, seconds=61)
    assert attempt_figure(failed, trunk) == (5_000, 'USD', 'estimated')
    assert attempt_figure(delivered, trunk) == (10_000, 'USD', 'estimated')
    # Nothing measured: the time from submission to the result is used, as when the attempt finished.
    assert attempt_figure(Attempt('sip', 'sip', 'success', pages=1, elapsed_seconds=125), trunk) == (
        15_000, 'USD', 'estimated')
    # A per-minute card cannot price a call of unknown length.
    assert attempt_figure(Attempt('sip', 'sip', 'success', pages=1), trunk) is None
    # An uncertain attempt is estimated as a successful send of its pages, by the shared predictor: four typical
    # pages take about 60 seconds, so one billed minute a quarter of the time and two otherwise.
    assert attempt_figure(Attempt('sip', 'sip', 'uncertain', pages=4), trunk) == (8_750, 'USD', 'estimated')
    per_page = card('phaxio', page='0.07', call='0.01')
    assert attempt_figure(Attempt('phaxio', 'phaxio', 'failed', pages=3), per_page) == (10_000, 'USD', 'estimated')
    # A reported charge always wins over an estimate, and a stored estimate over the current card.
    both = Attempt('sip', 'sip', 'success', reported_micros=500, reported_currency='USD', estimated_micros=5_000,
                   currency='USD')
    assert attempt_figure(both, trunk) == (500, 'USD', 'reported')
    stored = Attempt('sip', 'sip', 'success', estimated_micros=7_000, currency='USD', seconds=61)
    assert attempt_figure(stored, trunk) == (7_000, 'USD', 'estimated')
    route = delivered_costs([failed, delivered], {'sip': trunk})['sip']
    assert route.per_delivered_micros == 15_000 and route.estimate and cost_text(route) == 'About $0.015'
    assert basis_text(route) == '2 faxes estimated'


def test_a_flat_plan_is_included_never_a_free_cheapest_route():
    humblefax = card('humblefax', monthly='10')
    attempts = [Attempt('humblefax', 'humblefax', 'success', estimated_micros=0, currency='USD', pages=1),
                Attempt('humblefax', 'humblefax', 'success', pages=1)]  # captured before the plan had a card
    route = delivered_costs(attempts, {'humblefax': humblefax})['humblefax']
    assert route.state == 'included' and route.per_delivered_micros is None
    assert not route.comparable() and route.delivered == 2
    assert cost_text(route) == 'Included in your plan' and basis_text(route) is None


def test_a_mix_of_charges_and_estimates_is_an_estimate():
    route = delivered_costs([charged('sip', 'success', '0.005'), charged('sip', 'success', '0.005'),
                             estimated('sip', 'success', '0.006'), estimated('sip', 'failed', '0.004')])['sip']
    assert route.cost_micros == 20_000 and route.per_delivered_micros == 6_667
    # The figure stays exact; text shows it as the console shows money, up to four places under ten cents.
    assert route.estimate and cost_text(route) == 'About $0.0067'
    assert basis_text(route) == '2 faxes billed, 2 estimated'
    exact = figure('sip', 3, each='0.005')
    assert not exact.estimate and cost_text(exact) == '$0.005' and basis_text(exact) == '3 faxes billed'


def test_unknown_cost_mixed_currencies_undelivered_and_direct_are_never_compared():
    unknown = delivered_costs([charged('phaxio', 'success', '0.07')] * 3 + [Attempt('phaxio', 'phaxio', 'failed')])
    assert unknown['phaxio'].state == 'unpriced' and unknown['phaxio'].per_delivered_micros is None
    assert cost_text(unknown['phaxio']) == 'Unknown' and basis_text(unknown['phaxio']) == '3 faxes billed, 1 with no price'
    mixed = delivered_costs([charged('sip', 'success', '0.005')] * 3 + [charged('sip', 'success', '0.004',
                                                                               currency='EUR')])
    assert mixed['sip'].state == 'mixed' and not mixed['sip'].comparable()
    undelivered = figure('sip', 0, 2)
    assert undelivered.state == 'undelivered' and cost_text(undelivered) == 'None delivered'
    assert undelivered.cost_micros == 10_000 and undelivered.per_delivered_micros is None
    direct = delivered_costs([Attempt('direct', 'direct', 'success')] * 4)['direct']
    assert direct.state == 'direct' and cost_text(direct) == 'No charge, sent straight to the partner' and not direct.comparable()


# -- the policy -------------------------------------------------------------------------

def provider(key, rate=None, bound=False):
    return RouteCandidate(key, 'provider', key, rate, bound=bound)


def keys(choices):
    return [(choice.route.key, choice.reason) for choice in choices]


TRUNK = provider('sip', card('sip', minute='0.005'))
API = provider('phaxio', card('phaxio', page='0.07'), bound=True)


def test_enough_delivered_faxes_on_two_routes_order_them_by_cost_per_delivered_fax():
    # The rate cards put the trunk first, but its calls often had to be placed twice.
    delivered = {'sip': figure('sip', 4, 1, each='0.10'), 'phaxio': figure('phaxio', 3, each='0.07')}
    assert keys(RoutePolicy().order([API, TRUNK])) == [('sip', 'cheapest'), ('phaxio', 'alternative')]
    choices = RoutePolicy().order([API, TRUNK], delivered=delivered)
    assert keys(choices) == [('phaxio', 'cheapest_delivered'), ('sip', 'alternative')]
    assert choices[0].compared == 2 and choices[0].delivered.per_delivered_micros == 70_000
    # Evidence that agrees with the rate cards keeps the order and records why.
    agree = {'sip': figure('sip', 3, each='0.005'), 'phaxio': figure('phaxio', 3, each='0.07')}
    assert keys(RoutePolicy().order([API, TRUNK], delivered=agree)) == [
        ('sip', 'cheapest_delivered'), ('phaxio', 'alternative')]


def test_below_the_threshold_the_rate_card_order_is_unchanged():
    assert MIN_DELIVERED == 3
    candidates = [API, TRUNK, provider('signalwire', card('signalwire', minute='0.0095'))]
    unpriced = delivered_costs([charged('phaxio', 'success', '0.01')] * 3 + [Attempt('phaxio', 'phaxio', 'failed')])
    # Two delivered faxes on one route; evidence on one route only; one route with an attempt of unknown cost.
    for delivered in ({'sip': figure('sip', 2, each='0.50'), 'phaxio': figure('phaxio', 3, each='0.01')},
                      {'sip': figure('sip', 9, each='0.50')},
                      {'sip': figure('sip', 3, each='0.50'), 'phaxio': unpriced['phaxio']}):
        assert RoutePolicy().order(candidates, delivered=delivered) == [
            RouteChoice(choice.route, choice.reason, choice.estimated_cost_micros, delivered.get(choice.route.key))
            for choice in RoutePolicy().order(candidates)]
    # Exactly at the threshold, the evidence decides.
    at = {'sip': figure('sip', 3, each='0.50'), 'phaxio': figure('phaxio', 3, each='0.01')}
    assert RoutePolicy().order(candidates, delivered=at)[0].reason == 'cheapest_delivered'
    assert RoutePolicy(min_delivered=4).order(candidates, delivered=at)[0].reason == 'cheapest'


def test_the_preferred_route_always_wins():
    delivered = {'sip': figure('sip', 5, each='0.50'), 'phaxio': figure('phaxio', 5, each='0.01'),
                 'signalwire': figure('signalwire', 5, each='0.02')}
    signalwire = provider('signalwire', card('signalwire', minute='0.0095'))
    choices = RoutePolicy().order([API, TRUNK, signalwire], delivered=delivered, preferred='sip')
    # The rest still follow the evidence, behind the administrator's choice.
    assert keys(choices) == [('sip', 'preferred'), ('phaxio', 'alternative'), ('signalwire', 'alternative')]


def test_routes_without_evidence_keep_their_places_and_a_flat_plan_stays_included():
    plan = provider('humblefax', card('humblefax', monthly='10'))
    untested = provider('signalwire', card('signalwire', minute='0.008'))
    included = delivered_costs([Attempt('humblefax', 'humblefax', 'success')] * 5, {'humblefax': plan.card})
    delivered = {'sip': figure('sip', 6, 2, each='0.10'), 'phaxio': figure('phaxio', 3, each='0.07'),
                 'humblefax': included['humblefax']}
    before = keys(RoutePolicy().order([API, TRUNK, plan, untested]))
    assert before == [('humblefax', 'included'), ('sip', 'alternative'), ('signalwire', 'alternative'),
                      ('phaxio', 'alternative')]
    after = keys(RoutePolicy().order([API, TRUNK, plan, untested], delivered=delivered))
    # The two routes with evidence swap within their own places; the plan and the untested route stay put.
    assert after == [('humblefax', 'included'), ('phaxio', 'alternative'), ('signalwire', 'alternative'),
                     ('sip', 'alternative')]


def test_an_unreliable_route_or_another_currency_is_not_compared():
    delivered = {'sip': figure('sip', 3, 2, each='0.001'), 'phaxio': figure('phaxio', 3, each='0.07')}
    stats = {'sip': RouteStats(attempts=5, successes=3)}
    assert keys(RoutePolicy().order([API, TRUNK], stats=stats, delivered=delivered)) == [
        ('phaxio', 'cheapest'), ('sip', 'unreliable')]
    euro = {'sip': figure('sip', 4, each='0.50'),
            'phaxio': delivered_costs([charged('phaxio', 'success', '0.01', currency='EUR')] * 3)['phaxio']}
    assert keys(RoutePolicy().order([API, TRUNK], delivered=euro))[0] == ('sip', 'cheapest')


def test_the_reason_is_one_plain_sentence_with_the_amount_and_the_routes_compared():
    delivered = {'sip': figure('sip', 3, each='0.006'), 'phaxio': figure('phaxio', 3, each='0.07')}
    first = RoutePolicy().order([API, TRUNK], delivered=delivered)[0]
    assert explain(first) == (f'{route_label("sip")} cost $0.006 per delivered fax to this number over the last '
                              '30 days, the cheapest of 2 routes.')
    guessed = {'sip': figure('sip', 3, each='0.006', estimate=True), 'phaxio': figure('phaxio', 3, each='0.07')}
    assert explain(RoutePolicy().order([API, TRUNK], delivered=guessed)[0]) == (
        f'{route_label("sip")} cost about $0.006 per delivered fax to this number over the last 30 days, '
        'the cheapest of 2 routes.')
    # A stored decision has only its reason: Sent details say it without the amount.
    assert explain(RouteChoice(TRUNK, 'cheapest_delivered', None)) == REASON_TEXT['cheapest_delivered'] == (
        'The cheapest route per delivered fax to this number over the last 30 days.')


def test_averages_read_like_the_money_on_every_other_screen():
    assert [short_money_text(micros, 'USD') for micros in (8_889, 375_000, 1_500_000, 5_000, 0, -8_889)] == [
        '$0.0089', '$0.38', '$1.50', '$0.005', '$0.00', '-$0.0089']
    assert short_money_text(8_889, 'EUR') == '0.0089 EUR'


def test_delivered_cost_is_a_frozen_value():
    with pytest.raises(Exception):
        DeliveredCost('sip', 'sip').attempts = 1


# -- the send path and recommendations, on SQLite and PostgreSQL ------------------------

from api.tests.test_routing_multiprovider import Inner, accept, multi  # noqa: E402,F401 (fixture)
from api.tests.test_schema import database  # noqa: E402,F401 (fixture)


async def _send(multi, receipts):
    """Send one fax per receipt through the worker and the route chooser; returns the attempts and routes used."""
    from api.app.outbound_worker import OutboundWorker
    from api.app.routing.transport import RoutedTransport
    _, delivery, _, _ = multi
    attempts, used = [], []
    for receipt in receipts:
        job = accept(multi)
        inner = Inner(delivery, [receipt])
        assert await OutboundWorker(delivery, RoutedTransport(inner)).step() is True
        attempts.append((job, delivery.get(job)['attempt_id']))
        used += inner.used
    return attempts, used


@pytest.mark.asyncio
async def test_the_worker_sends_by_the_route_that_cost_less_per_delivered_fax(multi):
    from api.app.outbound_worker import SubmissionReceipt
    from api.app.routing.capture import CostRecorder
    from api.app.routing.delivered_store import DeliveredEvidence
    from api.app.routing.fallback import FallbackScheduler
    from api.app.routing.plan import RoutePlanner
    from api.app.routing.recommendations import sending_recommendations
    configuration, delivery, routes, snapshot = multi
    number = '+12025550123'
    # The rate cards rank SignalWire first: $0.0095 a minute against Phaxio's $0.07 a page.
    routes.update_destination(number, preferred_route='signalwire')
    sent, used = await _send(multi, [SubmissionReceipt(f'SW{index}', 'success') for index in range(4)]
                             + [SubmissionReceipt('SW4', 'failed')])
    assert used == ['signalwire'] * 5
    CostRecorder(routes).step()
    # SignalWire charged $0.30 for each call, the failed one too: $1.50 for 4 delivered faxes.
    for index, (_, attempt) in enumerate(sent):
        assert routes.ingest_charge(attempt, provider_id='signalwire', charge_id=f'SW{index}',
                                    amount_micros=300_000, currency='USD', final=True) == 'new'
    routes.update_destination(number, preferred_route='phaxio')
    _, used = await _send(multi, [SubmissionReceipt(f'PX{index}', 'success') for index in range(3)])
    assert used == ['phaxio'] * 3
    CostRecorder(routes).step()
    figures = DeliveredEvidence(routes).for_destination(number)
    assert (figures['signalwire'].per_delivered_micros, figures['signalwire'].estimate) == (375_000, False)
    # Phaxio has no charges yet: 3 pages at $0.07 each, estimated from its rate card.
    assert (figures['phaxio'].per_delivered_micros, figures['phaxio'].estimate) == (210_000, True)

    # While the administrator prefers SignalWire, Faxbot says what Phaxio would have saved.
    routes.update_destination(number, preferred_route='signalwire')
    revision = configuration.read().active
    advice = sending_recommendations(routes, revision, 'phaxio')
    assert [(item['number'], item['current']['route'], item['suggested']['route']) for item in advice] == [
        (number, 'signalwire', 'phaxio')]
    assert advice[0]['chosen_by_you'] is True and advice[0]['saving_per_fax'] == {'currency': 'USD', 'amount': '0.165'}
    assert advice[0]['sentence'] == ('Phaxio cost about $0.21 per delivered fax to this number over the last 30 '
                                     'days. SignalWire, which you chose, cost $0.38.')
    routes.update_destination(number, preferred_route=None)
    assert sending_recommendations(routes, revision, 'phaxio') == []

    # With no preferred route, the evidence beats the rate cards on the real send path.
    [(job, attempt)], used = await _send(multi, [SubmissionReceipt('PX4', 'in_progress')])
    assert used == ['phaxio']
    decision = routes.decision(attempt)
    assert (decision['route'], decision['route_reason']) == ('phaxio', 'cheapest_delivered')
    plan = RoutePlanner(routes).plan(to_number=number, bound='phaxio', values=revision.values, pages=3,
                                     alternates=True)
    assert [(choice.route.key, choice.reason) for choice in plan.choices] == [
        ('phaxio', 'cheapest_delivered'), ('signalwire', 'alternative')]
    assert explain(plan.first) == ('Phaxio cost about $0.21 per delivered fax to this number over the last 30 days, '
                                   'the cheapest of 2 routes.')
    # A definite failure would fall back to the next route in the same order.
    assert FallbackScheduler(delivery, routes).next_route(job, attempt, 'phaxio').route.key == 'signalwire'
    # Sent details: only the reason is stored, so the sentence carries no amount.
    from api.app.routing.carriers import CarrierChargeStore
    from api.app.routing.http import _cost_view
    from api.app.routing.spending import Spending
    cost = _cost_view(Spending(routes, CarrierChargeStore(configuration.engine)).job(job))
    assert (cost['route'], cost['route_reason']) == ('phaxio', 'cheapest_delivered')
    assert cost['route_explanation'] == 'The cheapest route per delivered fax to this number over the last 30 days.'


def test_faxes_sent_together_and_attempts_still_in_progress_are_not_counted_twice(multi):
    """A fax that rode in another fax's call keeps a pending cost row; only the call that placed it counts."""
    from datetime import timedelta
    from uuid import uuid4
    from api.app.routing.delivered_store import DeliveredEvidence
    _, _, routes, _ = multi
    now = datetime.utcnow()
    rows = [('success', 5_000, 0), ('pending', None, 0), ('failed', 10_000, 0), ('success', 1, 31)]
    jobs = [accept(multi) for _ in rows]
    with routes.engine.begin() as connection:
        for index, (job, (outcome, reported, days)) in enumerate(zip(jobs, rows)):
            attempt = uuid4().hex
            connection.execute(routes.attempts.insert().values(
                id=attempt, job_id=job, sequence=1, phase='success' if outcome == 'pending' else outcome,
                created_at=now, submitted_at=now, completed_at=now))
            # The last one is older than the window: no longer evidence.
            connection.execute(routes.costs.insert().values(
                id=attempt, job_id=job, destination='+12025550123', route='signalwire', route_reason='configured',
                provider_id='signalwire', outcome=outcome, reported_cost_micros=reported,
                reported_currency='USD' if reported else None, billing_checks=0,
                created_at=now - timedelta(days=days, minutes=index), updated_at=now))
    route = DeliveredEvidence(routes).for_destination('+12025550123', timing=True)['signalwire']
    assert (route.attempts, route.delivered, route.failed, route.cost_micros) == (2, 1, 1, 15_000)
    assert route.per_delivered_micros == 15_000 and route.average_pages == 3.0 and route.average_seconds is None


def _finished(multi, route, outcomes, *, reported=None, estimated=None, number='+12025550123'):
    """Finished attempts on ``route`` with their cost rows, as the cost recorder and billing leave them."""
    from uuid import uuid4
    _, _, routes, _ = multi
    now = datetime.utcnow()
    jobs = [accept(multi, number) for _ in outcomes]
    with routes.engine.begin() as connection:
        for job, outcome in zip(jobs, outcomes):
            attempt = uuid4().hex
            connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase=outcome,
                                                               created_at=now, submitted_at=now, completed_at=now))
            connection.execute(routes.costs.insert().values(
                id=attempt, job_id=job, destination=number, route=route, route_reason='preferred', provider_id=route,
                outcome=outcome, estimated_cost_micros=None if estimated is None else parse_amount(estimated),
                currency=None if estimated is None else 'USD',
                reported_cost_micros=None if reported is None else parse_amount(reported),
                reported_currency=None if reported is None else 'USD', billing_checks=0, created_at=now,
                updated_at=now))


def test_a_flat_plan_is_suggested_only_over_a_metered_route_you_chose(multi):
    from api.app.routing.recommendations import sending_recommendations
    configuration, _, routes, _ = multi
    number, revision = '+12025550123', configuration.read().active
    # Phaxio is a flat monthly plan here; SignalWire charges by the minute.
    routes.replace_cards([card('phaxio', monthly='10'), card('signalwire', minute='0.0095')])
    _finished(multi, 'signalwire', ['success'] * 4 + ['failed'], reported='0.10')
    _finished(multi, 'phaxio', ['success'] * 2, estimated='0')
    routes.update_destination(number, preferred_route='signalwire')
    # Two delivered faxes through the plan are not enough evidence yet.
    assert sending_recommendations(routes, revision, 'phaxio') == []
    _finished(multi, 'phaxio', ['success'], estimated='0')
    [item] = sending_recommendations(routes, revision, 'phaxio')
    assert (item['kind'], item['current']['route'], item['suggested']['route']) == ('plan', 'signalwire', 'phaxio')
    assert item['saving_per_fax'] is None and item['suggested']['cost_text'] == 'Included in your plan'
    assert item['sentence'] == ('Your Phaxio plan already includes faxes to this number. '
                                'SignalWire cost $0.13 per delivered fax here over the last 30 days.')
    # With no preferred route, the plan already goes first ("included"): nothing to suggest, and never the
    # metered route over the plan.
    routes.update_destination(number, preferred_route=None)
    assert sending_recommendations(routes, revision, 'phaxio') == []
    # The chosen metered route has no figure here (nothing sent on it): the plan is suggested without an amount.
    other = '+12025550199'
    _finished(multi, 'phaxio', ['success'] * 3, estimated='0', number=other)
    routes.update_destination(other, preferred_route='signalwire')
    [item] = [entry for entry in sending_recommendations(routes, revision, 'phaxio') if entry['number'] == other]
    assert (item['kind'], item['current'], item['current_label']) == ('plan', None, 'SignalWire')
    assert item['sentence'] == 'Your Phaxio plan already includes faxes to this number.'
    # A plan that often fails to this number is not suggested.
    routes.update_destination(number, preferred_route='signalwire')
    _finished(multi, 'phaxio', ['failed'] * 3)
    assert [entry['number'] for entry in sending_recommendations(routes, revision, 'phaxio')] == [other]


# -- the HTTP routes and `faxbot`, over the real application -----------------------------

@pytest.fixture
def routed_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for client in _serve(monkeypatch, tmp_path, FAX_OUTBOUND_ROUTES='signalwire'):
        yield Cli(client)


def _charged_faxes(cli, number, route, outcomes, amount, *, call_seconds=None):
    """Faxes sent through the API (held in test mode), each with one finished attempt charged ``amount``."""
    from datetime import timedelta
    from uuid import uuid4
    import app.main as main_module
    from app.routing.store import RouteStore
    from app.sip_calls import SipCallRecords
    store = main_module.app.state.configuration_runtime.manager.store
    routes, now = RouteStore(store.engine), datetime.utcnow()
    jobs = []
    for _ in outcomes:
        sent = cli.client.post('/fax', headers={'X-API-Key': cli_bootstrap()}, data={'to': number},
                               files={'file': ('note.txt', b'Synthetic\n', 'text/plain')})
        assert sent.status_code == 202, sent.text
        jobs.append(sent.json()['id'])
    with store.engine.begin() as connection:
        calls = SipCallRecords(store.engine).table
        for index, (job, outcome) in enumerate(zip(jobs, outcomes)):
            attempt, start = uuid4().hex, now - timedelta(hours=index + 1)
            connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase=outcome,
                                                               created_at=start, submitted_at=start,
                                                               completed_at=start + timedelta(seconds=70)))
            connection.execute(routes.costs.insert().values(
                id=attempt, job_id=job, destination=number, route=route, route_reason='preferred', provider_id=route,
                outcome=outcome, billed_pages=2 if outcome == 'success' else 0, reported_cost_micros=parse_amount(amount),
                reported_currency='USD', billing_checks=0, created_at=start, updated_at=start))
            if call_seconds is not None:
                connection.execute(calls.insert().values(
                    id=uuid4().hex, direction='outbound', call_id=attempt, job_id=job, attempt_id=attempt,
                    trunk_preset=None, did=None, caller=None, called=number, started_at=start, answered_at=start,
                    ended_at=start + timedelta(seconds=call_seconds), disposition='answered',
                    connected_seconds=call_seconds, t38='yes', pages=2, fax_status='SUCCESS', fax_preference=0,
                    created_at=start, updated_at=start))
    return jobs


def test_recipients_and_recommendations_show_cost_per_delivered_fax(routed_cli, tmp_path):
    import json
    cli, number = routed_cli, '+12025550123'
    cards = tmp_path / 'cards.json'
    cards.write_text(json.dumps({'cards': [
        {'provider_id': 'phaxio', 'label': 'Phaxio', 'per_page': '0.07', 'captured_on': '2026-10-03T00:00:00'},
        {'provider_id': 'signalwire', 'label': 'SignalWire', 'per_minute': '0.0095', 'captured_on': '2026-10-03T00:00:00'}]}))
    assert cli('costs', 'rate-cards', '--replace', cards).exit_code == 0
    empty = cli('costs', 'recommendations')
    assert empty.exit_code == 0 and 'Sending' in empty.stdout
    assert 'Nothing to suggest yet.' in ' '.join(empty.stdout.split())
    # SignalWire: 4 of 5 delivered at $0.10 a call; Phaxio: 3 of 3 at $0.07. SignalWire is the chosen route.
    sent = _charged_faxes(cli, number, 'signalwire', ['success'] * 4 + ['failed'], '0.10', call_seconds=65)
    _charged_faxes(cli, number, 'phaxio', ['success'] * 3, '0.07')
    assert cli('recipients', 'set', number, '--preferred-route', 'signalwire').exit_code == 0
    # Sent details say why the fax went by its route, from the reason recorded when Faxbot chose it.
    response = cli.client.get(f'/routing/faxes/{sent[0]}/cost', headers={'X-API-Key': cli_bootstrap()})
    assert response.status_code == 200, response.text
    cost = response.json()
    assert (cost['route'], cost['route_reason']) == ('signalwire', 'preferred')
    assert cost['route_explanation'] == 'You chose this route for this number.'
    details = ' '.join(cli('sent', 'show', sent[0]).stdout.split())
    assert 'Route SignalWire' in details and 'Why this route You chose this route for this number.' in details

    view = cli.json('recipients', 'show', number)
    figures = {item['route']: item for item in view['delivered_costs']}
    assert [item['route'] for item in view['delivered_costs']] == ['phaxio', 'signalwire']
    assert figures['signalwire']['cost_per_delivered'] == {'currency': 'USD', 'amount': '0.125'}
    assert (figures['signalwire']['delivered'], figures['signalwire']['attempts'],
            figures['signalwire']['delivered_percent']) == (4, 5, 80)
    assert figures['signalwire']['average_connected_seconds'] == 65 and figures['phaxio']['average_pages'] == 1.0
    assert figures['phaxio']['basis_text'] == '3 faxes billed' and figures['phaxio']['enough_evidence'] is True
    shown = ' '.join(cli('recipients', 'show', number).stdout.split())
    assert 'Cost per delivered fax, last 30 days' in shown and '4 of 5' in shown and '1 minute 5 seconds' in shown
    listed = ' '.join(cli('recipients', 'list').stdout.split())
    assert 'Per delivered fax' in listed and 'Phaxio: $0.07; SignalWire: $0.13' in listed

    advice = cli.json('costs', 'recommendations')['sending']
    assert [(item['number'], item['current']['route'], item['suggested']['route']) for item in advice['items']] == [
        (number, 'signalwire', 'phaxio')]
    assert advice['items'][0]['saving_per_fax'] == {'currency': 'USD', 'amount': '0.055'}
    human = ' '.join(cli('costs', 'recommendations').stdout.split())
    assert advice['items'][0]['sentence'] in human
    assert f'faxbot recipients set {number} --preferred-route phaxio' in human and '$0.055' in human

    # Back to automatic: Faxbot sends by the cheaper route and says why.
    assert cli('recipients', 'set', number, '--preferred-route', 'automatic').exit_code == 0
    first = cli.json('recipients', 'show', number)['recommended_routes'][0]
    assert (first['route'], first['reason']) == ('phaxio', 'cheapest_delivered')
    assert first['explanation'] == ('Phaxio cost $0.07 per delivered fax to this number over the last 30 days, '
                                    'the cheapest of 2 routes.')
    assert cli.json('costs', 'recommendations')['sending']['items'] == []

    # Reading recommendations needs settings:read; a sending key gets nothing.
    sender = cli.client.post('/admin/api-keys', headers={'X-API-Key': cli_bootstrap()},
                             json={'name': 'synthetic sender', 'scopes': ['fax:send']}).json()['token']
    assert cli.client.get('/routing/recommendations/sending', headers={'X-API-Key': sender}).status_code == 403
    assert cli.client.get('/routing/recommendations/sending').status_code == 401


def cli_bootstrap():
    from api.tests.test_cli import BOOTSTRAP
    return BOOTSTRAP
