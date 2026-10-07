"""Plan budgets (M3), the contract view (B11) and other carriers' prices (B13), on SQLite and PostgreSQL.

HumbleFax's $10 unlimited plan sends to US numbers; Telnyx (the carrier
trunk) is metered and calls US toll-free numbers for nothing; Sinch charges by
the page. All numbers and faxes are synthetic. Expected money is worked out
here from the shipped prices, never read back from the code under test.
"""
from datetime import date, datetime
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.routing import plan_budget
from api.app.routing.carrier_compare import NOTHING, SWITCHING, carriers, compare
from api.app.routing.costs import (Money, RateCard, attempt_cost, billed_seconds, format_amount, parse_amount,
                                   terms_cost)
from api.app.routing.plan_budget import (Budget, InvalidBudget, Usage, billing_period, budget_left, burn_down,
                                         format_budgets, marginal, normalize_budgets, order_key, pace_sentence,
                                         parse_budgets, with_entry)
from api.app.routing.plan_check import contract_report
from api.app.routing.predict import Prediction, Shape, predict_from
from api.app.routing.predict_facts import facts_for
from api.app.routing.store import RouteStore
from api.tests.test_routing_http import ADMIN, client, scoped_key  # noqa: F401 (fixture)
from api.tests.test_routing_multiprovider import accept, multi  # noqa: F401 (fixture)
from api.tests.test_schema import database  # noqa: F401 (fixture)


CAPTURED = datetime(2026, 10, 5)
LAB, CLINIC = '+12025550124', '+12025550123'
TRUNK, HUMBLE = '+13035550100', '+13035550150'
TOLL_FREE = '+18005550100'
ABROAD = '+442079460123'  # London's range Ofcom keeps for drama, never in service
OCTOBER = datetime(2026, 10, 20, 15)


def card(provider, direction='outbound', *, minute='0', page='0', minimum=0, increment=60, monthly=None):
    return RateCard(None, provider, direction, provider.title(), 'USD', parse_amount(minute), parse_amount(page), 0,
                    increment, minimum, None, CAPTURED, None if monthly is None else parse_amount(monthly, whole_digits=4))


HUMBLEFAX = card('humblefax', monthly='10')
TELNYX_OUT = card('sip-telnyx', minute='0.005', minimum=60)
TELNYX_IN = card('sip-telnyx', 'inbound', minute='0.0032', minimum=60)
SINCH = card('sinch', page='0.045')
EFAX = card('efax', monthly='18.99')  # eFax Personal: 200 pages a month, then $0.10 a page (reference_plans)


def values(**changes):
    base = dict(sip_trunk_preset='telnyx', fax_default_country='US', sip_trunk_did_list=(TRUNK,),
                sip_trunk_caller_id=TRUNK, effective_outbound='humblefax', outbound_route_providers=('sip', 'sinch'),
                effective_inbound='sip', inbound_enabled=False, route_min_success_percent=80,
                humblefax_from_number=HUMBLE, efax_caller_id='', signalwire_fax_from_e164='', fs_caller_id_number='',
                plan_budgets='', time_zone='', local_delivery_enabled=True, direct_delivery_enabled=False,
                direct_fax_number='', sip_t38_enabled=True)
    return SimpleNamespace(**{**base, **changes})


@pytest.fixture
def plans(multi):  # noqa: F811
    configuration, _, _, _ = multi
    routes = RouteStore(configuration.engine, sip_preset=lambda: 'telnyx')
    routes.replace_cards([HUMBLEFAX, TELNYX_OUT, TELNYX_IN, SINCH])
    return multi, routes


def record(multi, routes, route, number, when, *, pages=3, outcome='success', seconds=None, cost=None):  # noqa: F811
    """One finished attempt on ``route``; ``seconds`` adds the trunk's call record, ``cost`` an estimate."""
    job = accept(multi, number)
    attempt = uuid4().hex
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=routes.engine)
    with routes.engine.begin() as connection:
        connection.execute(jobs.update().where(jobs.c.id == job).values(pages=pages))
        connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase=outcome,
                                                           created_at=when, submitted_at=when, completed_at=when))
        connection.execute(routes.costs.insert().values(
            id=attempt, job_id=job, destination=number, route=route, route_reason='configured', provider_id=route,
            outcome=outcome, billed_pages=pages if outcome == 'success' else 0,
            billed_seconds=None if seconds is None else billed_seconds(routes.card_for(route), seconds),
            estimated_cost_micros=cost, currency='USD' if cost is not None else None, billing_checks=0,
            created_at=when, updated_at=when))
        if seconds is not None:
            calls = sa.Table('sip_call_records', sa.MetaData(), autoload_with=routes.engine)
            connection.execute(calls.insert().values(
                id=uuid4().hex, direction='outbound', call_id=attempt, job_id=job, attempt_id=attempt,
                started_at=when, answered_at=when, ended_at=when, connected_seconds=seconds, disposition='answered',
                t38='yes', pages=pages, fax_status='SUCCESS', fax_preference=0, created_at=when, updated_at=when))
    return attempt


def received(routes, backend, number, when, *, pages=1, count=1):
    faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=routes.engine)
    with routes.engine.begin() as connection:
        for _ in range(count):
            connection.execute(faxes.insert().values(id=uuid4().hex, from_number=CLINIC, to_number=number,
                                                     status='received', backend=backend, pages=pages, created_at=when,
                                                     received_at=when, updated_at=when))


def money(micros):
    return [{'currency': 'USD', 'amount': format_amount(micros)}]


# The setting ----------------------------------------------------------------------------------------

