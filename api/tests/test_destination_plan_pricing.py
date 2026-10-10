"""Destination tariffs constrain plan prices; all documents and accounts are synthetic."""
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from app.config_profiles import ConfigurationDocument
from app.config_values import ConfigurationValues
from app.routing import predict_facts
from app.routing.costs import RateCard, parse_amount
from app.routing.plan_allocation import Hold
from app.routing.pricing import price, quotes_for
from app.routing.store import RouteStore
from app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)


NOW = datetime(2026, 10, 9, 12)
DOMESTIC = '+12025550123'
CANADA = '+14165550100'
ABROAD = '+442079460123'
TOLL_FREE = '+18005550100'


def card(key, *, monthly=None, page='0', minute='0'):
    return RateCard(None, key, 'outbound', key, 'USD', parse_amount(minute), parse_amount(page), 0,
                    60, 0, None, NOW, parse_amount(monthly) if monthly else None)


@pytest.fixture
def routes(database):
    upgrade_schema(database)
    return RouteStore(database, sip_preset=lambda: 'telnyx')


def values(**changes):
    return SimpleNamespace(**{
        'fax_default_country': 'US', 'sip_trunk_preset': 'telnyx', 'sip_t38_enabled': True,
        'plan_budgets': '', 'time_zone': '', 'provider_accounts': {}, **changes,
    })


@pytest.mark.parametrize('key,rate,budget', [
    ('humblefax', card('humblefax', monthly='10'), ''),
    ('humblefax', card('humblefax', monthly='10'), 'humblefax:included_pages=100'),
    ('sip', card('sip-telnyx', minute='0.005'), 'sip:included_minutes=100'),
])
def test_a_budget_cannot_price_a_destination_with_no_applicable_tariff(routes, key, rate, budget):
    routes.replace_cards([rate])
    result = price(routes, values(plan_budgets=budget), key, ABROAD, 2, now=NOW)
    assert result.micros is None
    assert result.in_plan is False and result.uses_budget is False
    assert result.plan is None and result.text() == 'Price unknown'
    if key == 'humblefax':
        # HumbleFax's terms limit it to the US and Canada: abroad it is not an unknown price but no route at all.
        assert result.refused and result.sentence == ('HumbleFax does not send faxes to numbers in the United '
                                                      'Kingdom, so this fax cannot go this way.')
    else:
        assert 'unknown' in result.sentence and not result.refused


@pytest.mark.parametrize('monthly,budget', [
    ('10', ''),
    (None, 'phaxio:included_pages=100,page_overage=0.20'),
])
def test_a_separate_international_tariff_is_not_covered_by_the_domestic_plan(routes, monthly, budget):
    routes.replace_cards([card('phaxio', monthly=monthly, page='0' if monthly else '0.07')])
    settings = values(plan_budgets=budget)
    abroad = price(routes, settings, 'phaxio', ABROAD, 2, now=NOW)
    # The shipped international price is $0.10 per page, regardless of this local plan's room.
    assert abroad.micros == 200_000
    assert abroad.in_plan is False and abroad.uses_budget is False
    # Canada's explicit same-as-card terms do share this plan.
    canada = price(routes, settings, 'phaxio', CANADA, 2, now=NOW)
    assert canada.micros == 0 and canada.in_plan is True and canada.uses_budget is True


def test_a_free_destination_tariff_does_not_consume_a_minute_allowance(routes):
    routes.replace_cards([card('sip-telnyx', minute='0.005')])
    settings = values(plan_budgets='sip:included_minutes=100')
    free = price(routes, settings, 'sip', TOLL_FREE, 2, now=NOW)
    assert free.micros == 0 and free.in_plan is False and free.uses_budget is False
    domestic = price(routes, settings, 'sip', DOMESTIC, 2, now=NOW)
    assert domestic.micros == 0 and domestic.in_plan is True and domestic.uses_budget is True


