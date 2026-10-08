"""Number advice from synthetic records: NPPES evidence (M18 b and c), where each number should live (M24), and
US prices by where a call really starts (jurisdiction).

NPPES answers come from fixtures shaped like the registry API (``fixtures/nppes``, with their source); the
registry itself is never called: the real network read is replaced by one that fails the test. Rate files are
synthetic rows in AnveoDirect's published column layout. Advice only: nothing here ports a number, changes
caller ID or blocks a fax.
"""
from datetime import datetime, timedelta
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from api.app.config_profiles import ConfigurationDocument
from api.app.config_values import ConfigurationValues
from api.app.routing import jurisdiction, nppes, number_placement, origin_rates
from api.app.routing.costs import parse_amount
from api.app.routing.receiving import receiving_report
from api.app.routing.seed import load_cards
from api.app.routing.store import RouteStore
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)


NOW = datetime(2026, 10, 8, 12, 0)
FIXTURES = Path(__file__).parent / 'fixtures' / 'nppes'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def no_registry(monkeypatch):
    """The real registry is never asked; a test that reaches it fails."""
    def refuse(params, *, timeout=10.0):
        raise AssertionError(f'NPPES was called for real with {params}')
    monkeypatch.setattr(nppes, '_fetch', refuse)
    monkeypatch.setattr(nppes, '_LOOKUPS', {})   # each test starts with no lookup remembered


