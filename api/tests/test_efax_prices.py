"""eFax's published plans: recorded with source and date, never a starting card, offered as an estimate."""
from datetime import date, datetime
import json
from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest

from app import main
from app.routing.costs import RateCard, parse_amount
from app.routing.reference import load_reference_plans, suggestion
from app.routing.seed import load_cards


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = 'synthetic-efax-prices-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
MONEY = re.compile(r'[0-9]+(?:\.[0-9]{1,6})?')


def test_every_published_efax_plan_is_recorded_with_its_source_and_read_date():
    document = json.loads((ROOT / 'config' / 'rate_cards.json').read_text())
    plans = [plan for plan in document['reference_plans'] if plan['provider_id'] == 'efax']
    assert {plan['country'] for plan in plans} == {'US', 'AU', 'GB'}
    for plan in plans:
        assert plan['source_url'].startswith('https://') and plan['source_url'] in plan['sources']
        assert date.fromisoformat(plan['advertised_on']) == date(2026, 10, 4)
        assert plan['currency'] in {'USD', 'AUD', 'GBP'} and plan['notes'].endswith('.')
        if plan['monthly_fee'] is None:
            assert 'No published price; add your rate.' in plan['notes']
        else:
            assert MONEY.fullmatch(plan['monthly_fee']) and MONEY.fullmatch(plan['overage_per_page'])
            assert type(plan['included_pages']) is int and plan['included_pages'] > 0
    priced = {(plan['country'], plan['label']): (plan['monthly_fee'], plan['included_pages'], plan['overage_per_page'])
              for plan in plans if plan['monthly_fee']}
    assert priced == {('US', 'eFax Personal plan'): ('18.99', 200, '0.10'),
                      ('US', 'eFax Business plan'): ('39.99', 500, '0.07'),
                      ('AU', 'eFax Plus plan (Australia)'): ('16.95', 150, '0.10'),
                      ('AU', 'eFax Pro plan (Australia)'): ('18.95', 200, '0.10'),
                      ('AU', 'eFax Pro2 plan (Australia)'): ('23.95', 300, '0.10')}
    # eFax prices the API Faxbot uses by quote, so none of these becomes a starting card.
    assert all(card.provider_id != 'efax' for card in load_cards())
    for key in ('cards', 'providers', 'plans'):
        assert all(entry['provider_id'] != 'efax' for entry in document[key])


@pytest.mark.parametrize(('country', 'sentence', 'fee', 'currency'), [
    ('US', 'eFax prices its API by quote. Its published plans start at USD 18.99 a month in the US '
           '(200 pages to the US and Canada, then 10¢ a page).', '18.99', 'USD'),
    ('AU', 'eFax prices its API by quote. Its published plans start at AUD 16.95 a month in Australia, before GST '
           '(150 pages sent and 150 received, then 10¢ a page).', '16.95', 'AUD'),
    ('CA', 'eFax prices its API by quote. Its published plans start at USD 18.99 a month in the US '
           '(200 pages to the US and Canada, then 10¢ a page).', '18.99', 'USD'),
])
def test_the_cheapest_published_plan_is_offered_as_a_valid_card(country, sentence, fee, currency):
    found = suggestion('efax', country)
    assert found['sentence'] == sentence
    card = found['card']
    assert (card['provider_id'], card['direction'], card['monthly_fee'], card['currency']) == ('efax', 'outbound', fee,
                                                                                              currency)
    assert card['label'].endswith('(my estimate)') and len(card['label']) <= 100
    built = RateCard(None, card['provider_id'], card['direction'], card['label'], card['currency'],
                     parse_amount(card['per_minute']), parse_amount(card['per_page']), parse_amount(card['per_call']),
                     card['billing_increment_seconds'], card['minimum_seconds'], card['source_url'],
                     datetime.fromisoformat(card['captured_on']), parse_amount(card['monthly_fee'], whole_digits=4))
    assert built.flat_plan


def test_a_uk_installation_hears_that_the_uk_prices_were_not_readable():
    found = suggestion('efax', 'GB')
    assert found['card'] is None
    assert found['sentence'] == ("eFax prices its API by quote. Faxbot could not read eFax's prices for the UK; "
                                 "see eFax's UK page.")
    assert found['page_url'] == 'https://ww2.efax.com/uk/' and found['page_label'] == "eFax's UK page"


def test_unknown_providers_and_unreadable_files_have_no_reference_plans(tmp_path):
    assert suggestion('documo', 'US') is None
    assert load_reference_plans(tmp_path / 'missing.json') == []
    (tmp_path / 'broken.json').write_text('{')
    assert load_reference_plans(tmp_path / 'broken.json') == []


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'efax', 'MAX_REQUESTS_PER_MINUTE': '0',
                        'FAXBOT_CONSOLE_ORIGINS': 'https://testserver'}.items():
        monkeypatch.setenv(name, value)
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def test_efax_in_use_reads_no_published_price_and_its_plans_come_from_the_api(client):
    assert all(card['provider_id'] != 'efax'
               for card in client.get('/routing/rate-cards', headers=ADMIN).json()['cards'])
    found = client.get('/routing/published-plans', params={'provider_id': 'efax'}, headers=ADMIN)
    assert found.status_code == 200, found.text
    assert found.json()['country'] == 'US' and found.json()['card']['monthly_fee'] == '18.99'
    assert len(found.json()['plans']) == 8
    assert client.get('/routing/published-plans', params={'provider_id': 'documo'}, headers=ADMIN).status_code == 404
    assert client.get('/routing/published-plans', params={'provider_id': 'efax'}).status_code == 401
