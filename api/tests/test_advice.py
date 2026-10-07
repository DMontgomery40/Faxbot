"""Advice from history: the fax marker, billing steps and partner candidates, on SQLite and PostgreSQL.

Synthetic calls only. Every expected figure is worked out here from the calls
themselves, never read back from the code under test. Each piece of advice has
its "not enough data" case, and an unknown cost is never shown as $0.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api.app.routing.boundaries import billing_boundaries, near_seconds, seconds_past_step, step_price
from api.app.routing.costs import RateCard, parse_amount
from api.app.routing.partners import MIN_FAXES, partner_candidates
from api.app.routing.preference import MIN_CALLS, fax_marker_report
from api.app.routing.store import RouteStore
from api.app.routing.trunk_calls import trunk_calls
from api.tests.test_outbound_store import accept, installation  # noqa: F401  (fixture)
from api.tests.test_schema import database  # noqa: F401  (fixture: SQLite and PostgreSQL)


NOW = datetime.utcnow().replace(microsecond=0)
CAPTURED = datetime(2026, 10, 5)
CLINIC, LAB, PHARMACY = '+12025550123', '+12025550140', '+13035550177'
TELNYX = RateCard(None, 'sip-telnyx', 'outbound', 'Telnyx', 'USD', parse_amount('0.005'), 0, 0, 60, 0,
                  'https://telnyx.com/pricing/elastic-sip', CAPTURED)


@pytest.fixture
def routes(installation):  # noqa: F811
    configuration, _, _ = installation
    store = RouteStore(configuration.engine, sip_preset=lambda: 'telnyx')
    store.replace_cards([TELNYX])
    return store


def place(installation, routes, *, to=CLINIC, ago=timedelta(days=1), seconds=45, pages=1, outcome='success',  # noqa: F811
          marker=True, recorded=True, t38='yes', reported=None, settled=None, estimated=None, currency='USD',
          route='sip', provider='sip', call=True):
    """One placed attempt with its cost row and, for the trunk, its call record."""
    job, attempt = accept(installation, to_number=to), uuid4().hex
    start = NOW - ago
    end = start + timedelta(seconds=seconds)
    with routes.engine.begin() as connection:
        connection.execute(routes.jobs.update().where(routes.jobs.c.id == job).values(pages=pages))
        connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase=outcome,
                                                           created_at=start, submitted_at=start, completed_at=end))
        connection.execute(routes.costs.insert().values(
            id=attempt, job_id=job, destination=to, route=route, route_reason='cheapest', provider_id=provider,
            started_at=start, ended_at=end,
            estimated_cost_micros=None if estimated is None else parse_amount(estimated),
            currency=currency if estimated is not None else None,
            reported_cost_micros=None if reported is None else parse_amount(reported),
            reported_currency=currency if reported is not None else None,
            settled_cost_micros=None if settled is None else parse_amount(settled),
            billing_checks=0, outcome=outcome, created_at=start, updated_at=end))
        if call:
            from api.app.routing.carriers import CarrierChargeStore
            calls = CarrierChargeStore(routes.engine).calls
            connection.execute(calls.insert().values(
                id=uuid4().hex, direction='outbound', call_id=attempt, job_id=job, attempt_id=attempt,
                trunk_preset='telnyx' if recorded else None, did='+13035550100', caller='+13035550100',
                called=to if recorded else None, started_at=start, answered_at=start, ended_at=end,
                disposition='answered', connected_seconds=seconds, t38=t38, pages=pages,
                fax_status={'success': 'SUCCESS', 'failed': 'FAILED'}.get(outcome), fax_preference=1 if marker else 0,
                created_at=start, updated_at=end))
    return attempt


def values(**changes):
    return SimpleNamespace(**{'sip_trunk_preset': 'telnyx', 'sip_fax_preference_header': True,
                              'fax_default_country': 'US', **changes})


# -- the fax marker -----------------------------------------------------------------------------------------

def test_with_no_calls_or_only_marked_calls_there_is_nothing_to_compare(installation, routes):  # noqa: F811
    report = fax_marker_report(routes.engine, values())
    assert (report['state'], report['enough']) == ('no_calls', False)
    assert report['sentence'] == ('Faxbot placed no calls over your carrier line in the last 90 days, so there is '
                                  'nothing to compare yet.')
    assert report['setting_sentence'] == ('Mark calls as fax is on, and Faxbot leaves it on: this comparison never '
                                          'changes a setting.')
    for _ in range(3):
        place(installation, routes, reported='0.005')
    report = fax_marker_report(routes.engine, values())
    assert report['state'] == 'one_side' and report['marked']['calls'] == 3 and report['not_marked']['calls'] == 0
    assert report['sentence'] == ('Every call in the last 90 days was marked as fax (3 calls), so there is nothing to '
                                  'compare it with.')


def test_too_few_calls_on_one_side_says_so_and_compares_nothing(installation, routes):  # noqa: F811
    for _ in range(MIN_CALLS):
        place(installation, routes, reported='0.005')
    for _ in range(2):
        place(installation, routes, marker=False, reported='0.005')
    report = fax_marker_report(routes.engine, values())
    assert report['state'] == 'too_few' and report['difference'] is None and report['caveat'] is None
    assert report['sentence'] == ('Too few calls to tell: 10 marked as fax and 2 not marked in the last 90 days. '
                                  'Faxbot compares them once it has at least 10 of each.')


def test_a_call_whose_marker_was_never_recorded_is_left_out_not_counted_as_off(installation, routes):  # noqa: F811
    place(installation, routes, reported='0.005')
    place(installation, routes, marker=False, recorded=False, reported='0.005')
    assert sorted(str(call.marker) for call in trunk_calls(routes.engine, days=90)) == ['None', 'True']
    report = fax_marker_report(routes.engine, values())
    assert report['state'] == 'one_side' and report['left_out'] == 1
    assert report['left_out_sentence'] == ('1 call is left out because Faxbot did not record whether it was marked.')


def test_enough_calls_on_each_side_are_compared_by_delivery_mode_time_and_cost(installation, routes):  # noqa: F811
    # Marked: 10 delivered of 10, 9 by T.38, 40 s for 2 pages, settled at $0.005 each.
    for index in range(10):
        place(installation, routes, seconds=40, pages=2, t38='yes' if index < 9 else 'no', reported='0.005',
              settled='0.005', ago=timedelta(days=1, minutes=index))
    # Not marked: 8 delivered of 10 (2 failed), 5 by T.38; the failed calls are still paid for.
    for index in range(10):
        place(installation, routes, marker=False, seconds=60, pages=2, outcome='success' if index < 8 else 'failed',
              t38='yes' if index < 5 else 'no', reported='0.005', ago=timedelta(days=40, minutes=index))
    report = fax_marker_report(routes.engine, values())
    marked, plain = report['marked'], report['not_marked']
    assert report['state'] == 'compared' and report['enough'] is True
    assert (marked['delivered_percent'], marked['t38_percent'], marked['seconds_per_page']) == (100, 90, 20)
    assert (plain['delivered_percent'], plain['t38_percent'], plain['seconds_per_page']) == (80, 50, 30)
    # Every attempt counts toward the cost; only delivered faxes divide it: $0.05 / 8 rounds up to $0.00625.
    assert marked['cost_per_delivered'] == {'currency': 'USD', 'amount': '0.005'} and marked['cost_text'] == '$0.005'
    assert plain['cost_per_delivered'] == {'currency': 'USD', 'amount': '0.00625'}
    assert plain['cost_text'] == 'About $0.0063'  # reported, not yet settled
    assert report['difference'] == ('Calls marked as fax delivered more often and calls marked as fax used fax over '
                                    'IP (T.38) more often.')
    assert report['sentence'].startswith('Marked as fax: 100% delivered, 90% used fax over IP (T.38), 20 seconds a '
                                         'page, $0.005 per delivered fax. Not marked: 80% delivered')
    assert report['caveat'].startswith('The two groups are calls from different days, not a controlled test')


def test_an_unpriced_call_makes_the_cost_unknown_never_zero(installation, routes):  # noqa: F811
    for _ in range(MIN_CALLS):
        place(installation, routes, reported='0.005')
        place(installation, routes, marker=False, reported='0.005')
    place(installation, routes, marker=False)  # no charge and no estimate
    report = fax_marker_report(routes.engine, values(sip_fax_preference_header=False))
    assert report['not_marked']['cost_per_delivered'] is None and report['not_marked']['cost_text'] == 'Unknown'
    assert report['not_marked']['unpriced'] == 1 and 'cost per delivered fax unknown' in report['sentence']
    assert report['setting_sentence'] == ('Mark calls as fax is off on your carrier trunk; this comparison never '
                                          'changes a setting.')


# -- billing steps ---------------------------------------------------------------------------------------------

def test_seconds_past_a_step_follow_the_cards_minimum_and_first_step():
    card = RateCard(None, 'sip', 'outbound', 'Trunk', 'USD', 5_000, 0, 0, 60, 30, None, CAPTURED)
    assert seconds_past_step(card, 61) == 1 and seconds_past_step(card, 70) == 10 and seconds_past_step(card, 120) == 60
    # Inside the first minute a shorter call costs the same; so does a call inside a 90-second minimum.
    assert seconds_past_step(card, 59) is None and seconds_past_step(card, 0) is None
    minimum = RateCard(None, 'sip', 'outbound', 'Trunk', 'USD', 5_000, 0, 0, 60, 90, None, CAPTURED)
    assert seconds_past_step(minimum, 100) is None and seconds_past_step(minimum, 125) == 5
    assert near_seconds(60) == 10 and near_seconds(6) == 1 and step_price(card) == 5_000


def test_calls_ending_just_past_a_minute_are_named_with_the_minutes_they_would_save(installation, routes):  # noqa: F811
    for seconds in (61, 65, 70, 110, 45):
        place(installation, routes, to=CLINIC, seconds=seconds, pages=3)
    for seconds in (100, 110, 115):
        place(installation, routes, to=LAB, seconds=seconds)
    place(installation, routes, to=PHARMACY, seconds=62)  # too few calls to say anything
    report = billing_boundaries(routes.engine, routes, values())
    assert report['state'] == 'near' and report['numbers_total'] == 1 and report['calls_near'] == 3
    (clinic,) = report['numbers']
    assert (clinic['number'], clinic['calls'], clinic['calls_near']) == (CLINIC, 5, 3)
    assert clinic['seconds_past'] == {'least': 1, 'most': 10}
    assert clinic['saving'] == {'currency': 'USD', 'amount': '0.015'}
    assert clinic['sentence'] == ('Calls to this number end 1–10 s past a billed minute on 3 of 5 calls; one page less '
                                  'or a faster mode would have saved a minute on each, about $0.015 in the last 30 '
                                  'days (estimate).')
    assert report['sentence'] == ('Calls to one number often end just past a billed minute: one page less or a faster '
                                  'mode would have saved a minute on 3 calls, about $0.015 in the last 30 days '
                                  '(estimate).')
    assert report['step']['seconds'] == 60 and report['step']['read_on'] == '2026-10-05'


def test_billing_steps_say_plainly_when_there_is_too_little_or_nothing_to_measure(installation, routes):  # noqa: F811
    place(installation, routes, seconds=61)
    assert billing_boundaries(routes.engine, routes, values())['sentence'] == (
        'Faxbot needs at least 3 measured calls to the same number in the last 30 days to show how calls end against '
        'a billed minute; no number has that many yet.')
    for _ in range(3):
        place(installation, routes, to=LAB, seconds=100)
    assert billing_boundaries(routes.engine, routes, values())['state'] == 'none_near'
    assert billing_boundaries(routes.engine, routes, values(sip_trunk_preset=''))['state'] == 'no_trunk'
    routes.replace_cards([RateCard(None, 'sip-telnyx', 'outbound', 'Telnyx', 'USD', 5_000, 0, 0, 6, 6, None,
                                   CAPTURED)])
    fine = billing_boundaries(routes.engine, routes, values())
    assert fine['state'] == 'fine_steps' and fine['sentence'] == (
        'Telnyx bills calls in 6-second steps, so a call that ends a few seconds sooner saves at most $0.0005; there '
        'is nothing worth changing.')
    routes.replace_cards([])
    assert billing_boundaries(routes.engine, routes, values())['state'] == 'no_price'


# -- partner candidates ----------------------------------------------------------------------------------------

def test_partner_candidates_rank_numbers_by_recurring_cost_with_unknown_cost_last(installation, routes):  # noqa: F811
    for _ in range(4):
        place(installation, routes, to=CLINIC, pages=12, reported='0.02', call=False)
    place(installation, routes, to=CLINIC, outcome='failed', reported='0.01', call=False)
    for _ in range(3):
        place(installation, routes, to=LAB, pages=2, reported='0.005', call=False)
    for _ in range(3):  # a route with no charge, no estimate and no rate card: no price at all
        place(installation, routes, to=PHARMACY, pages=1, call=False, route='phaxio', provider='phaxio')
    place(installation, routes, to='+12025550188', reported='0.50', call=False)  # once only: not recurring
    result = partner_candidates(routes)
    assert result['state'] == 'candidates' and result['link'] == 'recipients/partners'
    assert [item['number'] for item in result['items']] == [CLINIC, LAB, PHARMACY]
    clinic, lab, pharmacy = result['items']
    # Every attempt counts, the failed one too: 4 x $0.02 + $0.01.
    assert clinic['monthly_cost'] == {'currency': 'USD', 'amount': '0.09'} and clinic['faxes'] == 5
    assert clinic['sentence'] == ('You sent 5 faxes to this number in the last 30 days, about 12 pages each, costing '
                                  'about $0.09 a month (estimate). If the recipient runs Faxbot and enrolls as a direct '
                                  'partner, those faxes go straight to them with no call charge.')
    assert lab['monthly_cost'] == {'currency': 'USD', 'amount': '0.015'}
    assert pharmacy['monthly_cost'] is None and pharmacy['cost_text'] == 'Unknown'
    assert 'monthly cost is unknown' in pharmacy['sentence']
    assert clinic['link_label'] == 'Enroll as direct partner'


def test_partners_already_enrolled_and_thin_history_are_not_suggested(installation, routes):  # noqa: F811
    assert partner_candidates(routes)['state'] == 'none'
    for _ in range(MIN_FAXES - 1):
        place(installation, routes, to=CLINIC, reported='0.02', call=False)
    thin = partner_candidates(routes)
    assert thin['state'] == 'too_few' and thin['items'] == []
    assert thin['sentence'] == ('No number you fax had 3 or more faxes in the last 30 days, so there is no partner to '
                                'suggest yet.')
    place(installation, routes, to=CLINIC, reported='0.02', call=False)
    assert [item['number'] for item in partner_candidates(routes)['items']] == [CLINIC]
    with routes.engine.begin() as connection:
        connection.execute(routes.peers.insert().values(
            id=uuid4().hex, organization='Synthetic Clinic', phone_number=CLINIC, endpoint_url='https://peer.invalid',
            signing_key='a' * 64, exchange_key='b' * 64, state='verified', challenge_failures=0,
            verified_at=NOW, version=1, created_at=NOW, updated_at=NOW))
    assert partner_candidates(routes)['items'] == []


# -- quiet numbers at other fax services ---------------------------------------------------------------------

HUMBLEFAX, EFAX = '+13035550197', '+17205550198'


def received(routes, provider, ago, to):
    with routes.engine.begin() as connection:
        from api.app.routing.database import reflect
        inbound = reflect(routes.engine, ('inbound_faxes',))['inbound_faxes']
        moment = NOW - ago
        connection.execute(inbound.insert().values(id=uuid4().hex, from_number='+12025550111', to_number=to,
                                                   status='received', backend=provider, created_at=moment,
                                                   received_at=moment, updated_at=moment))


def test_fax_service_numbers_are_quiet_or_in_use_and_ask_before_any_release(installation, routes):  # noqa: F811
    from api.app.routing.receiving import provider_numbers
    service = values(humblefax_from_number=HUMBLEFAX, efax_caller_id=EFAX)
    assert provider_numbers(routes.engine, routes, values())['state'] == 'none'
    thin = provider_numbers(routes.engine, routes, service)
    assert thin['state'] == 'too_little_history'
    assert thin['numbers'][0]['sentence'] == ('Faxbot needs 30 days of HumbleFax history to tell whether '
                                              '+1 303-555-0197 is quiet; it has none yet.')
    routes.replace_cards([TELNYX, RateCard(None, 'humblefax', 'outbound', 'HumbleFax', 'USD', 0, 0, 0, 60, 0, None,
                                           CAPTURED, parse_amount('10'))])
    received(routes, 'humblefax', timedelta(days=45), HUMBLEFAX)  # history reaches back past the window
    received(routes, 'humblefax', timedelta(days=3), HUMBLEFAX)
    received(routes, 'efax', timedelta(days=60), EFAX)
    for days in (1, 2, 3):
        received(routes, 'efax', timedelta(days=days), EFAX)
    result = provider_numbers(routes.engine, routes, service)
    humblefax, efax = result['numbers']
    assert (humblefax['quiet'], humblefax['received'], humblefax['sent']) == (True, 1, 0)
    assert humblefax['plan_fee'] == [{'currency': 'USD', 'amount': '10.00'}]
    assert humblefax['sentence'] == ('HumbleFax number +1 303-555-0197 had 1 fax in the last 30 days, 1 received and 0 '
                                     'sent. Your HumbleFax plan costs $10.00 a month; that is what giving it up would '
                                     'save, if nothing else uses the plan (estimate).')
    assert humblefax['question'] == ('Is +1 303-555-0197 still printed on your letterhead, forms or website, or listed '
                                     'anywhere? If it is, keep it.')
    assert (efax['quiet'], efax['question'], efax['plan_fee']) == (False, None, [])
    assert efax['sentence'] == 'eFax number +1 720-555-0198 had 3 faxes in the last 30 days, so it is in use.'
    assert result['state'] == 'quiet' and result['sentence'] == (
        '1 fax service number had 2 faxes or fewer in the last 30 days. Before you give one up, check that it is not '
        'still printed or published anywhere.')


# -- over HTTPS ------------------------------------------------------------------------------------------

def test_the_advice_routes_need_settings_read_and_answer_in_sentences(telnyx_client):  # noqa: F811
    from api.tests.test_routing_http import ADMIN, scoped_key
    sender = scoped_key(telnyx_client, ['fax:send'])
    for route, state in (('fax-marker', 'no_calls'), ('billing-steps', None), ('partners', 'none'),
                         ('toll-free', 'none')):
        response = telnyx_client.get(f'/routing/recommendations/{route}', headers=ADMIN)
        assert response.status_code == 200, (route, response.text)
        body = response.json()
        assert body['sentence'] and (state is None or body['state'] == state), (route, body)
        assert telnyx_client.get(f'/routing/recommendations/{route}', headers=sender).status_code == 403
    receiving = telnyx_client.get('/routing/recommendations/receiving', headers=ADMIN).json()
    assert receiving['provider_numbers'] == {'state': 'none', 'numbers': [], 'most_faxes': 2, 'sentence': (
        'Faxbot knows no fax service number besides your carrier line.')}
    assert telnyx_client.get('/routing/recommendations/fax-marker', headers=ADMIN,
                             params={'days': 3}).status_code == 422


from api.tests.test_routing_http import telnyx_client  # noqa: E402,F401  (fixture: a Telnyx trunk over HTTPS)