def test_the_budget_setting_reads_back_in_one_order_and_refuses_mistakes():
    text = ' efax:day=15, included_pages=200,page_overage=0.10 ; SIP-telnyx:commitment=50,included_minutes=1000;' \
           'humblefax:faxes=none,pages=2000'
    assert normalize_budgets(text) == ('efax:day=15,included_pages=200,page_overage=0.10; '
                                       'humblefax:pages=2000,faxes=none; sip:included_minutes=1000,commitment=50.00')
    parsed = parse_budgets(text)
    assert parsed['humblefax'] == {'faxes': None, 'pages': 2000}
    assert parsed['sip'] == {'commitment': 50_000_000, 'included_minutes': 1000}
    assert format_budgets(parsed) == normalize_budgets(text)
    assert normalize_budgets('') == ''
    assert with_entry('humblefax:pages=200', 'efax', {'included_pages': 150, 'day': 3}) == \
        'efax:day=3,included_pages=150; humblefax:pages=200'
    assert with_entry('efax:day=3; humblefax:pages=200', 'efax', None) == 'humblefax:pages=200'
    refusals = {
        'humblefax': 'Write each plan budget as the route, a colon and its values, such as '
                     'humblefax:pages=200,faxes=50,day=1.',
        'humblefax:pages=0': 'Budgets and allowances are whole numbers from 1 to 1,000,000, or none.',
        'humblefax:pages=lots': 'Budgets and allowances are whole numbers from 1 to 1,000,000, or none.',
        'humblefax:day=32': 'The billing day is a day of the month from 1 to 31.',
        'humblefax:colour=blue': 'Use only pages, faxes, day, included_pages, page_overage, included_minutes, '
                                 'commitment in a plan budget.',
        'humblefax:pages=1,pages=2': 'pages is given twice for humblefax.',
        'humblefax:pages=1; humblefax:faxes=2': 'The budget for humblefax is given twice.',
        'efax:page_overage=ten': 'Enter amounts as numbers, such as 0.10 or 50.',
    }
    for wrong, sentence in refusals.items():
        with pytest.raises(InvalidBudget) as refused:
            parse_budgets(wrong)
        assert str(refused.value) == sentence


def test_the_setting_is_checked_where_every_setting_is():
    from api.app.config_values import ConfigurationValues
    saved = ConfigurationValues.model_validate({'FAX_PLAN_BUDGETS': 'humblefax:faxes=40, pages=300'})
    assert saved.plan_budgets == 'humblefax:pages=300,faxes=40'
    with pytest.raises(ValueError):
        ConfigurationValues.model_validate({'FAX_PLAN_BUDGETS': 'humblefax:pages=-1'})


# The billing period ---------------------------------------------------------------------------------

def test_the_period_starts_on_the_billing_day_by_the_installation_clock():
    # Denver is six hours behind UTC in October: the 15th starts at 06:00 UTC.
    before = billing_period(datetime(2026, 10, 15, 5, 59, 59), 15, 'America/Denver')
    at = billing_period(datetime(2026, 10, 15, 6, 0), 15, 'America/Denver')
    assert (before.first_day, before.next_day) == (date(2026, 9, 15), date(2026, 10, 15))
    assert (at.first_day, at.next_day) == (date(2026, 10, 15), date(2026, 11, 15))
    assert (at.start, at.end) == (datetime(2026, 10, 15, 6), datetime(2026, 11, 15, 7))  # standard time in November
    # Billed on the 31st: February's period starts on its last day, and March's on the 31st.
    feb = billing_period(datetime(2026, 2, 28, 12), 31)
    assert (feb.first_day, feb.next_day) == (date(2026, 2, 28), date(2026, 3, 31))
    late_jan = billing_period(datetime(2026, 2, 27, 12), 31)
    assert (late_jan.first_day, late_jan.next_day) == (date(2026, 1, 31), date(2026, 2, 28))
    leap = billing_period(datetime(2028, 2, 29, 12), 30)
    assert (leap.first_day, leap.next_day) == (date(2028, 2, 29), date(2028, 3, 30))
    january = billing_period(datetime(2027, 1, 3), 5)
    assert (january.first_day, january.next_day) == (date(2026, 12, 5), date(2027, 1, 5))
    assert plan_budget.billing_day_text(1) == 'the 1st' and plan_budget.billing_day_text(22) == 'the 22nd'
    assert plan_budget.billing_day_text(31) == "the 31st (or the month's last day)"


# M3: the synthetic month ---------------------------------------------------------------------------------

def route_order(routes, settings, number, now, pages=3):
    """The order "cheapest marginal" gives: the predictor's cost, and each plan's budget."""
    found = []
    for position, route in enumerate(('sip', 'humblefax', 'sinch')):
        facts = facts_for(route, number, now=now, engine=routes.engine, values=settings)
        prediction = predict_from(facts, Shape(pages, None, 'standard', 'normal'))
        left = budget_left(route, now, engine=routes.engine, values=settings)
        found.append((plan_budget.order_key(marginal(left, pages, prediction), position), route))
    return [route for _, route in sorted(found)]


