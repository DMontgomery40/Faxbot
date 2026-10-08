"""What a missing fact costs you (research candidate 9): synthetic installations with real history rows, the real
predictor (``predict_from`` over ``facts_for``) and the real number placement. Nothing is stood in for them.

Prices are the shipped, dated cards (``config/rate_cards.json``): Telnyx $0.005 a minute in 60-second steps,
Phaxio $0.07 a page, a HumbleFax $10 plan. The predictor's typical page with the default coding (MR) takes about
12.3 seconds on the line, so a 4-page fax runs just past a minute and with the most compact coding (MMR) just under.
"""
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.config_profiles import ConfigurationDocument
from api.app.config_values import ConfigurationValues
from api.app.routing import fact_advice, predict, predict_facts
from api.app.routing.seed import load_cards
from api.app.routing.store import RouteStore
from api.app.routing.tollfree import TollFreeApprovals
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)


NOW = datetime(2026, 10, 8, 12, 0)
PARTNER, TOLL, CASES, QUIET = '+13035550123', '+13035550124', '+13035550125', '+13035550126'
ALTERNATE = '+18005550199'


def values(**extra):
    environment = {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip',
                   'SIP_TRUNK_HOST': 'sip.telnyx.com', 'SIP_TRUNK_DIDS': '+13035550100',
                   'FAX_DEFAULT_COUNTRY': 'US', 'INBOUND_ENABLED': 'true', 'FAX_OUTBOUND_ROUTES': 'phaxio',
                   'PHAXIO_API_KEY': 'synthetic', 'PHAXIO_API_SECRET': 'synthetic', **extra}
    return ConfigurationValues.from_environment(environment)


def seeded(engine):
    upgrade_schema(engine)
    routes = RouteStore(engine, sip_preset=lambda: 'telnyx')
    routes.seed_cards(load_cards())
    return routes


def tables(connection, *names):
    return {name: sa.Table(name, sa.MetaData(), autoload_with=connection) for name in names}


def sent(engine, number, *, pages=4, route='sip', provider='sip', when=None, count=1):
    """Delivered faxes with their attempt and its cost row, as delivery records them."""
    jobs = []
    with engine.begin() as connection:
        t = tables(connection, 'fax_jobs', 'outbound_attempts', 'delivery_attempt_costs')
        for index in range(count):
            at = (when or NOW - timedelta(days=3)) + timedelta(minutes=index)
            job, attempt = uuid4().hex, uuid4().hex
            connection.execute(t['fax_jobs'].insert().values(
                id=job, to_number=number, file_name='synthetic.pdf', tiff_path='', status='SUCCESS', pages=pages,
                backend=provider, created_at=at, updated_at=at))
            connection.execute(t['outbound_attempts'].insert().values(
                id=attempt, job_id=job, sequence=1, phase='success', created_at=at, submitted_at=at,
                completed_at=at + timedelta(minutes=2)))
            connection.execute(t['delivery_attempt_costs'].insert().values(
                id=attempt, job_id=job, destination=number, route=route, route_reason='cheapest',
                provider_id=provider, billing_checks=0, outcome='success', created_at=at, updated_at=at))
            jobs.append(job)
    return jobs


def suggest_partner(engine, number, organization='Synthetic Partner Clinic'):
    with engine.begin() as connection:
        table = tables(connection, 'direct_discovery_suggestions')['direct_discovery_suggestions']
        connection.execute(table.insert().values(
            id=uuid4().hex, number=number, source='call', organization=organization, signing_key='a' * 43,
            endpoint='https://partner.example.org/direct', created_at=NOW - timedelta(days=10)))


def facts_by_key(result, number):
    item = next(item for item in result['recipients'] if item['number'] == number)
    return item, {row['fact']: row for row in item['facts']}


def money(rows):
    return rows[0]['amount'] if rows else None


def test_the_catalogue_is_fixed_and_each_fact_has_its_establishing_step():
    keys = [fact.key for fact in fact_advice.CATALOGUE]
    assert keys == ['partner', 'digital_address', 'capabilities', 'toll_free', 'case_reuse', 'route_price',
                    'receiving_price', 'plan_allowance']
    kinds = {fact.key: fact.kind for fact in fact_advice.CATALOGUE}
    assert {key for key, kind in kinds.items() if kind == 'authorization'} == {
        'partner', 'digital_address', 'toll_free', 'case_reuse'}
    for fact in fact_advice.CATALOGUE:
        assert fact.step and fact.confirm and fact.time
        # Caller ID is never part of a fact: Faxbot never changes it to lower a charge.
        assert 'caller id' not in (fact.title + fact.confirm + fact.step).lower()


