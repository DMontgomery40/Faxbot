"""Prices by the caller ID a call presents (N15): carrier decks with origination classes, and caller-ID eligibility.

The deck fixtures follow the exact layouts carriers publish (Twilio's voice price file: ISO, Country, Description,
Price / min, Origination Prefixes, Destination Prefixes) with synthetic prices and numbers. A cheaper row applies
only to a caller ID you confirmed on that account; anonymous or malformed caller IDs get the surcharged row; the
real predictor, pricing and quote routes are used, never stand-ins. Not yet run against a real carrier's deck
download or invoice.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from api.app.config_profiles import ConfigurationDocument
from api.app.config_values import ConfigurationValues
from api.app.routing import origin_classes, origin_rates
from api.app.routing.origin_classes import (EEA, LOCAL, NON_SURCHARGED, SURCHARGED, ClassRow, DeckError, Eligibility,
                                            parse_deck, price_origin)
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)


CARIBBEAN = '"1242, 1246, 1264"'
EEA_LIST = '"30, 31, 32, 33, 34, 43, 44, 49"'
TWILIO_DECK = '\n'.join([
    'ISO,Country,Description,Price / min,Origination Prefixes,Destination Prefixes',
    'AT,Austria,Programmable Outbound Minute - Austria,0.15500,,"43, 431"',
    f'AT,Austria,Programmable Outbound Minute - Austria,0.15500,{CARIBBEAN},"43, 431"',
    'AT,Austria,Programmable Outbound Minute - Austria - Mobile,0.24000,,"43664, 4367"',
    f'AT,Austria,Programmable Outbound Minute - Austria - Mobile - from EEA,0.04500,{EEA_LIST},"43664, 4367"',
    'AT,Austria,Programmable Outbound Minute - Austria - Mobile - from US/CA,0.04500,1,"43664, 4367"',
    f'AT,Austria,Programmable Outbound Minute - Austria - from EEA,0.01600,{EEA_LIST},"43, 431"',
    'AT,Austria,Programmable Outbound Minute - Austria - from US/CA,0.01600,1,"43, 431"',
    'DE,Germany,Programmable Outbound Minute - Germany,0.01500,,49',
    '',
])
US_CALLER, BAHAMAS_CALLER, GERMAN_CALLER = '+13035550100', '+12423570000', '+4930901820'
AT_LANDLINE, AT_MOBILE, DE_LANDLINE = '+43123456789', '+436641234567', '+4930123456'


def rows(text=TWILIO_DECK, route='sip-telnyx'):
    found, skipped = parse_deck(text, route=route, captured_on=datetime(2026, 10, 9))
    assert skipped == []
    return found


def confirmed(caller, *, bought_here=False, account='sip'):
    return Eligibility(account, caller, 'confirmed', bought_here, 'Synthetic number order')


def test_twilios_layout_is_recognised_and_each_row_gets_its_origination_class():
    found = rows()
    assert origin_classes.detect_format(TWILIO_DECK) == 'twilio'
    kinds = {(row.destination_prefix, row.description.rsplit(' - ', 1)[-1], row.origination_type) for row in found}
    assert ('43', 'from EEA', EEA) in kinds and ('43', 'from US/CA', NON_SURCHARGED) in kinds
    assert ('43', 'Austria', SURCHARGED) in kinds and ('43', 'Austria', NON_SURCHARGED) in kinds  # the Caribbean list
    us = next(row for row in found if row.destination_prefix == '43' and row.description.endswith('US/CA'))
    assert us.origin_prefixes == ('1',) and us.per_minute_micros == 16000
    # Twilio's file has no billing step or currency: its rows take the card's (60-second steps, USD by default).
    assert (us.billing_increment_seconds, us.minimum_seconds, us.currency, us.deck_format) == (60, 0, 'USD', 'twilio')
    with pytest.raises(DeckError):
        parse_deck('ISO,Country,Description\nAT,Austria,x\n', route='sip-telnyx', deck_format='twilio')


def test_an_unconfirmed_caller_id_is_priced_at_the_surcharged_row_and_shown_the_cheaper_one():
    quote = price_origin(rows(), AT_LANDLINE, US_CALLER)
    assert quote.eligibility == 'unconfirmed' and quote.row.per_minute_micros == 155000
    assert quote.cheaper.per_minute_micros == 16000 and quote.origin == 'caller:unconfirmed'
    assert 'once you confirm' in quote.sentence
    # Confirmed on the account, the same caller ID gets the US/Canada row.
    quote = price_origin(rows(), AT_LANDLINE, US_CALLER, confirmed(US_CALLER))
    assert (quote.eligibility, quote.row.per_minute_micros, quote.origin) == ('confirmed', 16000,
                                                                              'caller:non_surcharged')


def test_the_longest_caller_id_prefix_wins_so_a_bahamas_number_is_not_priced_as_us():
    quote = price_origin(rows(), AT_LANDLINE, BAHAMAS_CALLER, confirmed(BAHAMAS_CALLER))
    assert quote.row.per_minute_micros == 155000 and quote.row.origin_prefixes[0] == '1242'


def test_the_longest_destination_prefix_wins_before_the_caller_id_is_looked_at():
    quote = price_origin(rows(), AT_MOBILE, GERMAN_CALLER, confirmed(GERMAN_CALLER))
    assert (quote.row.destination_prefix, quote.row.origination_type, quote.row.per_minute_micros) == (
        '43664', EEA, 45000)
    # Germany has only one row: the caller ID makes no difference there.
    quote = price_origin(rows(), DE_LANDLINE, US_CALLER)
    assert (quote.eligibility, quote.row.per_minute_micros) == ('not_needed', 15000)
    assert price_origin(rows(), '+33123456789', US_CALLER) is None  # the deck does not cover France


@pytest.mark.parametrize('caller', [None, '', 'anonymous', '+10000000000', '3035550100'])
def test_anonymous_or_malformed_caller_ids_get_the_surcharged_row(caller):
    quote = price_origin(rows(), AT_LANDLINE, caller, confirmed(US_CALLER))
    assert (quote.eligibility, quote.row.per_minute_micros, quote.origin) == ('no_caller_id', 155000, 'caller:none')


def test_a_local_row_needs_a_number_bought_on_this_account():
    deck = '\n'.join(['destination_prefix,origination_type,origination_prefixes,per_minute,billing_increment_seconds',
                      '33,surcharged,,0.050,1', '33,local,,0.010,1', '33,eea,"49, 44",0.020,1', ''])
    found = rows(deck)
    assert origin_classes.detect_format(deck) == 'faxbot' and {row.billing_increment_seconds for row in found} == {1}
    french = '+33612345678'
    assert price_origin(found, '+33123456789', french).row.per_minute_micros == 50000
    held = price_origin(found, '+33123456789', french, confirmed(french))
    assert (held.eligibility, held.row.per_minute_micros, held.cheaper.origination_type) == ('unconfirmed', 50000,
                                                                                             LOCAL)
    assert 'bought on this account' in held.sentence
    bought = price_origin(found, '+33123456789', french, confirmed(french, bought_here=True))
    assert (bought.eligibility, bought.row.origination_type, bought.row.per_minute_micros) == ('confirmed', LOCAL,
                                                                                               10000)
    withdrawn = Eligibility('sip', french, 'withdrawn', False)
    assert price_origin(found, '+33123456789', french, withdrawn).eligibility == 'unconfirmed'
    assert price_origin(found, '+33123456789', GERMAN_CALLER, confirmed(GERMAN_CALLER)).row.per_minute_micros == 20000


def test_rows_that_break_the_layout_are_refused_with_a_reason():
    with pytest.raises(DeckError):
        ClassRow('sip-telnyx', '43', EEA, (), 'USD', 1000, 60, 0)
    with pytest.raises(DeckError):
        ClassRow('sip-telnyx', '43', SURCHARGED, ('1',), 'USD', 1000, 60, 0)
    deck = 'destination_prefix,origination_type,origination_prefixes,per_minute\n43,sometimes,,0.1\n43,surcharged,,x\n'
    with pytest.raises(DeckError):
        parse_deck(deck, route='sip-telnyx')
    found, skipped = parse_deck(deck + '43,surcharged,,0.2\n', route='sip-telnyx')
    assert len(found) == 1 and len(skipped) == 2 and 'origination type' in skipped[0]
    with pytest.raises(DeckError):
        parse_deck('prefix,rate\n43,0.1\n', route='sip-telnyx')


TRUNK = {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': 'sip.telnyx.com',
         'SIP_TRUNK_CALLER_ID': US_CALLER, 'SIP_TRUNK_DIDS': US_CALLER, 'FAX_DEFAULT_COUNTRY': 'US'}


def _values(**extra):
    return ConfigurationValues.from_environment({**TRUNK, **extra})


def test_the_caller_id_priced_is_the_one_the_call_would_carry():
    from api.app.ami import originate_fields_for
    for values in (_values(), _values(FAX_REPLY_NUMBER=US_CALLER), _values(FAX_STATION_ID='+13035550199')):
        caller, how = origin_classes.presented_caller_id(values, 'sip')
        fields = originate_fields_for(values, 'a' * 32, AT_LANDLINE, '/tmp/fax.tif')
        assert how == 'trunk' and caller == fields['CallerID'] == US_CALLER
    values = _values().with_provider_accounts(ConfigurationDocument({
        'sip-vienna': {'provider': 'sip', 'label': 'Vienna trunk', 'site': 'vienna',
                       'settings': {'preset': 'telnyx', 'auth': 'ip', 'host': 'sip.telnyx.com',
                                    'caller_id': '+43123450000', 'dids': '+43123450000'}}}))
    assert origin_classes.presented_caller_id(values, 'sip-vienna')[0] == '+43123450000'
    assert origin_classes.presented_caller_id(values, 'phaxio') == (None, 'provider')


def test_imported_decks_supersede_and_confirmations_keep_their_history(database):  # noqa: F811
    upgrade_schema(database)
    first = origin_classes.import_deck(database, 'sip-telnyx', rows(), actor={'name': 'Synthetic Admin'})
    assert first['rows'] == len(rows()) and first['format'] == 'twilio' and first['published_on'] == '2026-10-09'
    assert first['by_type'][EEA] == 4 and first['imported_by'] == 'Synthetic Admin'
    cheaper = TWILIO_DECK.replace('0.01600,1,', '0.01200,1,')
    origin_classes.import_deck(database, 'sip-telnyx', rows(cheaper))
    current = origin_classes.rows_for(database, ['sip', 'sip-telnyx'], AT_LANDLINE)
    assert {row.per_minute_micros for row in current if row.origination_type == NON_SURCHARGED} == {12000, 155000}
    assert {row.destination_prefix for row in current} == {'43', '431'}
    with database.connect() as connection:
        total = connection.exec_driver_sql('SELECT count(*) FROM origin_class_rates').scalar()
        live = connection.exec_driver_sql(
            'SELECT count(*) FROM origin_class_rates WHERE superseded_at IS NULL').scalar()
    assert (total, live) == (2 * len(rows()), len(rows()))
    assert origin_classes.has_rows(database, ['sip-telnyx']) and not origin_classes.has_rows(database, ['sip-gamma'])
    record = origin_classes.record_eligibility(database, 'sip', US_CALLER, bought_here=False,
                                               evidence='Synthetic number order 1', actor={'name': 'Synthetic Admin'})
    assert record.confirmed and record.recorded_by == 'Synthetic Admin'
    withdrawn = origin_classes.withdraw_eligibility(database, 'sip', US_CALLER, note='Number released')
    assert not withdrawn.confirmed
    assert origin_classes.eligibility(database, 'sip', US_CALLER).state == 'withdrawn'
    with database.connect() as connection:
        assert connection.exec_driver_sql('SELECT count(*) FROM caller_id_eligibility').scalar() == 2
    with pytest.raises(origin_classes.EligibilityError):
        origin_classes.record_eligibility(database, 'sip', 'not a number', bought_here=False, evidence='x')
    with pytest.raises(origin_classes.EligibilityError):
        origin_classes.record_eligibility(database, 'sip', US_CALLER, bought_here=False, evidence=' ')
    with pytest.raises(origin_classes.EligibilityError):
        origin_classes.withdraw_eligibility(database, 'sip', US_CALLER)


def test_real_pricing_changes_with_the_presented_caller_id_and_its_confirmation(database):  # noqa: F811
    """The real ``pricing.price`` and ``quotes_for`` (the route chooser's figures) through the real predictor: the
    hook in ``origin_rates.rated_terms`` is wired, never skipped."""
    from api.app.routing.pricing import price, quotes_for
    from api.app.routing.seed import load_cards
    from api.app.routing.store import RouteStore
    upgrade_schema(database)
    routes = RouteStore(database, sip_preset=lambda: 'telnyx')
    routes.seed_cards(load_cards())
    values = _values()
    before = price(routes, values, 'sip', AT_LANDLINE, 1, provider='sip')
    origin_classes.import_deck(database, 'sip-telnyx', rows())
    unconfirmed = price(routes, values, 'sip', AT_LANDLINE, 1, provider='sip')
    assert unconfirmed.origin == 'caller:unconfirmed' and unconfirmed.currency == 'USD'
    assert origin_rates.origin_label(unconfirmed.origin).startswith('Caller ID not confirmed')
    origin_classes.record_eligibility(database, 'sip', US_CALLER, bought_here=False, evidence='Synthetic order')
    confirmed_price = price(routes, values, 'sip', AT_LANDLINE, 1, provider='sip')
    assert confirmed_price.origin == 'caller:non_surcharged'
    # About a minute on the line: 0.155 a minute unconfirmed, 0.016 confirmed, in whole 60-second steps.
    assert unconfirmed.micros > 9 * confirmed_price.micros > 0
    assert before.origin is None or before.origin != unconfirmed.origin
    quotes = quotes_for(routes, values, [SimpleNamespace(key='sip', provider='sip', sends=True)], AT_LANDLINE, 1)
    assert [(quote.origin, quote.micros) for quote in quotes] == [('caller:non_surcharged', confirmed_price.micros)]
    # A US number is not in the deck: the card's own price, as before.
    assert price(routes, values, 'sip', '+13035550142', 1, provider='sip').origin is None


def test_a_site_trunk_with_its_own_eligible_number_is_the_cheaper_route(database):  # noqa: F811
    """A subsidiary's trunk whose own caller ID you confirmed prices lower; the main trunk's unconfirmed caller ID
    does not, so the route choice (lowest price) picks the site trunk. No caller ID is ever changed."""
    from api.app.routing.pricing import prices_for
    from api.app.routing.seed import load_cards
    from api.app.routing.store import RouteStore
    upgrade_schema(database)
    routes = RouteStore(database, sip_preset=lambda: 'telnyx')
    routes.seed_cards(load_cards())
    values = _values().with_provider_accounts(ConfigurationDocument({
        'sip-vienna': {'provider': 'sip', 'label': 'Vienna trunk', 'site': 'vienna',
                       'settings': {'preset': 'telnyx', 'auth': 'ip', 'host': 'sip.telnyx.com',
                                    'caller_id': '+43123450000', 'dids': '+43123450000'}}}))
    origin_classes.import_deck(database, 'sip-telnyx', rows())
    found = prices_for(routes, values, AT_MOBILE, 1, keys=['sip', 'sip-vienna'])
    assert found['sip'].origin == found['sip-vienna'].origin == 'caller:unconfirmed'
    assert found['sip'].micros == found['sip-vienna'].micros
    origin_classes.record_eligibility(database, 'sip-vienna', '+43123450000', bought_here=False,
                                      evidence='Vienna office line, carrier order 2026-03-02')
    found = prices_for(routes, values, AT_MOBILE, 1, keys=['sip', 'sip-vienna'])
    assert found['sip-vienna'].origin == 'caller:eea' and found['sip-vienna'].micros < found['sip'].micros


@pytest.fixture
def trunk_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    environment = {key: value for key, value in TRUNK.items() if key != 'FAX_BACKEND'}
    for client in _serve(monkeypatch, tmp_path, FAX_BACKEND='sip', FAX_OUTBOUND_BACKEND='sip', **environment):
        yield Cli(client)


def test_the_console_routes_and_the_command_line_import_quote_confirm_and_withdraw(trunk_cli, tmp_path):
    from api.tests.test_cli import BOOTSTRAP
    client, admin = trunk_cli.client, {'X-API-Key': BOOTSTRAP}
    state = client.get('/routing/caller-id-prices', headers=admin)
    assert state.status_code == 200, state.text
    sip = next(item for item in state.json()['callers'] if item['account'] == 'sip')
    assert (sip['caller_id'], sip['eligibility'], sip['priced_by_caller_id']) == (US_CALLER, None, True)
    assert state.json()['decks'] == [] and 'Mission Control' in state.json()['layouts']['telnyx']
    deck = tmp_path / 'twilio.csv'
    deck.write_text(TWILIO_DECK)
    imported = trunk_cli('costs', 'rate-rows', 'sip-telnyx', '--caller-id-deck', deck, '--source',
                         'https://example.com/deck.csv', '--published', '2026-10-09')
    assert imported.exit_code == 0, imported.stdout + imported.stderr
    assert 'sip-telnyx: 15 prices by caller ID (4 for EEA caller IDs' in imported.stdout and '60-second steps' in imported.stdout
    assert trunk_cli('costs', 'rate-rows', 'sip-telnyx').exit_code != 0  # neither --replace nor a deck
    quoted = trunk_cli.json('providers', 'trunk', 'caller-ids', '--quote', AT_LANDLINE)
    [quote] = quoted['quotes']
    assert (quote['eligibility'], quote['row']['per_minute'], quote['cheaper']['per_minute']) == (
        'unconfirmed', '0.155', '0.016')
    refused = trunk_cli('providers', 'trunk', 'confirm-caller-id', 'sip', 'not-a-number', '--evidence', 'x')
    assert refused.exit_code != 0 and 'country code' in refused.stdout + refused.stderr
    unknown = client.post('/routing/caller-ids/confirm', headers=admin, json={
        'account': 'nobody', 'caller_id': US_CALLER, 'evidence': 'x'})
    assert unknown.status_code == 404
    confirmed_line = trunk_cli('providers', 'trunk', 'confirm-caller-id', 'sip', US_CALLER, '--evidence',
                               'Synthetic number order 1')
    assert confirmed_line.exit_code == 0 and f'Confirmed {US_CALLER} on sip.' in confirmed_line.stdout
    quoted = trunk_cli.json('providers', 'trunk', 'caller-ids', '--quote', AT_LANDLINE)
    assert (quoted['quotes'][0]['eligibility'], quoted['quotes'][0]['row']['per_minute']) == ('confirmed', '0.016')
    listed = trunk_cli('providers', 'trunk', 'caller-ids')
    assert listed.exit_code == 0 and 'Synthetic number order 1' in listed.stdout and 'Mission Control' in listed.stdout
    withdrawn = trunk_cli('providers', 'trunk', 'withdraw-caller-id', 'sip', US_CALLER, '--note', 'Released')
    assert withdrawn.exit_code == 0 and 'priced as not confirmed' in withdrawn.stdout
    again = trunk_cli('providers', 'trunk', 'withdraw-caller-id', 'sip', US_CALLER)
    assert again.exit_code != 0
    bad = client.post('/routing/rate-cards/sip-telnyx/caller-id-prices', headers=admin,
                      files={'file': ('deck.csv', b'nothing,here\n1,2\n', 'text/csv')})
    assert bad.status_code == 400 and 'destination_prefix' in bad.json()['detail']
    reader = client.get('/routing/caller-id-prices', headers={'X-API-Key': 'wrong'})
    assert reader.status_code in (401, 403)