def test_a_synthetic_month_sends_toll_free_by_telnyx_and_the_rest_by_humblefax_until_the_budget(plans):
    multi, routes = plans
    settings = values(plan_budgets='humblefax:pages=30,faxes=none,day=1')
    taken, expected, used = [], [], 0
    for day in range(1, 31):
        now = datetime(2026, 10, day, 15)
        for number in [LAB] + ([TOLL_FREE] if day % 5 == 0 else []):
            order = route_order(routes, settings, number, now)
            taken.append((day, number, order))
            record(multi, routes, order[0], number, now)
            # Toll-free calls are free on Telnyx and use no budget; the rest go by HumbleFax while 3 more pages fit
            # in the 30-page budget, then Telnyx, then Sinch, with HumbleFax last once over its budget.
            if number == TOLL_FREE:
                expected.append((day, number, ['sip', 'humblefax', 'sinch'] if used < 30 else
                                 ['sip', 'sinch', 'humblefax']))
            elif used + 3 <= 30:
                expected.append((day, number, ['humblefax', 'sip', 'sinch']))
                used += 3
            else:
                expected.append((day, number, ['sip', 'sinch', 'humblefax']))
    assert taken == expected
    assert [day for day, number, order in taken if order[0] == 'humblefax'] == list(range(1, 11))
    left = budget_left('humblefax', datetime(2026, 10, 31, 12), engine=routes.engine, values=settings)
    assert (left.used.sent_faxes, left.used.pages, left.pages_left, left.over, left.state) == (10, 30, 0, True,
                                                                                               'over_budget')
    assert left.sentence == ('HumbleFax has carried 30 pages and 10 faxes since 1 October, past your normal-use '
                             'budget of 30 pages; the budget starts again on 1 November.')
    # The billing day: on 1 November the budget starts again.
    assert route_order(routes, settings, LAB, datetime(2026, 11, 1, 9)) == ['humblefax', 'sip', 'sinch']
    assert route_order(routes, settings, TOLL_FREE, datetime(2026, 11, 1, 9)) == ['sip', 'humblefax', 'sinch']
    # The predictor reads the same period and budget.
    facts = facts_for('humblefax', LAB, now=datetime(2026, 10, 31, 12), engine=routes.engine, values=settings)
    assert (facts.plan.pages, facts.plan.faxes, facts.plan.page_budget) == (30, 10, 30)


def test_uncertain_sends_and_received_faxes_count_and_failed_ones_do_not(plans):
    multi, routes = plans
    settings = values()
    record(multi, routes, 'humblefax', LAB, OCTOBER, pages=4)
    record(multi, routes, 'humblefax', LAB, OCTOBER, pages=5, outcome='uncertain')
    record(multi, routes, 'humblefax', LAB, OCTOBER, pages=6, outcome='failed')
    record(multi, routes, 'humblefax', LAB, datetime(2026, 9, 30, 23), pages=7)  # the period before
    received(routes, 'humblefax', HUMBLE, OCTOBER, pages=2, count=3)
    left = budget_left('humblefax', OCTOBER, engine=routes.engine, values=settings)
    assert (left.used.sent_faxes, left.used.sent_pages, left.used.received_faxes, left.used.received_pages) == (
        2, 9, 3, 6)
    # Faxbot's own start for HumbleFax: 200 pages and 50 faxes from the 1st.
    assert (left.budget.pages, left.budget.faxes, left.budget.day, left.budget.source) == (200, 50, 1, 'default')
    assert (left.pages_left, left.faxes_left) == (185, 45)
    assert left.sentence == ('HumbleFax has carried 15 pages and 5 faxes since 1 October; 185 pages and 45 faxes of '
                             'your normal-use budget are left until 1 November.')


# Allowance plans ---------------------------------------------------------------------------------------

def test_an_allowance_plan_charges_its_overage_price_past_the_allowance(plans):
    multi, routes = plans
    routes.replace_cards([EFAX, TELNYX_OUT, TELNYX_IN])
    settings = values()
    for _ in range(3):
        record(multi, routes, 'efax', LAB, OCTOBER, pages=60)
    received(routes, 'efax', '+13035550160', OCTOBER, pages=9, count=2)
    left = budget_left('efax', OCTOBER, engine=routes.engine, values=settings)
    budget = left.budget
    assert (budget.included_pages, budget.page_overage_micros, budget.source, budget.flat) == (
        200, 100_000, 'published', True)
    assert budget.sentence == "eFax's published plan includes 200 pages a month and charges $0.10 for each page past them."
    assert (left.used.pages, left.allowance_left, left.overage_pages, left.state) == (198, 2, 0, 'within')
    assert left.sentence == 'eFax has used 198 of the 200 pages your plan includes since 1 October; 2 are left until ' \
                            '1 November.'
    one = marginal(left, 3)
    assert (one.cost, one.uses_budget, one.over_budget) == (Money(100_000, 'USD'), True, False)
    assert one.sentence == '1 page past what your eFax plan includes, at $0.10 a page.'
    fits = marginal(left, 2)
    assert fits.cost == Money(0, 'USD')
    assert fits.sentence == 'Included in the 200 pages your eFax plan includes; 0 will be left until 1 November.'
    record(multi, routes, 'efax', LAB, OCTOBER, pages=10)
    past = budget_left('efax', OCTOBER, engine=routes.engine, values=settings)
    assert (past.overage_pages, past.overage_micros, past.state) == (8, 800_000, 'past_allowance')
    assert past.sentence == ('eFax has used 208 pages since 1 October, 8 past the 200 your plan includes, about $0.80 '
                             'in extra pages so far (estimate); the allowance starts again on 1 November.')
    assert marginal(past, 4).cost == Money(400_000, 'USD')
    # An allowance you set yourself replaces the published one, key by key; an unknown overage price stays unknown.
    own = values(plan_budgets='efax:included_pages=500,day=10')
    mine = budget_left('efax', OCTOBER, engine=routes.engine, values=own)
    assert (mine.budget.included_pages, mine.budget.page_overage_micros, mine.budget.source) == (500, 100_000, 'set')
    assert mine.budget.sentence == 'You set this budget for eFax.'
    assert mine.period.first_day == date(2026, 10, 10)
    routes.replace_cards([card('efax', monthly='25'), TELNYX_OUT])
    unknown = budget_left('efax', OCTOBER, engine=routes.engine, values=values(plan_budgets='efax:included_pages=100'))
    assert (unknown.overage_pages, unknown.overage_micros) == (108, None)
    assert unknown.sentence == ('eFax has used 208 pages since 1 October, 108 past the 100 your plan includes, the '
                                'price of extra pages is not known; the allowance starts again on 1 November.')
    assert marginal(unknown, 2).cost is None
    assert marginal(unknown, 2).sentence == ('2 pages of this fax would go past what your eFax plan includes, and '
                                             'the price of extra pages is not known.')


