"""Credential transport policy through the real HTTP application.

API-key clients (iOS, SDKs, Electron, MCP) authenticate with X-API-Key over any
transport and with any Origin. Browser cookie sessions need HTTPS, the loopback
development listener, or the explicit VPN-only insecure-session opt-in.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

BOOTSTRAP = 'synthetic-transport-bootstrap-key'


@pytest.fixture
def installation(isolated_installation, monkeypatch):
    monkeypatch.setenv('REQUIRE_API_KEY', 'true')
    monkeypatch.setenv('API_KEY', BOOTSTRAP)
    monkeypatch.setenv('PUBLIC_API_URL', 'https://fax.example')
    return monkeypatch


def _set_cookie(response):
    return [value for name, value in response.headers.multi_items() if name.lower() == 'set-cookie']


@pytest.mark.parametrize('origin', [None, 'null', 'https://unrelated.example'])
def test_api_key_works_over_plain_http_with_any_origin(installation, origin):
    headers = {'X-API-Key': BOOTSTRAP, **({'Origin': origin} if origin else {})}
    with TestClient(app, base_url='http://192.0.2.10:8080') as client:
        me = client.get('/auth/me', headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()['source'] == 'key' and 'csrf_token' not in me.json()
        # Unsafe key requests need no Origin or CSRF either (the iOS/SDK send contract).
        sent = client.post('/fax', data={'to': '+15551230001'},
            files={'file': ('note.txt', b'synthetic transport note', 'text/plain')}, headers=headers)
        assert sent.status_code == 202, sent.text
        assert set(sent.json()) >= {'id', 'status'}
        assert client.get('/fax/' + sent.json()['id'], headers=headers).status_code == 200
        assert client.get('/health', headers={'X-API-Key': ''}).status_code == 200


def test_plain_http_without_credentials_is_unauthenticated_not_transport_denied(installation):
    with TestClient(app, base_url='http://192.0.2.10:8080') as client:
        response = client.get('/auth/me')
        assert response.status_code == 401
        assert client.get('/auth/me', headers={'X-API-Key': 'wrong'}).status_code == 401


def test_plain_http_browser_session_requires_explicit_opt_in(installation):
    installation.setenv('FAXBOT_CONSOLE_ORIGINS', 'http://192.0.2.10:8080')
    with TestClient(app, base_url='http://192.0.2.10:8080') as client:
        login = client.post('/auth/key-login', json={'api_key': BOOTSTRAP},
            headers={'Origin': 'http://192.0.2.10:8080'})
        assert login.status_code == 403
        assert not _set_cookie(login)
        stray = client.get('/auth/me', headers={'Cookie': 'faxbot_session=forged'})
        assert stray.status_code == 403


def test_opted_in_plain_http_session_uses_non_secure_unprefixed_cookie(installation):
    installation.setenv('FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS', 'true')
    installation.setenv('FAXBOT_CONSOLE_ORIGINS', 'http://192.0.2.10:8080')
    origin = {'Origin': 'http://192.0.2.10:8080'}
    with TestClient(app, base_url='http://192.0.2.10:8080') as client:
        missing_origin = client.post('/auth/key-login', json={'api_key': BOOTSTRAP})
        assert missing_origin.status_code == 403, missing_origin.text
        assert client.post('/auth/key-login', json={'api_key': BOOTSTRAP},
            headers={'Origin': 'http://evil.example'}).status_code == 403
        login = client.post('/auth/key-login', json={'api_key': BOOTSTRAP}, headers=origin)
        assert login.status_code == 200, login.text
        (cookie,) = _set_cookie(login)
        assert cookie.startswith('faxbot_session=')
        assert 'secure' not in cookie.lower() and 'httponly' in cookie.lower()
        assert 'samesite=strict' in cookie.lower()
        me = client.get('/auth/me')
        assert me.status_code == 200 and me.json()['source'] == 'session'
        csrf = me.json()['csrf_token']
        # Cookie-authenticated unsafe requests keep exact Origin and CSRF.
        assert client.post('/auth/logout', headers={'X-CSRF-Token': csrf}).status_code == 403
        assert client.post('/auth/logout', headers=origin).status_code == 403
        assert client.post('/auth/logout', headers={**origin, 'X-CSRF-Token': csrf}).status_code == 200


def test_https_session_cookie_is_host_prefixed_and_secure(installation):
    installation.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://fax.example')
    with TestClient(app, base_url='https://fax.example') as client:
        login = client.post('/auth/key-login', json={'api_key': BOOTSTRAP},
            headers={'Origin': 'https://fax.example'})
        assert login.status_code == 200, login.text
        (cookie,) = _set_cookie(login)
        assert cookie.startswith('__Host-faxbot_session=') and 'secure' in cookie.lower()
        assert client.get('/auth/me').json()['source'] == 'session'