class Registry:
    """A fake registry answering from fixtures, counting questions."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, params):
        self.asked.append(dict(params))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def trunk_values(**extra):
    environment = {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip',
                   'SIP_TRUNK_HOST': 'sip.telnyx.com', 'SIP_TRUNK_DIDS': '+13035550100,+13035550101',
                   'FAX_DEFAULT_COUNTRY': 'US', 'INBOUND_ENABLED': 'true', **extra}
    return ConfigurationValues.from_environment(environment)


def received(engine, number, count, *, pages=5, days_ago=1, backend='sip'):
    table = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        for index in range(count):
            when = NOW - timedelta(days=days_ago, minutes=index)
            connection.execute(table.insert().values(
                id=uuid4().hex, from_number='+18015550100', to_number=number, status='received', backend=backend,
                pages=pages, created_at=when, received_at=when, updated_at=when))


def sent_job(engine, number):
    table = sa.Table('fax_jobs', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id=uuid4().hex, to_number=number, file_name='synthetic.pdf',
                                                 tiff_path='', status='SUCCESS', pages=1, backend='sip',
                                                 created_at=NOW, updated_at=NOW))


# -- reading the registry --------------------------------------------------------------------------------------------

def test_a_registry_record_keeps_every_address_number_and_endpoint():
    records = nppes.records_from(fixture('search_organization.json'))
    assert [record.npi for record in records] == ['1234567893', '1245319599']
    clinic = records[0]
    assert (clinic.name, clinic.enumeration_type) == ('SYNTHETIC HEALTH CLINIC', 'NPI-2')
    assert [(address.purpose, address.fax, address.state) for address in clinic.addresses] == [
        ('location', '+13035550111', 'CO'), ('mailing', '+18665550112', 'CO'), ('practice', '+17205550114', 'CO')]
    # Direct and FHIR addresses stay available to other readers (Direct delivery).
    assert [(item['type'], item['endpoint']) for item in clinic.endpoints] == [
        ('DIRECT', 'records@direct.synthetic-clinic.example.org'), ('FHIR', 'https://fhir.synthetic-clinic.example.org/r4')]
    assert ('+13035550110', 'phone') in [(number, kind) for number, kind, _ in clinic.numbers()]


def test_a_registry_error_is_unknown_never_an_empty_answer():
    with pytest.raises(nppes.RegistryError, match='No valid search criteria'):
        nppes.records_from(fixture('errors.json'))
    assert nppes.records_from({'result_count': 0, 'results': []}) == []
    with pytest.raises(nppes.RegistryError):
        nppes.records_from(['not', 'an', 'answer'])


def test_npi_check_digit_area_code_states_and_names():
    assert nppes.valid_npi('1234567893') and not nppes.valid_npi('1234567890') and not nppes.valid_npi('12345')
    assert [nppes.us_state(number) for number in ('+13035550100', '+12125550100', '+18015550100', '+18665550100',
                                                  '+16045550100')] == ['CO', 'NY', 'UT', None, None]
    assert nppes.same_provider('Synthetic Health', 'SYNTHETIC HEALTH CLINIC')
    assert nppes.same_provider('Dr. Ada Example MD', 'ADA EXAMPLE')
    assert not nppes.same_provider('Synthetic Health Clinic', 'SYNTHETIC HEALTH IMAGING LLC')


# -- your own record (M18 b) -------------------------------------------------------------------------------------------

def test_your_npi_record_marks_a_quiet_number_still_printed_there(database):  # noqa: F811
    upgrade_schema(database)
    store = nppes.NppesStore(database)
    with pytest.raises(nppes.NppesInputError):
        store.add('1234567890')
    assert nppes.own_record(database)['sentence'].startswith('Add your NPI')
    assert nppes.published_numbers(database) is None  # no NPI: nothing is known either way
    store.add('1234567893', 'Denver office', by_name='Ada', now=NOW)
    with pytest.raises(nppes.NppesInputError, match='already one of yours'):
        store.add('1234567893')
    assert nppes.published_numbers(database) is None  # set but never read: still unknown
    registry = Registry(fixture('own_record.json'))
    assert nppes.refresh_own(database, fetch=registry, now=NOW)['read'] == ['1234567893']
    assert registry.asked == [{'number': '1234567893'}]
    view = nppes.own_record(database)
    assert view['sentence'] == 'Faxbot last read your NPI record on October 8, 2026.'
    assert {item['display'] for item in view['npis'][0]['numbers']} == {'+1 720-555-0199', '+1 303-555-0100'}
    published = nppes.published_numbers(database)
    assert nppes.npi_evidence(published, '+17205550199')['sentence'] == (
        'Still printed on your NPI record as your practice location fax number: keep it.')
    assert nppes.npi_evidence(published, '+13035550101') == {'state': 'not_listed', 'read_at': None, 'sentence': None}
    # A failed read keeps the last one: unknown now is not "no longer listed".
    assert nppes.refresh_own(database, fetch=Registry(httpx.ConnectTimeout('synthetic')))['failed'] == ['1234567893']
    assert '+17205550199' in nppes.published_numbers(database)
    store.remove('1234567893', by_name='Ada')
    assert nppes.published_numbers(database) is None
    with database.connect() as connection:  # the read stays as history
        assert connection.execute(sa.text('SELECT count(*) FROM nppes_reads')).scalar() == 1


def test_the_quiet_number_advice_says_keep_it_when_your_npi_record_lists_it(database):  # noqa: F811
    upgrade_schema(database)
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'humblefax', 'HUMBLEFAX_ACCESS_KEY': 'synthetic', 'HUMBLEFAX_SECRET_KEY': 'synthetic',
        'HUMBLEFAX_FROM_NUMBER': '+17205550199', 'FAX_DEFAULT_COUNTRY': 'US'})
    received(database, '+17205550199', 1, days_ago=40, backend='humblefax')   # history reaches back 40 days
    routes = RouteStore(database)
    before = receiving_report(database, routes, values, now=NOW)['provider_numbers']['numbers'][0]
    assert before['quiet'] and before['question'].startswith('Is +1 720-555-0199 still printed')
    assert before['npi_record'] is None
    nppes.NppesStore(database).add('1234567893', now=NOW)
    nppes.refresh_own(database, fetch=Registry(fixture('own_record.json')), now=NOW)
    after = receiving_report(database, routes, values, now=NOW)['provider_numbers']['numbers'][0]
    assert after['question'] == 'Still printed on your NPI record as your practice location fax number: keep it.'
    assert after['npi_record']['state'] == 'listed'


# -- before the first fax (M18 c) --------------------------------------------------------------------------------------

def test_a_number_listed_for_another_provider_warns_without_asking_the_registry(database):  # noqa: F811
    upgrade_schema(database)
    nppes.NppesStore(database).record(nppes.records_from(fixture('search_organization.json')), 'lookup', now=NOW)
    values = trunk_values()
    other = nppes.recipient_check(database, values, '+13035550121', name='Synthetic Health Clinic', now=NOW)
    assert other['warning'] and other['state'] == 'listed_for_other'
    assert other['sentence'] == 'This number is listed for SYNTHETIC HEALTH IMAGING LLC in NPPES, not Synthetic Health Clinic.'
    same = nppes.recipient_check(database, values, '+17205550114', name='Synthetic Health', now=NOW)
    assert not same['warning'] and same['state'] == 'listed_for_named' and same['listed'][0]['where'] == (
        'other practice location')
    phone = nppes.recipient_check(database, values, '+13035550110', name='Synthetic Health Clinic', now=NOW)
    assert phone['warning'] and phone['sentence'] == (
        'NPPES lists this number as the telephone number of SYNTHETIC HEALTH CLINIC, not a fax number.')
    # The hook a send path runs reads only what is stored, so it adds no wait to a send.
    hook = nppes.check_before_sending(database, values, '+13035550121', 'Synthetic Health Clinic')
    assert hook['state'] == 'listed_for_other'
    assert nppes.check_before_sending(database, values, '+13035550199', 'Anyone') is None


def test_the_named_provider_lookup_names_its_listed_fax_and_is_reused_for_a_day(database):  # noqa: F811
    upgrade_schema(database)
    registry = Registry(fixture('search_organization.json'))
    values = trunk_values()
    answer = nppes.recipient_check(database, values, '+13035550199', name='Synthetic Health Clinic', fetch=registry,
                                   now=NOW)
    assert answer['warning'] and answer['state'] == 'named_lists_other'
    assert answer['sentence'] == "NPPES lists +1 303-555-0111 as SYNTHETIC HEALTH CLINIC's fax number, not this one."
    assert registry.asked == [{'organization_name': 'Synthetic Health Clinic*', 'enumeration_type': 'NPI-2',
                               'limit': 50, 'state': 'CO'}]
    again = nppes.recipient_check(database, values, '+13035550198', name='Synthetic Health Clinic', fetch=registry,
                                  now=NOW + timedelta(hours=2))
    assert again['state'] == 'named_lists_other' and len(registry.asked) == 1
    # The search results became evidence: a later check finds the imaging centre's number without a question.
    listed = nppes.recipient_check(database, values, '+13035550121', name='Synthetic Health Clinic', fetch=registry,
                                   now=NOW + timedelta(hours=3))
    assert listed['state'] == 'listed_for_other' and len(registry.asked) == 1


def test_a_name_nppes_does_not_know_is_asked_once_while_the_sender_checks_again(database):  # noqa: F811
    upgrade_schema(database)
    registry = Registry({'result_count': 0, 'results': []})
    values = trunk_values()
    for minutes, number in ((0, '+13035550197'), (1, '+13035550196'), (2, '+13035550195')):
        answer = nppes.recipient_check(database, values, number, name='Nobody Here', fetch=registry,
                                       now=NOW + timedelta(minutes=minutes))
        assert answer['state'] == 'not_found'
    # One lookup: as an organization, then as a person; the checks after it ask nothing.
    assert [sorted(question) for question in registry.asked] == [
        ['enumeration_type', 'limit', 'organization_name', 'state'],
        ['enumeration_type', 'first_name', 'last_name', 'limit', 'state']]
    # A day later the registry is asked again: it may have the provider by then.
    nppes.recipient_check(database, values, '+13035550194', name='Nobody Here', fetch=registry,
                          now=NOW + timedelta(days=1, minutes=1))
    assert len(registry.asked) == 4


@pytest.mark.parametrize('failure', [fixture('errors.json'), httpx.ReadTimeout('synthetic'),
                                     httpx.ConnectError('synthetic'), ValueError('not JSON')])
def test_a_registry_failure_leaves_the_number_not_checked(database, failure):  # noqa: F811
    upgrade_schema(database)
    answer = nppes.recipient_check(database, trunk_values(), '+13035550199', name='Synthetic Health Clinic',
                                   fetch=Registry(failure), now=NOW)
    assert answer['state'] == 'not_checked' and not answer['warning'] and not answer['checked']
    assert answer['sentence'] == 'Faxbot could not reach NPPES, so this number was not checked.'


def test_numbers_faxed_before_abroad_or_unnamed_are_not_looked_up(database):  # noqa: F811
    upgrade_schema(database)
    registry = Registry(fixture('search_organization.json'))
    values = trunk_values()
    sent_job(database, '+13035550199')
    assert nppes.recipient_check(database, values, '+13035550199', name='X Clinic', fetch=registry)['state'] == (
        'sent_before')
    assert nppes.recipient_check(database, values, '+442079460000', name='X Clinic', fetch=registry)['state'] == (
        'outside_us')
    assert nppes.recipient_check(database, values, '+13035550198', fetch=registry)['state'] == 'no_name'
    assert registry.asked == []
    nothing = nppes.recipient_check(database, values, '+13035550197', name='Nobody Here',
                                    fetch=Registry({'result_count': 0, 'results': []}), now=NOW)
    assert nothing['state'] == 'not_found' and not nothing['warning']
    assert nothing['sentence'] == ('NPPES lists no provider named Nobody Here in Colorado, so Faxbot could not '
                                   'check this number.')


# -- where each number should live (M24) --------------------------------------------------------------------------------

ANVEO = {'provider': 'sip', 'label': 'AnveoDirect trunk', 'receives': True, 'numbers': ['+17205550150'],
         'settings': {'preset': 'anveo', 'auth': 'ip', 'host': '192.0.2.50'}}


def two_trunks():
    return trunk_values().with_provider_accounts(ConfigurationDocument({'sip-anveo': ANVEO}))


def seeded(engine):
    routes = RouteStore(engine, sip_preset=lambda: 'telnyx')
    routes.seed_cards(load_cards())
    return routes


def test_a_quiet_number_moves_to_the_cheaper_trunk_with_both_carriers_porting_steps(database):  # noqa: F811
    upgrade_schema(database)
    routes = seeded(database)
    result = number_placement.placement(database, two_trunks(), routes=routes, now=NOW)
    rows = {row['number']: row for row in result['numbers']}
    quiet = rows['+13035550101']
    # Telnyx publishes $1.00 a month a number; AnveoDirect's card says $0.15; no faxes were received.
    assert quiet['state'] == 'move' and quiet['cheapest'] == 'AnveoDirect trunk'
    assert quiet['sentence'].startswith('Move +1 303-555-0101 from Telnyx to AnveoDirect trunk: about $0.85 a month less')
    steps = quiet['porting']
    assert steps['from'] == 'Telnyx' and steps['to'] == 'AnveoDirect'
    assert 'port-out PIN' in steps['steps'][0] and 'LNP@ANVEO.COM' in steps['steps'][1]
    assert steps['fee'].startswith('AnveoDirect: $15 a US or Canadian number')
    assert any(source['url'].startswith('https://support.telnyx.com') for source in steps['sources'])
    assert result['state'] == 'advice' and result['note'].startswith('Faxbot only advises')


def test_a_busy_number_stays_and_a_plan_whose_number_costs_less_elsewhere_is_named(database):  # noqa: F811
    upgrade_schema(database)
    routes = seeded(database)
    values = trunk_values(FAX_INBOUND_BACKEND='humblefax', HUMBLEFAX_ACCESS_KEY='synthetic',
                          HUMBLEFAX_SECRET_KEY='synthetic', HUMBLEFAX_FROM_NUMBER='+17205550199',
                          FAX_OUTBOUND_ROUTES='humblefax')
    received(database, '+13035550100', 40)
    result = number_placement.placement(database, values, routes=routes, now=NOW)
    rows = {row['number']: row for row in result['numbers']}
    # HumbleFax's plan includes its one number, and it publishes no price for a second, so nothing moves there.
    assert rows['+13035550100']['state'] == 'keep'
    assert 'it has no price at HumbleFax' in rows['+13035550100']['sentence']
    assert rows['+17205550199']['sentence'] == 'Keep +1 720-555-0199 at HumbleFax: it is included in your HumbleFax plan, the least of your accounts.'
    worth = result['accounts'][0]
    assert worth['account'] == 'HumbleFax' and worth['saving'] == [{'currency': 'USD', 'amount': '9.00'}]
    assert 'check first that it sends no faxes you need' in worth['sentence']
    assert '30 days' in worth['porting'][0]['steps'][0]   # HumbleFax wants 30 days' written notice
    assert result['state'] == 'account'


def test_a_toll_free_number_never_moves_to_an_account_that_takes_none(database):  # noqa: F811
    upgrade_schema(database)
    values = trunk_values(SIP_TRUNK_DIDS='+18665550100', FAX_INBOUND_BACKEND='humblefax',
                          HUMBLEFAX_ACCESS_KEY='synthetic', HUMBLEFAX_SECRET_KEY='synthetic',
                          HUMBLEFAX_FROM_NUMBER='+17205550199')
    result = number_placement.placement(database, values, routes=seeded(database), now=NOW)
    toll_free = next(row for row in result['numbers'] if row['number'] == '+18665550100')
    assert 'HumbleFax does not take toll-free numbers.' in toll_free['skipped']
    assert toll_free['state'] != 'move'


def test_every_porting_fact_has_a_source_and_unknown_fees_stay_unknown():
    facts = number_porting_facts()
    for key, item in facts.items():
        for side in ('port_in', 'port_out'):
            assert side in item, key
        assert item['port_in']['sources'], key
        for source in item['port_in']['sources'] + item['port_out']['sources']:
            assert source['url'].startswith('https://'), key
    assert facts['flowroute']['port_in']['fee'] is None   # not published: never guessed
    steps = number_placement.porting_steps('humblefax', 'flowroute')
    assert steps['fee'] == 'Flowroute publishes no porting fee.'
    assert 'never cancel first' in steps['steps'][2]


def number_porting_facts():
    number_placement.porting.cache_clear()
    return number_placement.porting()


# -- prices by where a call really starts --------------------------------------------------------------------------------

DECK = ('destination,prefix,rate_inter,rate_intra,billing\n'
        'USA,1303555,0.00200,0.01000,1-1\n'
        'USA,1801555,0.00300,0.00100,1-1\n'
        'USA,1801,0.00400,0.00400,6-6\n'
        'United Kingdom,44,0.00241,0.00241,1-1\n')


def test_a_rate_file_keeps_us_rows_and_a_newer_import_supersedes_the_older(database):  # noqa: F811
    upgrade_schema(database)
    rates = jurisdiction.parse_rows(DECK, 'sip-anveo', source_url='https://www.anveo.com/anveodirect.standard.csv',
                                    captured_on=NOW)
    assert [(rate.prefix, rate.interstate_micros, rate.intrastate_micros, rate.minimum_seconds,
             rate.billing_increment_seconds) for rate in rates] == [
        ('1303555', 2000, 10000, 1, 1), ('1801555', 3000, 1000, 1, 1), ('1801', 4000, 4000, 6, 6)]
    with pytest.raises(jurisdiction.JurisdictionInputError):
        jurisdiction.parse_rows('prefix,price\n1303,0.01\n', 'sip-anveo')
    jurisdiction.import_rows(database, 'sip-anveo', rates, now=NOW)
    summary = jurisdiction.import_rows(database, 'sip-anveo', rates[:2], now=NOW + timedelta(minutes=1))
    assert summary == [{'route': 'sip-anveo', 'carrier': 'AnveoDirect', 'rows': 2, 'differ': 2,
                        'source_url': 'https://www.anveo.com/anveodirect.standard.csv', 'read_on': '2026-10-08',
                        'imported_at': NOW + timedelta(minutes=1)}]
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT count(*) FROM jurisdiction_rates')).scalar() == 5  # history kept
    assert jurisdiction.rate_for(database, ['sip-anveo'], '+18015550100').prefix == '1801555'


def test_a_call_is_priced_from_its_sites_state_and_never_by_caller_id(database):  # noqa: F811
    upgrade_schema(database)
    jurisdiction.import_rows(database, 'sip-anveo', jurisdiction.parse_rows(DECK, 'sip-anveo', captured_on=NOW))
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'anveo',
                                                   'SIP_TRUNK_AUTH': 'ip', 'FAX_DEFAULT_COUNTRY': 'US'})
    from api.app.routing.destinations import classify
    sites = {'denver': {'key': 'denver', 'name': 'Denver', 'country': 'US', 'state': 'CO', 'accounts': ['sip']}}
    same_state, row = origin_rates.rated_terms(['sip', 'sip-anveo'], '+13035550100', classify('+13035550100', 'US'),
                                               values=values, account_key='sip', engine=database, sites=sites)
    assert row.origin == 'intrastate' and row.per_minute_micros == 10000
    between, row = origin_rates.rated_terms(['sip', 'sip-anveo'], '+18015550100', classify('+18015550100', 'US'),
                                            values=values, account_key='sip', engine=database, sites=sites)
    assert row.origin == 'interstate' and row.per_minute_micros == 3000
    assert origin_rates.origin_label('intrastate') == 'Within one state'
    # A site with no state: where the call starts is unknown, so no jurisdiction price is used.
    unknown, row = origin_rates.rated_terms(['sip', 'sip-anveo'], '+13035550100', classify('+13035550100', 'US'),
                                            values=values, account_key='sip', engine=database,
                                            sites={'denver': {**sites['denver'], 'state': None}})
    assert row is None
    assert jurisdiction.NO_CALLER_ID == ('Faxbot never changes caller ID to lower call charges (FCC, Truth in Caller '
                                         'ID; 47 CFR 64.1601).')


def _publish_sites(engine, sites):
    from api.app.rules.store import RuleStore
    store = RuleStore(engine)
    store.save_draft('organization', '', {'format': 1, 'sites': sites}, expected_version=0)
    store.publish('organization', '', expected_active_revision=None, expected_draft_version=1)


def test_the_predictor_prices_a_call_from_its_published_sites_state(database):  # noqa: F811
    from api.app.routing.pricing import price
    upgrade_schema(database)
    jurisdiction.import_rows(database, 'sip-anveo', jurisdiction.parse_rows(DECK, 'sip-anveo', captured_on=NOW))
    _publish_sites(database, [{'key': 'denver', 'name': 'Denver', 'country': 'US', 'state': 'CO',
                               'accounts': ['sip']}])
    routes = RouteStore(database, sip_preset=lambda: 'anveo')
    routes.seed_cards(load_cards())
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'anveo',
                                                   'SIP_TRUNK_AUTH': 'ip', 'FAX_DEFAULT_COUNTRY': 'US'})
    within_state = price(routes, values, 'sip', '+13035550100', 1, provider='sip')
    between = price(routes, values, 'sip', '+18015550100', 1, provider='sip')
    assert (within_state.origin, between.origin) == ('intrastate', 'interstate')
    # One page, about a minute on the line, billed by the second at $0.010 and $0.003 a minute.
    assert within_state.micros > 3 * between.micros > 0
    advice = jurisdiction.site_advice(database, values, now=NOW)   # sites read from the published rules
    assert advice['carriers'][0]['by_jurisdiction'] is True


def test_telnyx_publishes_one_us_price_so_where_a_call_starts_changes_nothing(database):  # noqa: F811
    upgrade_schema(database)
    advice = jurisdiction.site_advice(database, trunk_values(), now=NOW, sites={})
    assert advice['carriers'] == [{'account': 'sip', 'carrier': 'Telnyx', 'by_jurisdiction': False,
                                   'sentence': 'Telnyx publishes one price for US calls, so where a call starts makes '
                                               'no difference.'}]
    assert advice['sentence'] == advice['carriers'][0]['sentence'] and advice['items'] == []
    # The shipped Telnyx card is one US price a minute, whatever state a call starts in.
    telnyx = [card for card in load_cards() if card.provider_id == 'sip-telnyx' and card.direction == 'outbound']
    assert [card.per_minute_micros for card in telnyx] == [parse_amount('0.005')]


def _sent_call(engine, trunk_key, destination, seconds, when):
    tables = {name: sa.Table(name, sa.MetaData(), autoload_with=engine)
              for name in ('fax_jobs', 'outbound_attempts', 'delivery_attempt_costs', 'sip_call_records')}
    job, attempt = uuid4().hex, uuid4().hex
    with engine.begin() as connection:
        connection.execute(tables['fax_jobs'].insert().values(
            id=job, to_number=destination, file_name='synthetic.pdf', tiff_path='', status='SUCCESS', pages=1,
            backend='sip', created_at=when, updated_at=when))
        connection.execute(tables['outbound_attempts'].insert().values(
            id=attempt, job_id=job, sequence=1, phase='success', created_at=when, submitted_at=when,
            completed_at=when))
        connection.execute(tables['delivery_attempt_costs'].insert().values(
            id=attempt, job_id=job, destination=destination, route='sip', route_reason='rule', provider_id='sip',
            billed_seconds=seconds, currency='USD', billing_checks=0, outcome='success', created_at=when,
            updated_at=when))
        connection.execute(tables['sip_call_records'].insert().values(
            id=uuid4().hex, direction='outbound', call_id=uuid4().hex, attempt_id=attempt, started_at=when,
            answered_at=when, ended_at=when + timedelta(seconds=seconds), disposition='answered', t38='yes',
            fax_preference=0, connected_seconds=seconds, trunk_key=None if trunk_key == 'sip' else trunk_key,
            created_at=when, updated_at=when))


def test_the_site_whose_trunk_costs_less_for_a_states_numbers_is_named(database):  # noqa: F811
    upgrade_schema(database)
    jurisdiction.import_rows(database, 'sip-anveo', jurisdiction.parse_rows(DECK, 'sip-anveo', captured_on=NOW))
    slc = {'provider': 'sip', 'label': 'Salt Lake City trunk', 'receives': True, 'numbers': ['+18015550160'],
           'settings': {'preset': 'anveo', 'auth': 'ip', 'host': '192.0.2.60'}}
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'anveo', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': '192.0.2.61',
        'FAX_DEFAULT_COUNTRY': 'US'}).with_provider_accounts(ConfigurationDocument({'sip-slc': slc}))
    sites = {'denver': {'key': 'denver', 'name': 'Denver', 'country': 'US', 'state': 'CO', 'accounts': ['sip']},
             'slc': {'key': 'slc', 'name': 'Salt Lake City', 'country': 'US', 'state': 'UT', 'accounts': ['sip-slc']}}
    for index in range(20):   # Denver to Utah: interstate at $0.003 from Denver, intrastate at $0.001 from SLC
        _sent_call(database, 'sip', '+18015550100', 600, NOW - timedelta(days=1, minutes=index))
    advice = jurisdiction.site_advice(database, values, now=NOW, sites=sites)
    item = advice['items'][0]
    assert (item['from_site'], item['to_site'], item['state'], item['faxes']) == ('Denver', 'Salt Lake City', 'UT', 20)
    # 20 calls of 10 minutes: $0.60 from Denver, $0.20 from Salt Lake City.
    assert item['saving'] == [{'currency': 'USD', 'amount': '0.40'}]
    assert item['sentence'] == ('Faxes from Denver to Utah numbers would cost about $0.40 less a month from your Salt '
                                'Lake City trunk (estimate).')
    assert 'sends from the Salt Lake City site' in item['action']
    assert advice['carriers'][0]['by_jurisdiction'] is True


def test_a_us_site_takes_a_two_letter_state_and_nothing_else_does():
    from api.app.rules.compile import document_problems

    def state_problems(site):
        return [problem for problem in document_problems('organization', {'sites': [site]})
                if 'two-letter US state code' in problem.message]
    assert state_problems({'key': 'denver', 'name': 'Denver', 'country': 'US', 'state': 'CO'}) == []
    assert state_problems({'key': 'denver', 'name': 'Denver', 'state': 'CO'}) == []   # the installation's country
    assert state_problems({'key': 'x', 'name': 'X', 'country': 'US', 'state': 'XX'})
    assert state_problems({'key': 'leeds', 'name': 'Leeds', 'country': 'GB', 'state': 'CO'})