# The burn-down ----------------------------------------------------------------------------------------------

def test_the_burn_down_counts_each_local_day_and_starts_again_on_the_billing_day(plans):
    multi, routes = plans
    settings = values(time_zone='America/Denver', plan_budgets='humblefax:pages=40,faxes=none,day=15')
    record(multi, routes, 'humblefax', LAB, datetime(2026, 10, 15, 5), pages=9)   # 14 Oct, 11 PM in Denver
    record(multi, routes, 'humblefax', LAB, datetime(2026, 10, 15, 7), pages=2)   # 15 Oct, 1 AM
    record(multi, routes, 'humblefax', LAB, datetime(2026, 10, 17, 3), pages=4)   # 16 Oct, 9 PM
    received(routes, 'humblefax', HUMBLE, datetime(2026, 10, 17, 18), pages=1)  # 17 Oct, noon
    now = datetime(2026, 10, 18, 2)                                               # 17 Oct, 8 PM
    left = budget_left('humblefax', now, engine=routes.engine, values=settings)
    rows = burn_down(routes.engine, left, now=now, zone_name='America/Denver')
    assert [(row.day.day, row.pages, row.faxes) for row in rows] == [(15, 2, 1), (16, 6, 2), (17, 7, 3)]
    assert left.used.pages == 7 and left.pages_left == 33
    # 7 pages in 3 days of a 31-day period: about 73 pages by 15 November, past a 40-page budget.
    assert pace_sentence(left, rows) == ('At this pace HumbleFax will carry about 73 pages by 15 November, past your '
                                         'normal-use budget of 40.')
    assert pace_sentence(left, rows[:1]) is None
    roomy = budget_left('humblefax', now, engine=routes.engine,
                        values=values(time_zone='America/Denver', plan_budgets='humblefax:pages=400,day=15'))
    assert pace_sentence(roomy, rows) == ('At this pace HumbleFax will carry about 73 pages by 15 November, within '
                                          'your normal-use budget of 400.')


# B11: the contract view ---------------------------------------------------------------------------------------

def test_the_contract_view_says_whose_bill_each_side_falls_on_between_your_own_accounts(plans):
    multi, routes = plans
    settings = values()
    each = attempt_cost(TELNYX_OUT, seconds=50, pages=3, delivered=True)
    assert each == 5_000
    for _ in range(2):
        record(multi, routes, 'sip', HUMBLE, OCTOBER, seconds=50, cost=each)  # Telnyx calls your HumbleFax number
    record(multi, routes, 'humblefax', TRUNK, OCTOBER, pages=2)              # HumbleFax faxes your Telnyx number
    record(multi, routes, 'humblefax', LAB, OCTOBER, pages=5)
    report = contract_report(routes, settings, now=OCTOBER, accounts={})
    (plan,) = report['plans']
    assert (plan['route'], plan['name'], plan['kind'], plan['monthly_fee']) == ('humblefax', 'HumbleFax', 'flat',
                                                                                money(10_000_000))
    assert plan['used']['sent_faxes'] == 2 and plan['used']['pages'] == 7
    assert plan['left'] == {'pages': 193, 'faxes': 48, 'allowance': None, 'minutes': None, 'commitment': []}
    assert plan['committed'] == money(10_000_000) and plan['bill_so_far'] == money(10_000_000)
    assert plan['bill_sentence'] == 'Committed this period with HumbleFax: the $10 plan fee; nothing past it so far.'
    assert plan['budget']['sentence'] == (
        "Faxbot starts HumbleFax at 200 pages and 50 faxes a month because HumbleFax's terms keep unlimited faxing for "
        'normal, individual use without naming a number; this is a cautious start, not a limit HumbleFax has '
        'promised to accept.')
    assert plan['count_sentence'] == ("HumbleFax counts each document page or each 60 seconds on the line, whichever "
                                      "is more, so its own count can be higher than Faxbot's.")
    sent, came = plan['own_accounts']
    assert (sent['direction'], sent['faxes'], sent['pages'], sent['sending_bill'], sent['receiving_bill']) == (
        'sent', 1, 2, 'HumbleFax', 'Telnyx')
    assert sent['sentence'] == ('1 fax from HumbleFax went to your Telnyx number: sending it used your plan with '
                                'HumbleFax, and receiving it is on your Telnyx bill.')
    assert (came['direction'], came['faxes'], came['sending_bill'], came['receiving_bill'], came['sending_cost']) == (
        'received', 2, 'Telnyx', 'HumbleFax', money(2 * each))
    assert came['sentence'] == ('2 faxes from Telnyx came to your HumbleFax number: Telnyx billed the calls (about '
                                '$0.01, estimate), and HumbleFax received them inside your plan.')
    # A fax whose cost Faxbot does not know yet is never priced as nothing.
    record(multi, routes, 'sip', HUMBLE, OCTOBER, seconds=None, cost=None)
    (plan,) = contract_report(routes, settings, now=OCTOBER, accounts={})['plans']
    assert plan['own_accounts'][1]['sending_cost'] == []
    assert plan['own_accounts'][1]['sentence'] == ('3 faxes from Telnyx came to your HumbleFax number: Telnyx billed '
                                                   'the calls, at a cost Faxbot does not know yet, and HumbleFax '
                                                   'received them inside your plan.')


