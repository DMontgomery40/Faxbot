"""Origin-rated prices (provider-rules design §3.7, B10): rate rows by where a call starts and the number it calls.

The row for the account's own site wins, then its country, then anywhere; within that origin the longest
destination prefix wins; with no row the card's own price applies, and an unknown price stays unknown. The
shipped rows are carriers' published per-destination prices with their source and date (AnveoDirect's rate
file); a row you save for one of your cards is read the same way. Synthetic numbers and accounts only.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from api.app.config_profiles import ConfigurationDocument
from api.app.config_values import ConfigurationValues
from api.app.routing import origin_rates
from api.app.routing.costs import RateCard, parse_amount
from api.app.routing.destinations import classify
from api.app.routing.origin_rates import ANY, OriginRate, best, origins
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)
from api.tests.test_cli import cli, server  # noqa: F401 (fixtures: the command line against a local server)
from api.tests.test_rules_delivery_http import ADMIN, US, client, publish  # noqa: F401 (fixture: the HTTPS stack)


def row(origin, prefix, minute, *, published=False, route='sip-gamma'):
    return OriginRate(route, origin, prefix, 'GBP', parse_amount(minute), 0, 0, 60, 60, None,
                      datetime(2026, 10, 8), published=published)


def test_the_most_specific_origin_with_a_matching_row_wins_then_the_longest_prefix():
    rows = [row(ANY, '44', '0.020'), row(ANY, '44113', '0.015'), row('country:GB', '44', '0.008'),
            row('leeds', '4411', '0.004'), row('leeds', '1', '0.050')]
    assert best(rows, ['leeds', 'country:GB', ANY], '+441132000000').per_minute_micros == 4000
    # Leeds has no row for London numbers: the country's row, never anywhere's longer one.
    assert best(rows, ['leeds', 'country:GB', ANY], '+442079460000').per_minute_micros == 8000
    assert best(rows, ['manchester', 'country:GB', ANY], '+441132000000').per_minute_micros == 8000
    assert best(rows, ['country:US', ANY], '+441132000000').per_minute_micros == 15000
    assert best(rows, ['country:US', ANY], '+33123456789') is None
    # A row you saved wins over a shipped one with the same origin and prefix.
    both = [row(ANY, '44', '0.020', published=True), row(ANY, '44', '0.010')]
    assert best(both, [ANY], '+441132000000').per_minute_micros == 10000


def test_where_an_accounts_calls_start_comes_from_its_site_and_the_sites_country():
    values = ConfigurationValues.from_environment({'FAX_DEFAULT_COUNTRY': 'US'}).with_provider_accounts(
        ConfigurationDocument({'sinch-uk': {'provider': 'sinch', 'site': 'leeds',
                                            'settings': {'project_id': 'p'}, 'credentials': {}}}))
    sites = {'leeds': {'key': 'leeds', 'name': 'Leeds office', 'country': 'GB'},
             'denver': {'key': 'denver', 'name': 'Denver', 'country': 'US', 'accounts': ['sinch']}}
    assert origins(values, 'sinch-uk', sites=sites) == ['leeds', 'country:GB', ANY]
    assert origins(values, 'sinch', sites=sites) == ['denver', 'country:US', ANY]
    assert origins(values, 'humblefax', sites=sites) == ['country:US', ANY]
    assert origins(values, 'humblefax', sites=sites, site='leeds') == ['leeds', 'country:GB', ANY]
    assert origin_rates.origin_label('leeds', sites) == 'Leeds office'
    assert origin_rates.origin_label('country:GB') == 'United Kingdom'
    assert origin_rates.origin_label(ANY) == 'Anywhere'


def test_published_rows_are_read_with_their_source_and_carriers_that_publish_none_have_no_row():
    shipped = origin_rates.shipped()
    anveo = {item.destination_prefix: item for item in shipped if item.route == 'sip-anveo'}
    assert {'44', '4420', '447', '353', '49', '33', '61'} <= set(anveo)
    mobile = anveo['447']
    assert (mobile.per_minute_micros, mobile.billing_increment_seconds, mobile.minimum_seconds) == (122110, 1, 1)
    assert mobile.source_url == 'https://www.anveo.com/anveodirect.standard.csv' and mobile.published
    assert mobile.captured_on == datetime(2026, 10, 8)
    assert not [item for item in shipped if item.route in ('sip-telnyx', 'sip-gamma', 'sip-bt-one-voice')]


def test_the_predictor_prices_a_uk_mobile_and_a_uk_landline_by_their_own_rows():
    from api.app.routing.predict import Shape, predict_from
    from api.app.routing.predict_facts import facts_for
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'anveo',
                                                   'SIP_TRUNK_AUTH': 'ip', 'FAX_DEFAULT_COUNTRY': 'US'})
    landline = facts_for('sip', '+442079460000', values=values, engine=None)
    mobile = facts_for('sip', '+447911123456', values=values, engine=None)
    assert (landline.origin, mobile.origin) == (ANY, ANY)
    assert landline.terms.card.per_minute_micros == 2410 and mobile.terms.card.per_minute_micros == 122110
    shape = Shape(1, None, 'standard', 'normal')
    assert predict_from(mobile, shape).cost.micros > 40 * predict_from(landline, shape).cost.micros
    # A toll-free number keeps its own class's price, never a row's.
    assert facts_for('sip', '+18005550100', values=values, engine=None).origin is None


def test_rows_you_enter_are_saved_listed_and_quoted_through_the_command_line(cli, tmp_path):
    import json
    cards = tmp_path / 'cards.json'
    cards.write_text(json.dumps({'cards': [{'provider_id': 'sip-gamma', 'direction': 'outbound', 'label': 'Gamma',
                                            'currency': 'GBP', 'per_minute': '0.010', 'per_page': '0',
                                            'per_call': '0', 'billing_increment_seconds': 60, 'minimum_seconds': 60,
                                            'captured_on': '2026-10-03T00:00:00'}]}))
    assert cli('costs', 'rate-cards', '--replace', cards).exit_code == 0
    rows = tmp_path / 'rows.json'
    rows.write_text(json.dumps({'rows': [
        {'origin': 'country:GB', 'destination_prefix': '+44', 'per_minute': '0.006', 'billing_increment_seconds': 60,
         'minimum_seconds': 60, 'captured_on': '2026-10-08T00:00:00'},
        {'origin': 'any', 'destination_prefix': '1', 'per_minute': '0.02'}]}))
    saved = cli.json('costs', 'rate-rows', 'sip-gamma', '--replace', rows)
    assert [(row['origin_label'], row['destination_prefix'], row['per_minute']) for row in saved['rows']] == [
        ('Anywhere', '+1', '0.02'), ('United Kingdom', '+44', '0.006')]
    listed = {card['provider_id']: card for card in cli.json('costs', 'rate-cards')['cards']}
    assert [row['origin'] for row in listed['sip-gamma']['rows']] == ['any', 'country:GB']
    shown = cli('costs', 'rate-cards')
    assert shown.exit_code == 0 and 'Gamma: prices by where calls start' in shown.stdout
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'rows': [{'origin': 'Not A Site!', 'destination_prefix': '44'}]}))
    refused = cli('costs', 'rate-rows', 'sip-gamma', '--replace', bad)
    assert refused.exit_code != 0 and 'Where calls start is a site' in refused.stdout + refused.stderr
    assert cli('costs', 'rate-rows', 'no-such-card', '--replace', rows).exit_code != 0
    advice = cli('costs', 'recommendations', 'trunks')
    assert advice.exit_code == 0, advice.stdout + advice.stderr


def _card(provider, minute='0.010', currency='GBP'):
    return RateCard(None, provider, 'outbound', provider.title(), currency, parse_amount(minute), 0, 0, 60, 60, None,
                    datetime(2026, 10, 3))


def test_a_row_you_save_for_a_sites_trunk_prices_its_calls_and_the_quote_says_where_from(database):  # noqa: F811
    from api.app.routing.pricing import price, quotes_for
    from api.app.routing.store import RouteStore
    upgrade_schema(database)
    routes = RouteStore(database, sip_preset=lambda: 'telnyx')
    [gamma] = routes.replace_cards([_card('sip-gamma', '0.010')])
    saved = origin_rates.save_rows(database, gamma.id, [
        row('leeds', '44', '0.004'), row('country:GB', '44', '0.006')])
    assert {(item.origin, item.per_minute_micros) for item in saved} == {('leeds', 4000), ('country:GB', 6000)}
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip',
        'FAX_DEFAULT_COUNTRY': 'US'}).with_provider_accounts(ConfigurationDocument({
            'sip-leeds': {'provider': 'sip', 'site': 'leeds', 'label': 'Leeds trunk',
                          'settings': {'preset': 'gamma', 'auth': 'ip', 'host': '192.0.2.40'}}}))
    leeds = price(routes, values, 'sip-leeds', '+441132000000', 1, provider='sip')
    assert (leeds.origin, leeds.currency) == ('leeds', 'GBP')
    # A UK number from the Leeds trunk: 60 s minimum at 0.004 a minute, plus nothing per page.
    assert leeds.micros == 4000
    elsewhere = price(routes, values, 'sip-leeds', '+15555550199', 1, provider='sip')
    assert elsewhere.origin is None  # no row for US numbers: the card's own price
    quotes = quotes_for(routes, values, [SimpleNamespace(key='sip-leeds', provider='sip', sends=True)],
                        '+441132000000', 1)
    assert [(quote.account, quote.origin, quote.micros) for quote in quotes] == [('sip-leeds', 'leeds', 4000)]
    # A new price supersedes the old rows; nothing is changed or deleted.
    origin_rates.save_rows(database, gamma.id, [row('leeds', '44', '0.003')])
    assert [(item.origin, item.per_minute_micros) for item in origin_rates.saved(database, ['sip-gamma'])] == [
        ('leeds', 3000)]
    with database.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM provider_rate_rows').scalar() == 3
    with pytest.raises(Exception):
        origin_rates.save_rows(database, 'no-such-card', [])
    assert classify('+441132000000', 'US').kind == 'international'


def test_a_quote_from_a_site_prices_each_account_from_that_site_and_names_it(client):  # noqa: F811
    """GET /routing/quote?site= (``faxbot savings fax --from-site``) prices each allowed account as a call from the
    site, with the site's name from the organization's rules: the price and the name come only from the real
    pricing and the real rules store, never from a fallback."""
    publish(client, {'format': 1, 'sites': [{'key': 'leeds', 'name': 'Leeds office', 'country': 'GB',
                                              'time_zone': 'Europe/London'}]})
    saved = client.put('/routing/rate-cards/phaxio/rows', headers=ADMIN, json={'rows': [
        {'origin': 'leeds', 'destination_prefix': '1', 'per_page': '0.05'}]})
    assert saved.status_code == 200, saved.text
    assert [row['origin_label'] for row in saved.json()['rows']] == ['Leeds office']
    plain = client.get('/routing/quote', headers=ADMIN, params={'to': US, 'pages': 2})
    phaxio = {item['account']: item for item in plain.json()['quotes']}['phaxio']
    assert phaxio['estimate'] == {'currency': 'USD', 'amount': '0.14'} and phaxio['origin'] is None
    quoted = client.get('/routing/quote', headers=ADMIN, params={'to': US, 'pages': 2, 'site': 'leeds'})
    assert quoted.status_code == 200, quoted.text
    phaxio = {item['account']: item for item in quoted.json()['quotes']}['phaxio']
    assert phaxio['estimate'] == {'currency': 'USD', 'amount': '0.10'}
    assert (phaxio['origin'], phaxio['origin_label']) == ('leeds', 'Leeds office')