def test_a_partner_suggestion_prices_direct_delivery_minus_the_enrollment_fax(database):  # noqa: F811
    routes = seeded(database)
    sent(database, PARTNER, count=3)
    suggest_partner(database, PARTNER)
    result = fact_advice.advice(database, values(), routes=routes, now=NOW)
    item, rows = facts_by_key(result, PARTNER)
    # A 4-page fax by Telnyx: 11 s setup + 4 x 12.3 s = 60.0 s, billed 2 minutes = $0.010; Phaxio would be $0.28.
    assert money(item['baseline']) == '0.03'
    partner = rows['partner']
    assert (partner['kind'], partner['boundary'], partner['faxes']) == ('authorization', True, 3)
    assert money(partner['saving']) == '0.03'
    # The enrollment fax is one page by the best route: $0.005, subtracted.
    assert money(partner['establish_cost']) == '0.005' and money(partner['net']) == '0.025'
    assert partner['sentence'].startswith('Would have cost $0.03 less over 3 faxes (estimate).')
    assert 'one enrollment fax (about $0.005)' in partner['sentence']
    assert 'Synthetic Partner Clinic must confirm the code' in partner['confirm']
    assert partner['realized'] is False and result['realized'].startswith('These figures are what your faxes')


def test_no_evidence_no_partner_fact(database):  # noqa: F811
    routes = seeded(database)
    sent(database, QUIET, pages=1)
    result = fact_advice.advice(database, values(), routes=routes, now=NOW)
    assert all(row['fact'] != 'partner' for item in result['recipients'] for row in item['facts'])


def test_a_toll_free_number_on_file_is_a_boundary_never_a_cheaper_route(database):  # noqa: F811
    routes = seeded(database)
    sent(database, TOLL, count=2)
    TollFreeApprovals(database).record(TOLL, action='noted', alternate_number=ALTERNATE, now=NOW - timedelta(days=2))
    result = fact_advice.advice(database, values(), routes=routes, now=NOW)
    item, rows = facts_by_key(result, TOLL)
    # Telnyx calls toll-free numbers for nothing, yet the best allowed route stays the recipient's own number.
    assert money(item['baseline']) == '0.02'
    toll = rows['toll_free']
    assert (toll['kind'], toll['boundary']) == ('authorization', True)
    assert money(toll['saving']) == '0.02' and toll['establish_cost'] == []
    assert 'they pay for those calls' in toll['confirm'] and '+1 800-555-0199' in toll['confirm']
    assert toll['sentence'].endswith('Faxbot will not use it until then.')
    # No penalty or score exists that a cheap route could outweigh.
    for row in item['facts']:
        assert not {'penalty', 'score', 'weight'} & set(row)
    # Approved, the toll-free number is part of the best allowed route and the fact is gone.
    TollFreeApprovals(database).record(TOLL, action='approved', alternate_number=ALTERNATE,
                                       approved_by='Dana at the lab', approved_on=NOW - timedelta(days=1),
                                       evidence='Email from Dana, 7 October 2026', now=NOW)
    after = fact_advice.advice(database, values(), routes=routes, now=NOW)
    assert all(item['number'] != TOLL or 'toll_free' not in {row['fact'] for row in item['facts']}
               for item in after['recipients'])


def test_no_record_of_the_far_machine_prices_the_compact_coding_as_an_upper_bound(database):  # noqa: F811
    routes = seeded(database)
    sent(database, QUIET, count=2)
    result = fact_advice.advice(database, values(), routes=routes, now=NOW)
    _, rows = facts_by_key(result, QUIET)
    coding = rows['capabilities']
    # MR: 60.0 s, two minutes; MMR: 11 + 4 x 9.75 = 50 s, one minute: $0.005 less a fax.
    assert coding['kind'] == 'information' and coding['boundary'] is False
    assert money(coding['saving']) == '0.01'
    assert coding['sentence'].startswith('Would have cost up to $0.01 less over 2 faxes (estimate), if its fax '
                                         'machine accepts the most compact coding.')
    assert 'next call through Telnyx records' in coding['confirm']