def test_the_contract_view_covers_allowances_commitments_and_minute_bundles(plans):
    multi, routes = plans
    routes.replace_cards([EFAX, TELNYX_OUT, TELNYX_IN])
    settings = values(efax_caller_id='+13035550160', plan_budgets='sip:commitment=0.02')
    record(multi, routes, 'efax', LAB, OCTOBER, pages=150)
    record(multi, routes, 'efax', LAB, OCTOBER, pages=60)
    record(multi, routes, 'sip', '+13035550160', OCTOBER, pages=4, seconds=100, cost=10_000)  # 2 minutes at $0.005
    record(multi, routes, 'sip', LAB, OCTOBER, pages=1, seconds=40, cost=5_000)
    report = contract_report(routes, settings, now=OCTOBER, accounts={})
    efax, trunk = report['plans']
    assert report['plan_budgets'] == 'sip:commitment=0.02'
    assert (efax['kind'], efax['used']['pages'], efax['left']['allowance'], efax['overage']['pages']) == (
        'allowance', 210, -10, 10)
    assert efax['overage']['cost'] == money(1_000_000) and efax['bill_so_far'] == money(19_990_000)
    assert efax['bill_sentence'] == ('Committed this period with eFax: the $18.99 plan fee, plus about $1.00 past it so '
                                     'far (estimate).')
    assert efax['own_accounts'][0]['sentence'] == ('1 fax from Telnyx came to your eFax number: Telnyx billed the '
                                                   'calls (about $0.01, estimate), and their 4 pages count against '
                                                   'your eFax allowance.')
    assert (trunk['route'], trunk['kind'], trunk['used']['spend'], trunk['left']['commitment']) == (
        'sip', 'commitment', money(15_000), money(5_000))
    assert trunk['sentence'] == ('You have spent about $0.015 with Telnyx since 1 October; $0.005 of your $0.02 '
                                 'monthly commitment is left until 1 November (estimate).')
    assert trunk['bill_sentence'] == ('Committed this period with Telnyx: your $0.02 monthly commitment; about $0.015 '
                                      'of the commitment used so far (estimate).')
    # A minute bundle on the trunk: 2 + 1 minutes sent this period.
    bundle = contract_report(routes, values(plan_budgets='sip:included_minutes=2'), now=OCTOBER, accounts={})
    trunk = bundle['plans'][-1]
    assert (trunk['kind'], trunk['used']['minutes'], trunk['left']['minutes'], trunk['overage']['minutes']) == (
        'minutes', 3, -1, 1)
    assert trunk['sentence'] == ('Telnyx has used 3 minutes since 1 October, 1 past the 2 your plan includes, about '
                                 '$0.005 extra so far (estimate); the minutes start again on 1 November.')


def test_a_metered_plan_with_a_monthly_fee_shows_its_fee_and_keeps_its_price_per_fax(plans):
    multi, routes = plans
    trunk_plan = card('sip-telnyx', minute='0.005', minimum=60, monthly='25')
    routes.replace_cards([trunk_plan, SINCH])
    settings = values(effective_outbound='sip', outbound_route_providers=('sinch',))
    record(multi, routes, 'sip', LAB, OCTOBER, pages=2, seconds=50, cost=5_000)
    (plan,) = contract_report(routes, settings, now=OCTOBER, accounts={})['plans']
    assert (plan['route'], plan['kind'], plan['committed'], plan['state']) == ('sip', 'metered', money(25_000_000),
                                                                               'no_limit')
    assert plan['budget']['sentence'] == ('Telnyx charges for each fax on top of its monthly fee, so Faxbot sets no '
                                          'normal-use budget for it.')
    assert plan['sentence'] == 'Telnyx has carried 2 pages and 1 fax since 1 October; the counts start again on ' \
                               '1 November.'
    assert plan['bill_sentence'] == 'Committed this period with Telnyx: the $25 plan fee; nothing past it so far.'
    left = budget_left('sip', OCTOBER, engine=routes.engine, values=settings)
    each = marginal(left, 2, prediction(5_000))
    # The fee is paid whatever is sent: one more fax costs the trunk's own price.
    assert (each.cost, each.uses_budget, each.over_budget) == (Money(5_000, 'USD'), False, False)


def test_with_no_plan_the_contract_view_says_so(plans):
    _, routes = plans
    routes.replace_cards([TELNYX_OUT, SINCH])
    report = contract_report(routes, values(effective_outbound='sip', outbound_route_providers=('sinch',)),
                             now=OCTOBER, accounts={})
    assert report['plans'] == []
    assert report['empty_sentence'] == ('You pay no monthly fee for a fax service and set no allowance or commitment, '
                                        'so there is no plan to show.')


# Every explanation sentence ------------------------------------------------------------------------------------

PERIOD = billing_period(OCTOBER, 1)


def budget(**changes):
    base = dict(route='humblefax', label='HumbleFax', pages=200, faxes=50, day=1, currency='USD',
                monthly_fee_micros=10_000_000, flat=True)
    return Budget(**{**base, **changes})


def left_for(budget_, **used):
    return plan_budget._left(budget_, PERIOD, Usage(**used))


def prediction(micros, seconds=50.0):
    cost = None if micros is None else Money(micros, 'USD')
    return Prediction(None, seconds, cost, 'Billed as 1 minute at $0.005 a minute.', False)


