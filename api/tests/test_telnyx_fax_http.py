"""Telnyx Fax API setup over HTTP: creating the Fax Application, and the results address. A fake Telnyx only."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient


BOOTSTRAP = 'synthetic-telnyx-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
KEY = 'KEYsynthetic-telnyx-key'
APP = '1293384261075731499'


@pytest.fixture
def http(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'sinch', 'SINCH_PROJECT_ID': 'synthetic-project',
                        'SINCH_API_KEY': 'synthetic-key', 'SINCH_API_SECRET': 'synthetic-secret',
                        'TELNYX_API_KEY': KEY, 'TELNYX_FAX_FROM_NUMBER': '+13035550100',
                        'FAXBOT_CONSOLE_ORIGINS': 'https://testserver', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    from app import main
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


@pytest.fixture
def telnyx(monkeypatch):
    """Telnyx's POST /v2/fax_applications; ``answer`` can be replaced per test."""
    seen = []
    fake = {'answer': lambda request: httpx.Response(201, json={'data': {'id': APP, 'record_type': 'fax_application'}})}
    original = httpx.AsyncClient

    def handle(request):
        seen.append(request)
        assert request.url.host == 'api.telnyx.com' and request.url.path == '/v2/fax_applications'
        assert request.headers['Authorization'] == 'Bearer ' + KEY
        return fake['answer'](request)

    def client(**kwargs):
        kwargs['transport'] = httpx.MockTransport(handle)
        return original(**kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    return seen, fake


def generation(http):
    return http.get('/admin/providers/accounts', headers=ADMIN).json()['generation']


def test_faxbot_creates_the_fax_application_once_and_saves_its_id(http, telnyx):
    seen, _ = telnyx
    created = http.post('/admin/providers/accounts/telnyx_fax/telnyx-fax-application', headers=ADMIN,
                        json={'expected_generation': generation(http)})
    assert created.status_code == 200, created.text
    result = created.json()
    assert result['created']['connection_id'] == APP
    assert 'outbound voice profile' in result['created']['sentence']
    assert json.loads(seen[0].content) == {'application_name': 'Faxbot (Telnyx Fax API)',
                                           'webhook_event_url': 'https://testserver/telnyx-fax-callback',
                                           'active': True}
    account = next(account for account in result['accounts'] if account['key'] == 'telnyx_fax')
    assert account['settings']['connection_id'] == APP and account['health']['state'] == 'ready'
    assert KEY not in json.dumps(result)
    view = http.get('/admin/settings', headers=ADMIN).json()['telnyx_fax']
    assert (view['connection_id'], view['results_url']) == (APP, 'https://testserver/telnyx-fax-callback')
    # A second press never makes a second application.
    again = http.post('/admin/providers/accounts/telnyx_fax/telnyx-fax-application', headers=ADMIN,
                      json={'expected_generation': result['generation']})
    assert again.status_code == 400 and 'already has a Fax Application' in again.json()['detail']
    assert len(seen) == 1


def test_an_extra_telnyx_account_gets_its_own_application(http, telnyx):
    added = http.post('/admin/providers/accounts', headers=ADMIN, json={
        'key': 'telnyx-2', 'provider': 'telnyx_fax', 'label': 'Telnyx (second)', 'sends': True, 'receives': False,
        'settings': {'from_number': '+13035550101'}, 'credentials': {'api_key': KEY},
        'expected_generation': generation(http)})
    assert added.status_code == 200, added.text
    extra = next(account for account in added.json()['accounts'] if account['key'] == 'telnyx-2')
    assert extra['health']['state'] == 'not_set_up' and 'Fax Application ID' in extra['health']['sentence']
    created = http.post('/admin/providers/accounts/telnyx-2/telnyx-fax-application', headers=ADMIN,
                        json={'expected_generation': added.json()['generation'], 'name': 'Faxbot second'})
    assert created.status_code == 200, created.text
    extra = next(account for account in created.json()['accounts'] if account['key'] == 'telnyx-2')
    assert extra['settings']['connection_id'] == APP and extra['health']['state'] == 'ready'


def test_a_refused_key_or_another_provider_is_one_plain_sentence(http, telnyx):
    seen, fake = telnyx
    fake['answer'] = lambda request: httpx.Response(401, json={'errors': [{'code': '10009', 'detail': KEY}]})
    refused = http.post('/admin/providers/accounts/telnyx_fax/telnyx-fax-application', headers=ADMIN,
                        json={'expected_generation': generation(http)})
    assert refused.status_code == 502
    assert refused.json()['detail'] == ('Telnyx did not accept the API key. Check it on the Telnyx Fax API '
                                        'provider page.')
    other = http.post('/admin/providers/accounts/sinch/telnyx-fax-application', headers=ADMIN,
                      json={'expected_generation': generation(http)})
    assert other.status_code == 400
    assert http.post('/admin/providers/accounts/telnyx_fax/telnyx-fax-application',
                     json={'expected_generation': generation(http)}).status_code in (401, 403)
    assert len(seen) == 1


def test_the_results_address_refuses_unsigned_notices_and_ignores_the_applications_own(http):
    # The Fax Application's own address names no attempt: nothing changes and Faxbot keeps checking.
    plain = http.post('/telnyx-fax-callback', content=b'{"data":{"event_type":"fax.delivered"}}')
    assert plain.status_code == 200 and plain.json() == {'ok': True, 'applied': False}
    forged = http.post(f"/telnyx-fax-callback?job_id={'a' * 32}&attempt_id={'b' * 32}",
                       content=b'{"data":{"payload":{"fax_id":"x"}}}',
                       headers={'telnyx-timestamp': '1', 'telnyx-signature-ed25519': 'AAAA'})
    assert forged.status_code == 401
