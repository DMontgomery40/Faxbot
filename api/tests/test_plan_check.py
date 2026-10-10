"""Is each monthly plan worth it at your traffic? On SQLite and PostgreSQL, with synthetic faxes only.

HumbleFax's $10 unlimited plan sends to US numbers; Telnyx (the carrier trunk)
is the metered way. Money expected here is worked out from the faxes the test
creates, never read back from the code under test.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.routing.costs import RateCard, estimate_cost, format_amount, parse_amount
from api.app.routing.plan_check import plan_report
from api.app.routing.store import RouteStore
from api.tests.test_routing_http import ADMIN, scoped_key, telnyx_client  # noqa: F401 (fixture)
from api.tests.test_routing_multiprovider import accept, multi  # noqa: F401 (fixture)
from api.tests.test_schema import database  # noqa: F401 (fixture)


NOW = datetime.utcnow().replace(microsecond=0)
CLINIC, LAB = '+12025550123', '+12025550124'
TRUNK, HUMBLE = '+13035550100', '+13035550150'
CAPTURED = datetime(2026, 10, 5)


def card(provider, direction='outbound', *, minute='0', page='0', minimum=0, monthly=None):
    return RateCard(None, provider, direction, provider.title(), 'USD', parse_amount(minute), parse_amount(page), 0, 60,
                    minimum, None, CAPTURED, None if monthly is None else parse_amount(monthly))


TELNYX_OUT = card('sip-telnyx', minute='0.005', minimum=60)
TELNYX_IN = card('sip-telnyx', 'inbound', minute='0.0032', minimum=60)
HUMBLEFAX = card('humblefax', monthly='10')


def values(**changes):
    base = dict(sip_trunk_preset='telnyx', fax_default_country='US', sip_trunk_did_list=(TRUNK,),
                sip_trunk_caller_id=TRUNK, effective_outbound='humblefax', outbound_route_providers=('sip',),
                effective_inbound='sip', inbound_enabled=True, route_min_success_percent=80,
                humblefax_from_number='', efax_caller_id='', signalwire_fax_from_e164='', fs_caller_id_number='')
    return SimpleNamespace(**{**base, **changes})


@pytest.fixture
def plans(multi):  # noqa: F811
    configuration, delivery, _, snapshot = multi
    routes = RouteStore(configuration.engine, sip_preset=lambda: 'telnyx')
    routes.replace_cards([HUMBLEFAX, TELNYX_OUT, TELNYX_IN])
    # Records reach back past both windows: an old fax on the trunk, 61 days ago.
    sent(multi, routes, 'sip', LAB, ['success'], days_ago=61)
    return multi, routes


def sent(multi, routes, route, number, outcomes, *, days_ago=1.0, reported=None):  # noqa: F811
    """Finished attempts on ``route`` to ``number``, each fax 3 pages, ``days_ago`` days back."""
    when = NOW - timedelta(days=days_ago)
    jobs = [accept(multi, number) for _ in outcomes]
    with routes.engine.begin() as connection:
        for job, outcome in zip(jobs, outcomes):
            attempt = uuid4().hex
            connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase=outcome,
                                                               created_at=when, submitted_at=when, completed_at=when))
            connection.execute(routes.costs.insert().values(
                id=attempt, job_id=job, destination=number, route=route, route_reason='configured', provider_id=route,
                outcome=outcome, reported_cost_micros=None if reported is None else parse_amount(reported),
                reported_currency=None if reported is None else 'USD', billing_checks=0, created_at=when,
                updated_at=when))


def received(routes, backend, number, count, *, days_ago=1.0):
    faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=routes.engine)
    when = NOW - timedelta(days=days_ago)
    with routes.engine.begin() as connection:
        for _ in range(count):
            connection.execute(faxes.insert().values(id=uuid4().hex, from_number=CLINIC, to_number=number,
                                                     status='received', backend=backend, pages=1, created_at=when,
                                                     received_at=when, updated_at=when))


def money(micros):
    return [{'currency': 'USD', 'amount': format_amount(micros)}]


def report(routes, **changes):
    return plan_report(routes, values(**changes), now=NOW)


def test_a_plan_cheaper_than_paying_per_fax_is_kept(plans):
    multi, routes = plans
    # Phaxio is the other way here, at a synthetic $3.00 a delivered fax reported on 3 faxes to the clinic.
    routes.replace_cards([HUMBLEFAX, TELNYX_OUT, TELNYX_IN, card('phaxio', page='1.00')])
    sent(multi, routes, 'phaxio', CLINIC, ['success'] * 3, reported='3.00')
    sent(multi, routes, 'humblefax', CLINIC, ['success'] * 4)
    (plan,) = report(routes, outbound_route_providers=('phaxio',), sip_trunk_preset='')['plans']
    assert (plan['route'], plan['state'], plan['monthly_fee']) == ('humblefax', 'keep', money(10_000_000))
    latest = plan['windows'][0]
    assert (latest['sent'], latest['received'], latest['days']) == (4, 0, 30)
    assert latest['fee'] == money(10_000_000) and latest['fee_per_fax'] == money(2_500_000)
    assert latest['other_way'] == money(12_000_000) and latest['other_routes'] == ['Phaxio']
    assert plan['sentence'] == ('Keep it: HumbleFax carried 4 faxes in the last 30 days for its $10 monthly fee, about '
                                '$2.50 a fax, while Phaxio would have cost about $12.00 for the same faxes (estimate).')
    assert plan['action'] is None


def test_a_plan_dearer_than_a_reliable_route_is_worth_reviewing_with_the_difference(plans):
    multi, routes = plans
    sent(multi, routes, 'humblefax', CLINIC, ['success'] * 5)
    sent(multi, routes, 'humblefax', LAB, ['success'] * 2, days_ago=40)  # the 30 days before
    (plan,) = report(routes)['plans']
    # 3 typical pages take about 48 seconds by the shared predictor: one billed minute at $0.005, an estimate.
    each = estimate_cost(TELNYX_OUT, 3)
    assert each == 5_000
    latest, before = plan['windows']
    assert plan['state'] == 'review' and latest['other_way'] == money(5 * each)
    # HumbleFax's plan comes with its own number; keeping it means renting it from Telnyx ($1 a month).
    assert latest['number_rental'] == money(1_000_000) and latest['other_routes'] == ['Telnyx']
    assert (before['sent'], before['other_way'], before['fee_per_fax']) == (2, money(2 * each), money(5_000_000))
    assert plan['sentence'] == ('Worth reviewing: HumbleFax carried 5 faxes in the last 30 days, about $2.00 each for '
                                'its $10 monthly fee; Telnyx would have cost about $0.025 for the same faxes and $1.00 '
                                'to keep the number, $8.98 less (estimate).')
    assert plan['action'] == ('If you decide to drop the plan, fax these numbers with Telnyx instead, then cancel the '
                              'plan in your HumbleFax account. Faxbot never cancels anything for you.')
    assert plan['caveats'] == ['Before you cancel, move your HumbleFax number to Telnyx if anyone still faxes it.']


def test_a_plan_whose_number_receives_faxes_says_to_move_the_number_first(plans):
    multi, routes = plans
    sent(multi, routes, 'humblefax', CLINIC, ['success'] * 3)
    received(routes, 'humblefax', HUMBLE, 2)
    # Faxbot itself faxed the plan's number once: a test of your own number, not a caller.
    sent(multi, routes, 'sip', HUMBLE, ['success'])
    (plan,) = report(routes, humblefax_from_number=HUMBLE)['plans']
    latest = plan['windows'][0]
    assert (latest['sent'], latest['received']) == (3, 2)
    # Received faxes would arrive on the trunk once the number moves: one page, one billed minute each.
    assert latest['other_way'] == money(3 * estimate_cost(TELNYX_OUT, 3) + 2 * estimate_cost(TELNYX_IN, 1))
    assert plan['state'] == 'review'
    assert plan['caveats'][0] == (f'Your HumbleFax plan includes the fax number {HUMBLE}, which received 2 faxes in the '
                                  'last 30 days. Move it to Telnyx before you cancel, or faxes sent to it will stop '
                                  'arriving.')
    # With nothing received through it, the number is still named, with no count.
    with routes.engine.begin() as connection:
        connection.execute(sa.text('DELETE FROM inbound_faxes'))
    (plan,) = report(routes, humblefax_from_number=HUMBLE)['plans']
    assert plan['caveats'][0] == (f'Before you cancel, move your HumbleFax number, {HUMBLE}, to Telnyx if anyone still '
                                  'faxes it.')


def test_the_number_humblefax_reports_for_the_account_counts_when_none_is_configured(plans, monkeypatch):
    """from_number unset, but HumbleFax's account answer (the cached GetUser read) names the plan's number."""
    from api.app import humblefax_service
    multi, routes = plans
    sent(multi, routes, 'humblefax', HUMBLE, ['success'] * 2)  # HumbleFax faxing its own number: tests
    sent(multi, routes, 'humblefax', CLINIC, ['success'] * 3)
    reads = []
    monkeypatch.setattr(humblefax_service, 'account_numbers',
                        lambda access, secret, **kwargs: reads.append(access) or (HUMBLE,))
    (plan,) = report(routes, humblefax_access_key='synthetic-access', humblefax_secret_key='synthetic-secret')['plans']
    assert reads == ['synthetic-access']
    latest = plan['windows'][0]
    assert (latest['sent'], latest['own_numbers']) == (5, 2)
    assert latest['other_way'] == money(3 * estimate_cost(TELNYX_OUT, 3))
    assert plan['state'] == 'review'
    assert plan['caveats'][:2] == [f'Before you cancel, move your HumbleFax number, {HUMBLE}, to Telnyx if anyone '
                                   'still faxes it.', '2 of these faxes were tests to your own numbers.']
    # With no keys there is no account to ask, and nothing is read.
    reads.clear()
    (plan,) = report(routes)['plans']
    assert reads == [] and plan['windows'][0]['own_numbers'] == 0


def test_an_unknown_number_rental_is_never_counted_as_free(plans, monkeypatch):
    """The trunk carrier publishes no price for keeping the plan's number: no saving is stated, and the rent row
    says the price is not published instead of showing nothing."""
    from api.app.routing import plan_check
    multi, routes = plans
    sent(multi, routes, 'humblefax', CLINIC, ['success'] * 5)
    monkeypatch.setattr(plan_check, 'carrier_prices', lambda carrier, path=None: SimpleNamespace(rental={}))
    (plan,) = report(routes)['plans']
    latest = plan['windows'][0]
    assert latest['other_way'] == money(5 * estimate_cost(TELNYX_OUT, 3))
    assert plan['state'] == 'review'
    assert plan['sentence'] == ('Worth reviewing: HumbleFax carried 5 faxes in the last 30 days, about $2.00 each for '
                                'its $10 monthly fee; Telnyx would have cost about $0.025 for the same faxes, but '
                                'Telnyx does not publish what it charges to keep your HumbleFax number, so Faxbot '
                                "can't tell whether dropping the plan would save money (estimate).")
    assert latest['number_rental'] == [] and latest['number_rental_unpublished'] is True
    # A plan that costs less than the faxes alone would another way is kept, whatever the number costs.
    routes.replace_cards([HUMBLEFAX, TELNYX_OUT, TELNYX_IN, card('phaxio', page='1.00')])
    sent(multi, routes, 'phaxio', CLINIC, ['success'] * 3, reported='3.00')
    (plan,) = report(routes, outbound_route_providers=('phaxio',))['plans']
    assert plan['state'] == 'keep' and plan['sentence'].startswith('Keep it: HumbleFax carried 5 faxes')
    # With the carrier's published rental the row shows it and nothing says it is unpublished.
    monkeypatch.setattr(plan_check, 'carrier_prices', lambda carrier, path=None: SimpleNamespace(
        rental={'local': 1_000_000}))
    (plan,) = report(routes)['plans']
    assert plan['windows'][0]['number_rental'] == money(1_000_000)
    assert plan['windows'][0]['number_rental_unpublished'] is False


def test_an_unreliable_alternative_suggests_nothing(plans):
    multi, routes = plans
    sent(multi, routes, 'humblefax', CLINIC, ['success'] * 5)
    sent(multi, routes, 'sip', CLINIC, ['success', 'success', 'failed', 'failed', 'failed'])  # 40% delivered
    (plan,) = report(routes)['plans']
    assert plan['state'] == 'keep' and plan['action'] is None
    assert plan['windows'][0]['without_other_way'] == 5 and plan['windows'][0]['other_way'] == []
    assert plan['sentence'] == ('Keep it: no other way of faxing works reliably for 5 of the faxes HumbleFax sent in '
                                'the last 30 days.')


def test_a_flat_plan_is_never_the_free_cheaper_way(plans):
    multi, routes = plans
    # Phaxio is a $5 flat plan too: it is reviewed on its own, never offered as a $0 way for HumbleFax's faxes.
    routes.replace_cards([HUMBLEFAX, TELNYX_OUT, TELNYX_IN, card('phaxio', monthly='5')])
    sent(multi, routes, 'phaxio', CLINIC, ['success'] * 3)
    sent(multi, routes, 'humblefax', CLINIC, ['success'] * 4)
    found = {plan['route']: plan for plan in report(routes, outbound_route_providers=('phaxio', 'sip'))['plans']}
    assert sorted(found) == ['humblefax', 'phaxio']
    assert found['humblefax']['windows'][0]['other_routes'] == ['Telnyx']
    assert found['humblefax']['windows'][0]['other_way'] == money(4 * estimate_cost(TELNYX_OUT, 3))
    assert found['phaxio']['windows'][0]['other_routes'] == ['Telnyx']
    assert '$0.00 for the same faxes' not in found['humblefax']['sentence']


def test_faxes_to_your_own_numbers_are_tests_that_need_no_other_way(plans):
    multi, routes = plans
    sent(multi, routes, 'humblefax', TRUNK, ['success'] * 6)  # HumbleFax faxing the trunk's own number
    sent(multi, routes, 'humblefax', CLINIC, ['success'])
    (plan,) = report(routes)['plans']
    latest = plan['windows'][0]
    assert (latest['sent'], latest['own_numbers'], latest['without_other_way']) == (7, 6, 0)
    assert latest['other_way'] == money(estimate_cost(TELNYX_OUT, 3))  # only the clinic's fax is priced
    assert plan['state'] == 'review'
    assert '6 of these faxes were tests to your own numbers.' in plan['caveats']


def test_a_test_fax_that_places_a_paid_call_is_counted_as_a_cost(plans):
    """Telnyx (a trunk with a $5 monthly fee) faxed the HumbleFax number twice: tests, but HumbleFax cannot receive
    into Faxbot, so each was a paid call and is priced; a test to the trunk's own number arrives with no call."""
    multi, routes = plans
    routes.replace_cards([card('sip-telnyx', minute='0.005', minimum=60, monthly='5'), TELNYX_IN,
                          card('phaxio', page='1.00')])
    sent(multi, routes, 'sip', HUMBLE, ['success'] * 2)
    sent(multi, routes, 'sip', TRUNK, ['success'])
    sent(multi, routes, 'sip', CLINIC, ['success'] * 3)
    (plan,) = report(routes, effective_outbound='sip', outbound_route_providers=('phaxio',),
                     humblefax_from_number=HUMBLE)['plans']
    latest = plan['windows'][0]
    assert plan['route'] == 'sip'
    each = estimate_cost(card('phaxio', page='1.00'), 3)
    assert latest['other_way'] == money(5 * each)  # the clinic's 3 faxes and the 2 paid tests
    assert (latest['sent'], latest['own_numbers'], latest['paid_tests']) == (6, 3, 2)
    assert ('3 of these faxes were tests to your own numbers. The 2 sent to +13035550150 still cost a phone call, '
            'so they are counted in what the other way would cost.') in plan['caveats']