def test_every_explanation_sentence():
    flat = left_for(budget(), sent_faxes=4, sent_pages=12)
    assert flat.sentence == ('HumbleFax has carried 12 pages and 4 faxes since 1 October; 188 pages and 46 faxes of '
                             'your normal-use budget are left until 1 November.')
    one = marginal(flat, 3)
    assert (one.cost, one.over_budget, one.uses_budget) == (Money(0, 'USD'), False, True)
    assert one.sentence == ('Included in your HumbleFax plan; 185 pages and 45 faxes of your normal-use budget will be '
                            'left until 1 November.')
    crossing = marginal(left_for(budget(), sent_faxes=1, sent_pages=199), 3)
    assert (crossing.cost, crossing.over_budget) == (Money(0, 'USD'), True)
    assert crossing.sentence == 'Over your normal-use budget for HumbleFax until 1 November; the fax itself costs ' \
                                'nothing extra.'
    by_faxes = left_for(budget(), sent_faxes=50, sent_pages=60)
    assert by_faxes.over and by_faxes.sentence == (
        'HumbleFax has carried 60 pages and 50 faxes since 1 October, past your normal-use budget of 200 pages and '
        '50 faxes; the budget starts again on 1 November.')
    unlimited = left_for(budget(pages=None, faxes=None), sent_faxes=1, sent_pages=1)
    assert unlimited.state == 'no_limit'
    assert unlimited.sentence == 'HumbleFax has carried 1 page and 1 fax since 1 October; you set no normal-use ' \
                                 'budget for it.'
    assert marginal(unlimited, 2).sentence == 'Included in your HumbleFax plan, with no normal-use budget set.'
    assert marginal(unlimited, 2).uses_budget is False
    # Shipped and generic starting budgets, and a budget you set.
    generic = plan_budget.budget_for('documo', card('documo', monthly='15'), values())
    assert (generic.pages, generic.faxes, generic.source) == (200, 50, 'default')
    assert generic.sentence == ("Faxbot starts Documo at 200 pages and 50 faxes a month, about what individual fax "
                                "plans include, because an unlimited plan's fair use is for Documo to judge; this is a "
                                "cautious start, not a limit Documo has promised to accept.")
    assert plan_budget.budget_for('sinch', SINCH, values()) is None  # a per-page route with nothing set
    # Minute bundles.
    minutes = budget(route='sip', label='Telnyx', pages=None, faxes=None, included_minutes=100, flat=False,
                     monthly_fee_micros=None, per_minute_micros=5_000)
    within = left_for(minutes, minutes=40)
    assert within.sentence == 'Telnyx has used 40 of the 100 minutes your plan includes since 1 October; 60 are ' \
                              'left until 1 November.'
    assert marginal(within, 1, prediction(5_000)).sentence == ('Included in the 100 minutes your Telnyx plan '
                                                               'includes; about 59 will be left until 1 November.')
    edge = marginal(left_for(minutes, minutes=100), 1, prediction(5_000, seconds=130))
    assert (edge.cost, edge.sentence) == (Money(15_000, 'USD'), 'About 3 minutes past what your Telnyx plan '
                                                                'includes, at $0.005 a minute.')
    assert marginal(within, 1, prediction(None, seconds=None)).sentence == (
        'Whether this fax fits in the minutes your Telnyx plan includes is unknown, because its time on the line is '
        'unknown.')
    # A committed monthly spend.
    committed = budget(route='sip', label='Telnyx', pages=None, faxes=None, commitment_micros=50_000_000, flat=False,
                       monthly_fee_micros=None)
    spent = left_for(committed, spend_micros=49_998_000, unpriced=2)
    assert spent.sentence == ('You have spent about $50.00 with Telnyx since 1 October, and 2 faxes and calls with no '
                              'known cost; $0.002 of your $50 monthly commitment is left until 1 November (estimate).')
    covered = marginal(spent, 1, prediction(2_000))
    assert (covered.cost, covered.uses_budget) == (Money(0, 'USD'), False)
    assert covered.sentence == 'Covered by your $50 monthly commitment with Telnyx, already paid for until 1 November.'
    beyond = marginal(spent, 1, prediction(5_000))
    assert (beyond.cost, beyond.sentence) == (Money(3_000, 'USD'), 'About $0.003 past your $50 monthly commitment '
                                                                   'with Telnyx.')
    assert marginal(spent, 1, prediction(None)).sentence == ('Whether your monthly commitment with Telnyx covers this '
                                                             'fax is unknown, because its cost is unknown.')
    # A route without a plan passes the predictor's own answer through.
    plain = marginal(None, 1, prediction(5_000))
    assert (plain.cost, plain.uses_budget, plain.sentence) == (Money(5_000, 'USD'), False,
                                                              'Billed as 1 minute at $0.005 a minute.')
    with pytest.raises(ValueError):
        marginal(None, 1)


def test_the_order_key_puts_free_before_budgeted_and_over_budget_last():
    free = plan_budget.Marginal(Money(0, 'USD'), False, False, '')
    budgeted = plan_budget.Marginal(Money(0, 'USD'), False, True, '')
    metered = plan_budget.Marginal(Money(5_000, 'USD'), False, False, '')
    unknown = plan_budget.Marginal(None, False, False, '')
    over = plan_budget.Marginal(Money(0, 'USD'), True, True, '')
    found = sorted([('over', over), ('unknown', unknown), ('metered', metered), ('budgeted', budgeted),
                    ('free', free)], key=lambda item: order_key(item[1]))
    assert [name for name, _ in found] == ['free', 'budgeted', 'metered', 'unknown', 'over']


# B13: other carriers ------------------------------------------------------------------------------------------------

def shipped_card(identity, direction='outbound'):
    from api.app.routing.seed import load_cards
    return next(card for card in load_cards() if card.provider_id == identity and card.direction == direction)


