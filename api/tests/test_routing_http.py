"""Delivery route administration over the real HTTPS stack and access policy."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app.routing.store import RouteStore


BOOTSTRAP = 'synthetic-routing-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
TELNYX = {'provider_id': 'sip', 'label': 'Telnyx SIP trunk', 'per_minute': '0.005',
          'billing_increment_seconds': 60, 'source_url': 'https://telnyx.com/pricing/elastic-sip',
          'captured_on': '2026-10-03'}
PHAXIO = {'provider_id': 'phaxio', 'label': 'Phaxio', 'per_page': '0.07', 'captured_on': '2026-10-03'}


@pytest.fixture
def client(isolated_installation, monkeypatch, tmp_path):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_ROUTES': 'sip, signalwire',
                        'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def scoped_key(client, scopes):
    response = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': scopes})
    assert response.status_code == 200, response.text
    return {'X-API-Key': response.json()['token']}


def test_rate_cards_round_trip_as_money(client):
    response = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [TELNYX, PHAXIO]})
    assert response.status_code == 200, response.text
    cards = {card['provider_id']: card for card in response.json()['cards']}
    assert cards['sip']['per_minute'] == '0.005' and cards['sip']['per_page'] == '0.00'
    assert cards['phaxio']['per_page'] == '0.07' and cards['sip']['captured_on'] == '2026-10-03'
    assert client.get('/routing/rate-cards', headers=ADMIN).json()['cards'] == response.json()['cards']
    bad = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [{**PHAXIO, 'per_page': '-1'}]})
    assert bad.status_code == 400 and 'six decimal places' in bad.json()['detail']


def test_destination_recommendation_ranks_configured_routes_by_cost(client):
    client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [TELNYX, PHAXIO]})
    response = client.patch('/routing/destinations/+1 (202) 555-0123', headers=ADMIN,
                            json={'display_name': 'County clinic', 'preferred_route': None})
    assert response.status_code == 200, response.text
    assert response.json()['number'] == '+12025550123' and response.json()['version'] == 1
    view = client.get('/routing/destinations/+12025550123', headers=ADMIN).json()
    assert view['display_name'] == 'County clinic'
    assert [(route['route'], route['reason']) for route in view['recommended_routes']] == [
        ('sip', 'cheapest'), ('phaxio', 'alternative'), ('signalwire', 'alternative')]
    assert view['recommended_routes'][0]['estimated_cost_one_page'] == {'currency': 'USD', 'amount': '0.005'}
    assert view['recommended_routes'][0]['explanation'].endswith('.')
    assert [route['route'] for route in view['available_routes']] == ['phaxio', 'sip', 'signalwire']
    listing = client.get('/routing/destinations', headers=ADMIN).json()
    assert [item['number'] for item in listing['destinations']] == ['+12025550123']
    stale = client.patch('/routing/destinations/+12025550123', headers=ADMIN, json={'notes': 'x', 'version': 0})
    assert stale.status_code == 409
    assert client.patch('/routing/destinations/555', headers=ADMIN, json={}).status_code == 400


def test_costs_report_totals_by_provider(client):
    engine = main.app.state.configuration_runtime.manager.store.engine
    store = RouteStore(engine)
    assert store.cost_totals(datetime(2026, 1, 1)) == []
    response = client.get('/routing/costs', headers=ADMIN, params={'since': '2026-01-01T00:00:00Z'})
    assert response.status_code == 200 and response.json()['providers'] == []


def test_routing_requires_settings_permissions(client):
    sender = scoped_key(client, ['fax:send'])
    for method, path, body in [('GET', '/routing/destinations', None), ('GET', '/routing/costs', None),
                               ('GET', '/routing/rate-cards', None),
                               ('PUT', '/routing/rate-cards', {'cards': []}),
                               ('PATCH', '/routing/destinations/+12025550123', {'notes': 'x'})]:
        response = client.request(method, path, json=body, headers=sender)
        assert response.status_code == 403, (method, path, response.status_code)
    assert client.get('/routing/destinations').status_code == 401


def test_route_fallback_policy_lives_with_the_application(isolated_installation, monkeypatch):
    import time
    from app.outbound_store import OutboundStore
    from app.routing.fallback import FallbackPolicy
    monkeypatch.setenv('API_KEY', BOOTSTRAP)
    monkeypatch.setenv('REQUIRE_API_KEY', 'true')
    assert OutboundStore.fallback_policy is None
    with TestClient(main.app, base_url='https://testserver'):
        deadline = time.monotonic() + 5
        while OutboundStore.fallback_policy is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert isinstance(OutboundStore.fallback_policy, FallbackPolicy)
    assert OutboundStore.fallback_policy is None