def test_per_page_routes_never_get_a_coding_fact(database):  # noqa: F811
    routes = seeded(database)
    sent(database, QUIET, count=2, route='phaxio', provider='phaxio')
    result = fact_advice.advice(database, values(FAX_BACKEND='phaxio', FAX_OUTBOUND_ROUTES=''), routes=routes, now=NOW)
    assert all(row['fact'] != 'capabilities' for item in result['recipients'] for row in item['facts'])


def test_documents_they_already_acknowledged_price_the_one_page_index(database):  # noqa: F811
    routes = seeded(database)
    job = sent(database, CASES, pages=10)[0]
    entry = uuid4().hex
    with database.begin() as connection:
        t = tables(connection, 'case_packet_sends', 'case_entries', 'case_entry_sends', 'case_entry_events')
        connection.execute(t['case_packet_sends'].insert().values(
            id=job, case_id='case-1', recipient=CASES, pages_sent=10, pages_left_out=0, documents_left_out=0,
            created_at=NOW - timedelta(days=3)))
        connection.execute(t['case_entries'].insert().values(
            id=entry, case_id='case-1', recipient=CASES, digest='d' * 64, source='upload', version='1',
            purpose='referral', title='Synthetic referral', page_count=6, created_at=NOW - timedelta(days=20)))
        connection.execute(t['case_entry_sends'].insert().values(
            id=uuid4().hex, entry_id=entry, job_id=job, created_at=NOW - timedelta(days=3)))
        connection.execute(t['case_entry_events'].insert().values(
            id=uuid4().hex, entry_id=entry, kind='accepted', source='person', occurred_at=NOW - timedelta(days=10),
            created_at=NOW - timedelta(days=10)))
    result = fact_advice.advice(database, values(), routes=routes, now=NOW)
    _, rows = facts_by_key(result, CASES)
    reuse = rows['case_reuse']
    # 10 pages: 133.6 s, 3 minutes ($0.015); 10 - 6 + the one-page list = 5 pages: 72.3 s, 2 minutes ($0.010).
    assert reuse['kind'] == 'authorization' and money(reuse['saving']) == '0.005' and reuse['faxes'] == 1
    assert 'one-page list' in reuse['confirm']


def test_a_route_with_no_price_gives_a_count_and_a_break_even_never_a_saving(database):  # noqa: F811
    routes = seeded(database)
    sent(database, QUIET, count=2)
    # Documo publishes no sending price Faxbot ships, and none was entered.
    result = fact_advice.advice(database, values(FAX_OUTBOUND_ROUTES='phaxio,documo', DOCUMO_API_KEY='synthetic'),
                                routes=routes, now=NOW)
    _, rows = facts_by_key(result, QUIET)
    price = rows['route_price']
    assert price['kind'] == 'price' and price['saving'] == [] and price['net'] == [] and price['unknown']
    assert price['faxes'] == 2
    assert 'Documo publishes no price for these calls' in price['sentence']
    # $0.020 over 8 pages, or over Documo's predicted 2 minutes each.
    assert price['break_even']['per_page'] == [{'currency': 'USD', 'amount': '0.0025'}]
    assert 'If it charges less than' in price['sentence']


def test_a_plan_at_faxbots_cautious_budget_prices_the_faxes_it_pushed_elsewhere(database):  # noqa: F811
    routes = seeded(database)
    plan_values = values(FAX_OUTBOUND_ROUTES='humblefax', HUMBLEFAX_ACCESS_KEY='synthetic',
                         HUMBLEFAX_SECRET_KEY='synthetic', HUMBLEFAX_FROM_NUMBER='+17205550199')
    # The plan carried 50 faxes this month, Faxbot's cautious start for HumbleFax; later faxes went by Telnyx.
    sent(database, '+13035550190', pages=1, route='humblefax', provider='humblefax', count=50,
         when=NOW - timedelta(days=6))
    sent(database, QUIET, count=2, when=NOW - timedelta(days=2))
    result = fact_advice.advice(database, plan_values, routes=routes, now=NOW)
    item, rows = facts_by_key(result, QUIET)
    allowance = rows['plan_allowance']
    assert allowance['kind'] == 'information'
    assert money(item['baseline']) == '0.02' and money(allowance['saving']) == '0.02'
    assert allowance['sentence'].startswith('2 faxes went another way because your HumbleFax plan was past the '
                                            'cautious budget')
    assert 'HumbleFax must tell you in writing' in allowance['confirm']