def test_other_carriers_price_your_faxes_at_their_published_prices(plans):
    multi, routes = plans
    settings = values(effective_outbound='sip', outbound_route_providers=())
    for _ in range(2):
        record(multi, routes, 'sip', LAB, OCTOBER, pages=3, seconds=50)
    received(routes, 'sip', TRUNK, OCTOBER, pages=2)
    result = compare(routes.engine, settings, now=datetime(2026, 10, 21))
    assert (result['sent'], result['received'], result['advice_only'], result['estimate']) == (2, 1, True, True)
    assert result['switching_sentence'] == SWITCHING
    assert result['unpublished_sentence'] == ('Sinch trunk, Gamma trunk, BT One Voice trunk, Telstra SIP Connect trunk '
                                              'and eFax publish no price Faxbot can use, so they are left out.')
    views = {view['id']: view for view in result['carriers']}
    # Telnyx, worked out here: each 50-second call is one billed minute; the received fax has no measured time, so it
    # takes the usual 30 seconds plus 30 a page (90 seconds, two minutes); one number at $1.00 a month.
    telnyx = 2 * attempt_cost(shipped_card('sip-telnyx'), seconds=50, pages=3, delivered=True) + attempt_cost(
        shipped_card('sip-telnyx', 'inbound'), seconds=90, pages=2, delivered=True) + 1_000_000
    assert telnyx == 1_016_400
    assert views['sip-telnyx']['total'] == money(telnyx) and views['sip-telnyx']['yours'] is True
    assert views['sip-telnyx']['difference'] == money(0)
    anveo = 2 * attempt_cost(shipped_card('sip-anveo'), seconds=50, pages=3, delivered=True) + attempt_cost(
        shipped_card('sip-anveo', 'inbound'), seconds=90, pages=2, delivered=True) + 150_000
    phaxio = 2 * 3 * 70_000 + 2 * 70_000 + 2_000_000  # 7 cents a page each way, and a $2 number
    assert views['sip-anveo']['total'] == money(anveo) and views['phaxio']['total'] == money(phaxio)
    assert views['sip-anveo']['difference'] == money(telnyx - anveo)
    # HumbleFax: the plan covers the faxes and includes one number.
    assert views['humblefax']['total'] == money(10_000_000) and views['humblefax']['complete'] is True
    assert views['humblefax']['sentence'] == ('About $10.00 for your 2 sent faxes and 1 received fax (its plan fee '
                                              'included) (estimate).')
    # Sinch and SignalWire publish no monthly number price: unknown, so never the cheapest.
    assert views['sinch']['complete'] is False and views['sinch']['numbers_not_priced'] == 1
    assert views['sinch']['difference'] == []
    complete = [view for view in result['carriers'] if view['complete'] and not view['over_budget']]
    cheapest = min(complete, key=lambda view: parse_amount(view['total'][0]['amount'], whole_digits=6))
    assert result['cheapest'] == cheapest['id'] == 'sip-anveo' and views['sip-anveo']['cheapest'] is True
    assert result['sentence'] == (f'At published prices, AnveoDirect trunk would have cost least for your last 30 '
                                  f'days of faxing: about {_short(anveo)}, {_short(telnyx - anveo)} less than your '
                                  f"current services' $1.02 (estimate).")
    assert views['sip-anveo']['sentence'] == (f'About {_short(anveo)} for your 2 sent faxes and 1 received fax (1 '
                                              'number included at its published monthly price) (estimate).')


def _short(micros):
    from api.app.routing.delivered import short_money_text
    return short_money_text(micros, 'USD')


def test_unknown_prices_stay_unknown_and_are_never_the_cheapest(plans):
    multi, routes = plans
    settings = values(effective_outbound='sip', outbound_route_providers=())
    record(multi, routes, 'sip', LAB, OCTOBER, pages=1, seconds=40)
    record(multi, routes, 'sip', ABROAD, OCTOBER, pages=1, seconds=40)
    result = compare(routes.engine, settings, now=datetime(2026, 10, 21))
    views = {view['id']: view for view in result['carriers']}
    # Telnyx publishes no price for calls abroad: one fax not priced, so your current cost is incomplete.
    assert (views['sip-telnyx']['complete'], views['sip-telnyx']['not_priced']) == (False, 1)
    # Sending needs a number too: one at the carrier's published monthly price.
    assert views['sip-telnyx']['sentence'] == ('About $1.01 for your 2 sent faxes (1 number included at its published '
                                               'monthly price, 1 fax it publishes no price for left out) (estimate).')
    assert views['humblefax']['not_priced'] == 1 and views['humblefax']['cheapest'] is False
    assert result['current'] == {'total': money(5_000), 'complete': False, 'not_priced': 1, 'routes': ['Telnyx trunk']}
    # AnveoDirect publishes a UK price, so it covers both faxes, plus one number at $0.15 a month.
    uk = next(entry for entry in json.loads(_rate_cards())['international'] if entry.get('prefixes') == ['+44'])
    from api.app.routing.costs import RateTerms
    from api.app.routing.predict_facts import _class_entry
    _, _, terms, _, _ = _class_entry(uk, 'international')
    abroad = terms_cost(terms, seconds=40, pages=1)[1]
    local = attempt_cost(shipped_card('sip-anveo'), seconds=40, pages=1, delivered=True)
    assert isinstance(terms, RateTerms) and views['sip-anveo']['total'] == money(local + abroad + 150_000)
    assert result['cheapest'] == 'sip-anveo'
    assert result['sentence'] == (f'At published prices, AnveoDirect trunk would have cost least for your last 30 days '
                                  f'of faxing: about {_short(local + abroad + 150_000)} (estimate). Faxbot cannot say how much '
                                  'that saves, because some of what your current services charge is not published.')
    assert all(view['difference'] == [] for view in result['carriers'])


def _rate_cards():
    from api.app.routing.seed import default_path
    return default_path().read_text(encoding='utf-8')


def test_with_no_faxes_there_is_nothing_to_compare(plans):
    _, routes = plans
    result = compare(routes.engine, values(), now=OCTOBER)
    assert result['carriers'] == [] and result['cheapest'] is None
    assert result['sentence'] == NOTHING.format(days=30) == ('You sent and received no faxes in the last 30 days, so '
                                                             'there is nothing to compare yet.')


