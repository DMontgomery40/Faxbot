"""Owner transport policy over the real HTTP app: header keys anywhere, cookies only when safe."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config_values import ConfigurationValues

BOOTSTRAP = 'synthetic-transport-bootstrap-key'


@pytest.fixture
def installation(monkeypatch, tmp_path):
    for name in ConfigurationValues.environment_keys():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv('FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS', raising=False)
    monkeypatch.delenv('FAXBOT_ALLOW_INSECURE_LOOPBACK', raising=False)
    for name, value in {
        'DATABASE_URL': f"sqlite:///{tmp_path / 'transport.db'}",
        'FAX_DATA_DIR': str(tmp_path / 'data'),
        'FAXBOT_INSTALLATION_KEY_PATH': str(tmp_path / '.configuration.key'),
        'FAX_DISABLED': 'true',
        'REQUIRE_API_KEY': 'true',
        'API_KEY': BOOTSTRAP,
        'PUBLIC_API_URL': 'https://fax.example',
        'FAXBOT_CONSOLE_ORIGINS': 'http://testserver',
        'ENABLE_PERSISTED_SETTINGS': 'false',
        'ENABLE_MCP_SSE': 'false',
        'ENABLE_MCP_HTTP': 'false',
    }.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


@pytest.mark.parametrize('origin', [None, 'null', 'https://unlisted.example'])
def test_header_key_authenticates_over_plain_http_without_origin_rules(installation, origin):
    headers = {'X-API-Key': BOOTSTRAP}
    if origin is not None:
        headers['Origin'] = origin
    with TestClient(app, base_url='http://testserver') as client:
        me = client.get('/auth/me', headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()['source'] == 'key' and 'csrf_token' not in me.json()
        assert client.get('/auth/context', headers=headers).status_code == 200
        # Health stays public and tolerates an empty key header (iOS reachability probe).
        health = client.get('/health', headers={'X-API-Key': ''})
        assert health.status_code == 200 and health.json() == {'status': 'ok'}
        # A wrong key is still 401, never a transport failure.
        assert client.get('/auth/me', headers={'X-API-Key': 'wrong', 'Origin': 'null'}).status_code == 401


def test_cookie_login_over_plain_http_is_refused_by_default(installation):
    with TestClient(app, base_url='http://testserver') as client:
        response = client.post('/auth/key-login', json={'api_key': BOOTSTRAP},
            headers={'Origin': 'http://testserver'})
        assert response.status_code == 403
        assert 'set-cookie' not in response.headers


def test_explicit_insecure_http_sessions_issue_plain_cookie_and_keep_csrf(installation):
    installation.setenv('FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS', 'true')
    with TestClient(app, base_url='http://testserver') as client:
        login = client.post('/auth/key-login', json={'api_key': BOOTSTRAP},
            headers={'Origin': 'http://testserver'})
        assert login.status_code == 200, login.text
        cookie = login.headers['set-cookie']
        assert cookie.startswith('faxbot_session=')
        assert 'secure' not in cookie.lower() and 'httponly' in cookie.lower()
        assert 'samesite=strict' in cookie.lower()
        me = client.get('/auth/me')
        assert me.status_code == 200 and me.json()['source'] == 'session'
        csrf = me.json()['csrf_token']
        # Cross-origin login is still refused.
        assert client.post('/auth/key-login', json={'api_key': BOOTSTRAP},
            headers={'Origin': 'http://evil.example'}).status_code == 403
        # Unsafe cookie requests still need Origin and CSRF.
        assert client.post('/auth/logout', headers={'Origin': 'http://testserver'}).status_code == 403
        assert client.post('/auth/logout', headers={'X-CSRF-Token': csrf}).status_code == 403
        out = client.post('/auth/logout', headers={'Origin': 'http://testserver', 'X-CSRF-Token': csrf})
        assert out.status_code == 200, out.text