def test_a_suggested_direct_address_without_an_account_is_unknown_not_free(database):  # noqa: F811
    from api.app.digital.store import DigitalStore
    routes = seeded(database)
    sent(database, QUIET, count=2)
    DigitalStore(database).add_address(number=QUIET, kind='direct', address='records@direct.example.org',
                                       source='nppes', action='suggested')
    result = fact_advice.advice(database, values(), routes=routes, now=NOW)
    _, rows = facts_by_key(result, QUIET)
    digital = rows['digital_address']
    assert digital['kind'] == 'authorization' and digital['unknown'] and digital['saving'] == []
    assert 'you have no account for sending there yet' in digital['sentence']


def test_a_suggested_fhir_endpoint_with_a_priced_account_is_priced_by_the_predictor(database):  # noqa: F811
    from api.app.digital.store import DigitalStore
    routes = seeded(database)
    sent(database, QUIET, count=2)
    DigitalStore(database).add_address(number=QUIET, kind='fhir', address='https://fhir.example.org/r4',
                                       source='nppes', action='suggested', account_key='fhir-hospital')
    priced = values().with_provider_accounts(ConfigurationDocument({'fhir-hospital': {
        'kind': 'digital', 'provider': 'fhir', 'label': 'County FHIR', 'settings': {'client_id': 'synthetic',
                                                                 'price_per_message': '0.002'}}}))
    result = fact_advice.advice(database, priced, routes=routes, now=NOW)
    _, rows = facts_by_key(result, QUIET)
    # Each fax costs $0.010 by Telnyx and $0.002 as a message: $0.008 less each.
    assert money(rows['digital_address']['saving']) == '0.016'


def test_a_receiving_account_with_no_price_is_a_count_for_your_number(database):  # noqa: F811
    routes = seeded(database)
    sinch = {'provider': 'sip', 'label': 'Sinch trunk', 'receives': True, 'numbers': ['+17205550150'],
             'settings': {'preset': 'sinch', 'auth': 'ip', 'host': '192.0.2.60'}}
    result = fact_advice.advice(database, values().with_provider_accounts(ConfigurationDocument({'sip-sinch': sinch})),
                                routes=routes, now=NOW)
    yours = [item for item in result['recipients'] if item['kind'] == 'your_number']
    assert yours, result
    row = yours[0]['facts'][0]
    assert row['fact'] == 'receiving_price' and row['kind'] == 'price' and row['unknown']
    assert 'has no receiving price' in row['sentence']


def test_the_largest_single_fact_ranks_recipients_and_facts_are_never_added(database):  # noqa: F811
    routes = seeded(database)
    sent(database, TOLL, count=2)
    TollFreeApprovals(database).record(TOLL, action='noted', alternate_number=ALTERNATE, now=NOW - timedelta(days=2))
    sent(database, PARTNER, count=3)
    suggest_partner(database, PARTNER)
    result = fact_advice.advice(database, values(), routes=routes, now=NOW)
    order = [item['number'] for item in result['recipients']]
    assert order.index(PARTNER) < order.index(TOLL)
    toll, rows = facts_by_key(result, TOLL)
    # The toll-free number ($0.020) and the compact coding ($0.010) each stand alone; the figure is the larger.
    assert money(toll['largest']) == '0.02'
    assert result['state'] == 'advice' and result['note'].startswith('Faxbot only advises')


def test_the_best_allowed_route_is_what_predict_itself_says(database, monkeypatch):  # noqa: F811
    """Companion test: the real ``predict.predict`` (its own fact gathering) gives the same baseline."""
    routes = seeded(database)
    sent(database, QUIET, count=1)
    configured = values(FAX_OUTBOUND_ROUTES='')
    monkeypatch.setattr(predict_facts, '_engine', lambda: database)
    monkeypatch.setattr(predict_facts, '_values', lambda: configured)
    direct = predict.predict('sip', QUIET, predict.Shape(4, None, 'standard', 'normal'), now=NOW)
    result = fact_advice.advice(database, configured, routes=routes, now=NOW)
    item, _ = facts_by_key(result, QUIET)
    from api.app.routing.costs import format_amount
    assert direct.cost is not None and item['baseline'] == [
        {'currency': direct.cost.currency, 'amount': format_amount(direct.cost.micros)}]


def test_nothing_sent_says_so(database):  # noqa: F811
    routes = seeded(database)
    result = fact_advice.advice(database, values(), routes=routes, now=NOW)
    assert result['state'] == 'nothing_sent' and result['recipients'] == []
