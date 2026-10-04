"""The iPhone app's contract: what an app built against the previous release reads.

The app pairs with a six-digit code (POST /mobile/pair, no key), then sends its
own key as X-API-Key: POST /fax (multipart `to` and `file`), GET /fax/{id},
GET /inbound, GET /inbound/{id}/pdf and GET /health. These tests pin the field
names and JSON types the app and both SDK models read (docs/api.md,
docs/apps/ios.md, sdks/*), and the device key's scope. Fields may be added;
none of these may be renamed, removed or change type.
"""
from datetime import datetime
import time

import pytest
from fastapi.testclient import TestClient

BOOTSTRAP = 'synthetic-mobile-bootstrap'
ORIGIN = 'https://testserver'
ADMIN = {'X-API-Key': BOOTSTRAP}
DEVICE_PERMISSIONS = {'fax:send', 'fax:read', 'fax:document', 'inbound:list', 'inbound:read', 'inbound:document'}


@pytest.fixture
def server(monkeypatch, tmp_path):
    import app.main as main_module
    from app.config_values import ConfigurationValues
    for name in ConfigurationValues.environment_keys():
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        'DATABASE_URL': f"sqlite:///{tmp_path / 'mobile.db'}", 'FAX_DATA_DIR': str(tmp_path / 'faxdata'),
        'FAXBOT_INSTALLATION_KEY_PATH': str(tmp_path / 'installation.key'),
        'FAXBOT_CONFIG_PATH': str(tmp_path / 'absent-legacy.json'), 'FAXBOT_PROVIDERS_DIR': str(tmp_path / 'providers'),
        # The app talks to an installation with a provider; sending stays held in tests.
        'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'true', 'INBOUND_ENABLED': 'true',
        'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': ORIGIN, 'MAX_REQUESTS_PER_MINUTE': '0',
        'ENABLE_PERSISTED_SETTINGS': 'false', 'ENABLE_MCP_SSE': 'false', 'ENABLE_MCP_HTTP': 'false', 'TZ': 'UTC',
    }.items():
        monkeypatch.setenv(name, value)
    time.tzset()
    monkeypatch.setattr(main_module, '_PAIR_ATTEMPTS', {})
    with TestClient(main_module.app, base_url=ORIGIN) as client:
        yield client
    main_module.app.state.direct_http = None


def _timestamp(value):
    assert isinstance(value, str)
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _pair(client, device_name='Synthetic iPhone'):
    code = client.post('/admin/tunnel/pair', headers=ADMIN)
    assert code.status_code == 200, code.text
    issued = code.json()
    assert set(issued) == {'code', 'expires_at'} and len(issued['code']) == 6 and issued['code'].isdigit()
    paired = client.post('/mobile/pair', json={'code': issued['code'], 'device_name': device_name})
    return issued['code'], paired


def test_pairing_returns_the_base_urls_and_one_device_key(server):
    code, paired = _pair(server)
    assert paired.status_code == 200, paired.text
    body = paired.json()
    assert set(body) == {'base_urls', 'token'}
    assert set(body['base_urls']) == {'local', 'tunnel', 'public'}
    assert all(value is None or isinstance(value, str) for value in body['base_urls'].values())
    assert body['base_urls']['public'] == ORIGIN
    assert isinstance(body['token'], str) and body['token'].startswith('fbk_live_')
    # A code works once; a refusal is 403 with a plain "detail" the app shows as is.
    again = server.post('/mobile/pair', json={'code': code, 'device_name': 'Second phone'})
    assert again.status_code == 403 and isinstance(again.json()['detail'], str)
    assert server.post('/mobile/pair', json={'code': 'abc'}).status_code == 403


def test_the_device_key_can_do_what_the_app_needs_and_nothing_more(server):
    _, paired = _pair(server)
    device = {'X-API-Key': paired.json()['token']}
    me = server.get('/auth/me', headers=device)
    assert me.status_code == 200
    assert me.json()['principal']['display_name'] == 'Device: Synthetic iPhone'
    assert server.get('/admin/api-keys', headers=device).status_code == 403
    assert server.get('/admin/settings', headers=device).status_code == 403
    # The key's own permission list is exactly what docs/apps/ios.md describes.
    token = paired.json()['token']
    keys = server.get('/access/keys', headers=ADMIN, params={'limit': 200}).json()['items']
    key, = [item for item in keys if token.startswith('fbk_live_' + item['id'] + '_')]
    assert {grant['permission'] for grant in key['ceiling']} == DEVICE_PERMISSIONS
    assert key['principal']['display_name'] == 'Device: Synthetic iPhone'


def test_health_needs_no_key(server):
    health = server.get('/health')
    assert health.status_code == 200 and health.json() == {'status': 'ok'}


def test_send_and_read_a_fax_with_the_fields_the_app_reads(server):
    _, paired = _pair(server)
    device = {'X-API-Key': paired.json()['token']}
    sent = server.post('/fax', headers=device, data={'to': '+15551230001'},
                       files={'file': ('note.txt', b'Synthetic fax from the app\n', 'text/plain')})
    assert sent.status_code == 202, sent.text
    for body in (sent.json(), server.get(f"/fax/{sent.json()['id']}", headers=device).json()):
        assert isinstance(body['id'], str) and body['id']
        assert isinstance(body['status'], str) and body['status']
        assert body['to'] == '+15551230001'
        assert body['pages'] is None or isinstance(body['pages'], int)
        assert isinstance(body['backend'], str) and body['backend'] == 'phaxio'
        assert body['error'] is None or isinstance(body['error'], str)
        _timestamp(body['created_at'])
        _timestamp(body['updated_at'])
    missing = server.get('/fax/' + 'f' * 32, headers=device)
    assert missing.status_code == 404 and isinstance(missing.json()['detail'], str)
    assert server.post('/fax', data={'to': '+15551230001'},
                       files={'file': ('note.txt', b'x', 'text/plain')}).status_code == 401


def test_received_faxes_list_and_open_with_the_fields_the_app_reads(server):
    simulated = server.post('/admin/inbound/simulate', headers=ADMIN,
                            json={'fr': '+15559870001', 'to': '+15550001111', 'pages': 2})
    assert simulated.status_code == 200, simulated.text
    _, paired = _pair(server)
    device = {'X-API-Key': paired.json()['token']}
    listed = server.get('/inbound', headers=device)
    assert listed.status_code == 200 and isinstance(listed.json(), list)
    item, = listed.json()
    assert isinstance(item['id'], str) and item['id']
    assert (item['fr'], item['to']) == ('+15559870001', '+15550001111')
    assert isinstance(item['status'], str) and item['status']
    assert item['pages'] is None or isinstance(item['pages'], int)
    _timestamp(item['received_at'])
    detail = server.get(f"/inbound/{item['id']}", headers=device)
    assert detail.status_code == 200 and detail.json()['id'] == item['id']
    document = server.get(f"/inbound/{item['id']}/pdf", headers=device)
    assert document.status_code == 200 and document.content.startswith(b'%PDF')