def test_a_flat_plan_past_its_normal_use_budget_is_not_named_the_cheapest(plans):
    multi, routes = plans
    settings = values(effective_outbound='sip', outbound_route_providers=(), plan_budgets='humblefax:pages=5')
    for _ in range(3):
        record(multi, routes, 'sip', LAB, OCTOBER, pages=3, seconds=50)
    result = compare(routes.engine, settings, now=datetime(2026, 10, 21))
    views = {view['id']: view for view in result['carriers']}
    assert views['humblefax']['over_budget'] is True and result['cheapest'] != 'humblefax'
    assert views['humblefax']['sentence'].endswith('That is more than the normal-use budget Faxbot uses for HumbleFax, '
                                                   'so its plan may not take this much.')


def test_the_carrier_list_reads_only_published_prices():
    found, missing = carriers()
    names = {carrier.id: carrier.name for carrier in found}
    assert names['sip-telnyx'] == 'Telnyx trunk' and names['phaxio'] == 'Phaxio'
    assert 'sip-gamma' not in names and 'Gamma trunk' in missing and 'eFax' in missing
    assert all(carrier.sending.per_minute_micros or carrier.sending.per_page_micros or carrier.sending.flat_plan
               for carrier in found)


# Over HTTP ---------------------------------------------------------------------------------------------------------

def test_plans_and_carriers_over_http_and_the_budget_saved_as_a_setting(client):  # noqa: F811
    empty = client.get('/routing/recommendations/carriers', headers=ADMIN)
    assert empty.status_code == 200, empty.text
    assert empty.json()['sentence'] == NOTHING.format(days=30)
    current = client.get('/admin/settings', headers=ADMIN).json()
    saved = client.put('/admin/settings', headers=ADMIN, json={
        'expected_revision_id': current['_meta']['desired_revision_id'],
        'plan_budgets': 'humblefax:faxes=40,pages=300,day=9'})
    assert saved.status_code == 200, saved.text
    view = client.get('/routing/plans', headers=ADMIN)
    assert view.status_code == 200, view.text
    body = view.json()
    assert body['plan_budgets'] == 'humblefax:pages=300,faxes=40,day=9'
    (plan,) = [plan for plan in body['plans'] if plan['route'] == 'humblefax']
    assert (plan['budget']['pages'], plan['budget']['faxes'], plan['budget']['day'], plan['budget']['source']) == (
        300, 40, 9, 'set')
    current = client.get('/admin/settings', headers=ADMIN).json()
    refused = client.put('/admin/settings', headers=ADMIN, json={
        'expected_revision_id': current['_meta']['desired_revision_id'], 'plan_budgets': 'humblefax:pages=lots'})
    assert refused.status_code in (400, 422)
    reader = scoped_key(client, ['fax:send'])
    assert client.get('/routing/plans', headers=reader).status_code == 403
    assert client.get('/routing/recommendations/carriers', headers=reader).status_code == 403


# The command line ------------------------------------------------------------------------------------------------

@pytest.fixture
def plan_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for served in _serve(monkeypatch, tmp_path, FAX_OUTBOUND_ROUTES='humblefax'):
        yield Cli(served)


def test_costs_plans_budget_show_and_carriers_on_the_command_line(plan_cli):
    cli = plan_cli
    saved = cli('costs', 'plans', 'budget', 'humblefax', '--pages', '300', '--billing-day', '9')
    assert saved.exit_code == 0, (saved.stdout, saved.stderr)
    assert 'HumbleFax' in saved.stdout and 'The 9th' in saved.stdout
    view = cli.json('costs', 'plans', 'show')
    assert view['plan_budgets'] == 'humblefax:pages=300,day=9'
    (plan,) = [plan for plan in view['plans'] if plan['route'] == 'humblefax']
    # Only the values given change: the fax budget keeps Faxbot's starting 50.
    assert (plan['budget']['pages'], plan['budget']['faxes'], plan['budget']['day']) == (300, 50, 9)
    more = cli('costs', 'plans', 'budget', 'humblefax', '--faxes', 'none')
    assert more.exit_code == 0, more.stderr
    assert cli.json('costs', 'plans', 'show')['plan_budgets'] == 'humblefax:pages=300,faxes=none,day=9'
    assert cli('costs', 'plans', 'show', '--by-day').exit_code == 0
    back = cli('costs', 'plans', 'budget', 'humblefax', '--default')
    assert back.exit_code == 0 and cli.json('costs', 'plans', 'show')['plan_budgets'] == ''
    for wrong, sentence in (
            (('--pages', 'lots'), 'Give --pages as a whole number from 1 to 1,000,000, or none.'),
            ((), 'Give at least one of --pages, --faxes, --billing-day, --included-pages, --page-overage, '
                 '--included-minutes or --commitment, or --default.'),
            (('--default', '--pages', '5'), 'Use --default on its own: it goes back to the starting budget.'),
            (('--page-overage', 'ten'), 'Enter amounts as numbers, such as 0.10 or 50.')):
        refused = cli('costs', 'plans', 'budget', 'efax', *wrong)
        assert refused.exit_code == 1 and refused.stderr.strip() == sentence
    carriers = cli.json('costs', 'recommendations', 'carriers')
    assert carriers['sentence'] == NOTHING.format(days=30) and carriers['switching_sentence'] == SWITCHING
    shown = cli('costs', 'recommendations', 'carriers')
    assert shown.exit_code == 0 and 'never switches anything' in shown.stdout
    assert 'carriers' in cli.json('costs', 'recommendations')
    # The older form still lists a fax service's published plans.
    assert cli.json('costs', 'plans', 'efax')['plans']
    assert cli.json('costs', 'plans', 'published', 'efax')['plans']