def test_refused_destination_terms_cannot_be_replaced_by_a_plan_budget(routes, tmp_path, monkeypatch):
    routes.replace_cards([card('humblefax', monthly='10')])
    path = tmp_path / 'synthetic-destination-terms.json'
    path.write_text(json.dumps({'toll_free': [
        {'route': 'humblefax', 'reaches': 'no', 'pricing': 'same_as_card'},
    ]}))
    data = predict_facts.shipped(str(path))
    monkeypatch.setattr(predict_facts, 'shipped', lambda: data)
    result = price(routes, values(), 'humblefax', TOLL_FREE, 1, now=NOW)
    assert result.micros is None and result.in_plan is False and result.uses_budget is False
    assert 'cannot go this way' in result.sentence


def test_unknown_destination_stays_unknown_with_a_hold_and_in_quotes(routes):
    routes.replace_cards([card('humblefax', monthly='10')])
    hold = Hold('humblefax', (200, 50), ('pages', 'faxes'), 200, False, 2, 0, 'queue')
    result = price(routes, values(), 'humblefax', ABROAD, 1, now=NOW, hold=hold)
    assert result.micros is None and result.in_plan is False and result.over_budget is False
    assert result.held is None and result.unheld_micros is None
    account = SimpleNamespace(key='humblefax', provider='humblefax', sends=True)
    quote, = quotes_for(routes, values(), [account], ABROAD, 1, now=NOW)
    assert quote.micros is None and quote.plan is None


def test_each_account_keeps_its_own_plan_room(routes):
    routes.replace_cards([card('phaxio', monthly='10'), card('phaxio-second', monthly='10')])
    settings = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio', 'FAX_DEFAULT_COUNTRY': 'US',
        'FAX_PLAN_BUDGETS': 'phaxio:included_pages=1,page_overage=0.20; '
                           'phaxio-second:included_pages=5,page_overage=0.30',
    }).with_provider_accounts(ConfigurationDocument({
        'phaxio-second': {'provider': 'phaxio', 'label': 'Second account',
                         'sends': True, 'receives': False, 'enabled': True},
    }))
    first = price(routes, settings, 'phaxio', DOMESTIC, 2, now=NOW)
    second = price(routes, settings, 'phaxio-second', DOMESTIC, 2, provider='phaxio', now=NOW)
    assert first.micros == 200_000 and first.in_plan is False
    assert second.micros == 0 and second.in_plan is True
    abroad = price(routes, settings, 'phaxio-second', ABROAD, 2, provider='phaxio', now=NOW)
    assert abroad.micros == 200_000 and abroad.in_plan is False


def test_price_exposes_only_the_applicable_tariff_for_display(routes):
    routes.replace_cards([card('phaxio', monthly='10'), card('humblefax', monthly='10')])
    domestic = price(routes, values(), 'phaxio', DOMESTIC, 2, now=NOW)
    abroad = price(routes, values(), 'phaxio', ABROAD, 2, now=NOW)
    unknown = price(routes, values(), 'humblefax', ABROAD, 2, now=NOW)
    assert domestic.rate_card.monthly_fee_micros == 10_000_000
    assert abroad.rate_card.per_page_micros == 100_000
    assert abroad.rate_card.monthly_fee_micros is None
    assert unknown.rate_card is None


def test_registered_cloud_account_keeps_its_card_and_provider_destination_rules(routes):
    routes.replace_cards([card('phaxio', page='0.07'), card('phaxio-second', page='0.02')])
    settings = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio', 'FAX_DEFAULT_COUNTRY': 'US',
    }).with_provider_accounts(ConfigurationDocument({
        'phaxio-second': {'provider': 'phaxio', 'sends': True},
    }))
    domestic = price(routes, settings, 'phaxio-second', DOMESTIC, 2, provider='phaxio', now=NOW)
    canada = price(routes, settings, 'phaxio-second', CANADA, 2, provider='phaxio', now=NOW)
    abroad = price(routes, settings, 'phaxio-second', ABROAD, 2, provider='phaxio', now=NOW)
    assert domestic.micros == 40_000
    assert canada.micros == 40_000
    assert abroad.micros == 200_000