def test_one_rule_for_own_numbers_with_its_two_uses(monkeypatch):
    """A HumbleFax account number is an account number of yours, but it does not receive into this Faxbot."""
    from api.app.routing import local
    from api.app.routing.own_numbers import account_numbers, receiving_numbers
    settings = values(humblefax_from_number=HUMBLE, local_delivery_enabled=True)
    assert receiving_numbers(settings) == {TRUNK} == local.own_numbers(settings)
    assert {TRUNK, HUMBLE} <= account_numbers(settings)
    assert '+13035550199' in account_numbers(settings, {'humblefax': ('+13035550199',)})
    assert not local.applies(settings, HUMBLE) and local.applies(settings, TRUNK)


def test_too_little_history_is_one_sentence(plans):
    multi, routes = plans
    with routes.engine.begin() as connection:  # records start 4 days ago, not 61
        connection.execute(routes.costs.delete())
    sent(multi, routes, 'humblefax', CLINIC, ['success'] * 2, days_ago=4)
    (plan,) = report(routes)['plans']
    assert plan['state'] == 'too_little_history' and plan['action'] is None
    assert plan['sentence'] == ('Not enough history yet to judge your $10 HumbleFax plan: Faxbot has records for 4 '
                                'days, and HumbleFax carried 2 faxes in that time.')
    # With enough faxes over those 4 days, the fee for those days is compared, and the sentence says so.
    sent(multi, routes, 'humblefax', CLINIC, ['success'] * 4, days_ago=3)
    (plan,) = report(routes)['plans']
    assert plan['state'] == 'review' and plan['windows'][0]['fee'] == money(-(-10_000_000 * 4 * 86_400 // (30 * 86_400)))
    assert 'over the 4 days Faxbot has records for' in plan['sentence']
    assert plan['sentence'].startswith('Worth reviewing: HumbleFax carried 6 faxes over the 4 days Faxbot has records '
                                       'for, about $0.22 each for that part of its $10 monthly fee;')


def test_no_plan_says_so(plans):
    _, routes = plans
    routes.replace_cards([TELNYX_OUT, TELNYX_IN])
    result = report(routes)
    assert result['plans'] == [] and result['estimate'] is True
    assert result['empty_sentence'] == 'You pay no monthly fee for a fax service, so there is no plan to review.'


def test_the_plans_route_needs_settings_read(telnyx_client):  # noqa: F811
    response = telnyx_client.get('/routing/recommendations/plans', headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['plans'] == [] and body['days'] == 30
    assert body['empty_sentence'] == 'You pay no monthly fee for a fax service, so there is no plan to review.'
    assert telnyx_client.get('/routing/recommendations/plans',
                             headers=scoped_key(telnyx_client, ['fax:send'])).status_code == 403