@pytest.mark.parametrize('own_card,want', [(True, 7000), (False, 19000)])
def test_registered_trunk_uses_its_own_card_then_its_own_carrier(routes, own_card, want):
    cards = [card('sip-telnyx', minute='0.005'), card('sip-gamma', minute='0.019')]
    if own_card:
        cards.append(card('sip-london', minute='0.007'))
    routes.replace_cards(cards)
    settings = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'FAX_DEFAULT_COUNTRY': 'US',
    }).with_provider_accounts(ConfigurationDocument({
        'sip-london': {'provider': 'sip', 'settings': {
            'preset': 'gamma', 'auth': 'ip', 'host': '192.0.2.40'}},
    }))
    result = price(routes, settings, 'sip-london', DOMESTIC, 1, provider='sip', now=NOW)
    assert result.micros == want


def test_inherited_provider_plan_keeps_currency_and_separate_account_usage(routes, tmp_path):
    from dataclasses import replace
    from datetime import timedelta
    from app.config_profiles import ProviderConfiguration
    from app.config_store import ConfigurationStore

    routes.replace_cards([replace(card('phaxio', monthly='10'), currency='GBP')])
    settings = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio', 'FAX_DEFAULT_COUNTRY': 'US',
        'FAX_PLAN_BUDGETS': 'phaxio:included_pages=2,page_overage=0.10; '
                           'phaxio-second:included_pages=2,page_overage=0.30',
    }).with_provider_accounts(ConfigurationDocument({
        'phaxio-second': {'provider': 'phaxio', 'sends': True},
    }))
    before = NOW - timedelta(minutes=1)
    configuration = ConfigurationStore(routes.engine, tmp_path / 'synthetic-installation.key')
    snapshot = configuration.initialize(settings, actor='test', providers={
        'outbound': ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-key'})})

    def sent(account, pages):
        job, attempt = f'{account}-job', f'{account}-attempt'
        configuration.accept_outbound(snapshot.active, {
            'id': job, 'to_number': DOMESTIC, 'file_name': 'synthetic.txt', 'tiff_path': '',
            'status': 'queued', 'pages': pages, 'created_at': before, 'updated_at': before})
        with routes.engine.begin() as connection:
            connection.execute(routes.attempts.insert().values(
                id=attempt, job_id=job, sequence=1, phase='success', created_at=before,
                submitted_at=before, completed_at=before))
            connection.execute(routes.costs.insert().values(
                id=attempt, job_id=job, destination=DOMESTIC, route=account, route_reason='configured',
                provider_id='phaxio', outcome='success', billed_pages=pages, billing_checks=0,
                created_at=before, updated_at=before))

    sent('phaxio', 100)
    own = price(routes, settings, 'phaxio-second', DOMESTIC, 2, provider='phaxio', now=NOW)
    assert (own.micros, own.currency, own.in_plan) == (0, 'GBP', True)
    sent('phaxio-second', 1)
    over = price(routes, settings, 'phaxio-second', DOMESTIC, 2, provider='phaxio', now=NOW)
    # Only this account's one earlier page counts: one new page is included, the other costs GBP 0.30.
    assert (over.micros, over.currency, over.in_plan) == (300_000, 'GBP', False)


def test_inherited_trunk_minute_allowance_uses_its_carrier_price_and_currency(routes):
    from dataclasses import replace

    routes.replace_cards([card('sip-telnyx', minute='0.005'),
                          replace(card('sip-gamma', minute='0.019'), currency='GBP')])
    settings = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'FAX_DEFAULT_COUNTRY': 'US',
        'FAX_PLAN_BUDGETS': 'sip-london:included_minutes=1',
    }).with_provider_accounts(ConfigurationDocument({
        'sip-london': {'provider': 'sip', 'settings': {
            'preset': 'gamma', 'auth': 'ip', 'host': '192.0.2.40'}},
    }))
    result = price(routes, settings, 'sip-london', DOMESTIC, 10, provider='sip', now=NOW)
    # The roughly 134-second fax uses three minutes: one included, two at GBP 0.019.
    assert (result.micros, result.currency, result.in_plan) == (38_000, 'GBP', False)
