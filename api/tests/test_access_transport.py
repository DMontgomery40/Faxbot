"""Pure credential transport parsing/policy; no HTTP user-flow acceptance."""
import importlib

import pytest

try:
    T = importlib.import_module('api.app.access.transport')
except ModuleNotFoundError:
    T = None

REMOTE = {'client': ('10.0.0.2', 123)}


def _scope(scheme, *headers, **extra):
    return {'scheme': scheme, **REMOTE, 'headers': [(b'host', b'fax.example'), *headers], **extra}


@pytest.mark.parametrize('scheme', ['http', 'https', 'ws', 'wss'])
@pytest.mark.parametrize('origin', [None, b'null', b'https://evil.example', b'http://10.0.0.9:8080'])
def test_api_key_header_is_accepted_over_any_transport_and_origin(scheme, origin):
    assert T is not None, 'Credential transport policy missing'
    policy = T.CredentialTransport({})
    headers = [(b'x-api-key', b'fbk_live_synthetic')] + ([(b'origin', origin)] if origin else [])
    result = policy.validate(_scope(scheme, *headers), public_url='https://fax.example')
    assert result.secure is (scheme in {'https', 'wss'})
    # An empty explicit header is still the sole credential; authentication rejects it.
    policy.validate(_scope(scheme, (b'x-api-key', b'')), public_url='https://fax.example')


def test_api_key_transport_still_rejects_duplicate_headers():
    policy = T.CredentialTransport({})
    for headers in ([(b'x-api-key', b'one'), (b'x-api-key', b'one')],
                    [(b'x-api-key', b'one'), (b'origin', b'null'), (b'origin', b'null')]):
        with pytest.raises(T.TransportError):
            policy.validate(_scope('http', *headers), public_url='https://fax.example')


def test_cookie_sessions_require_https_loopback_or_explicit_insecure_opt_in():
    cookie = (b'cookie', b'faxbot_session=opaque')
    with pytest.raises(T.TransportError):
        T.CredentialTransport({}).validate(_scope('http', cookie), public_url='https://fax.example')
    for name in (b'__Host-faxbot_session', b'faxbot_dev_session'):
        with pytest.raises(T.TransportError):
            T.CredentialTransport({}).validate(_scope('http', (b'cookie', name + b'=opaque')),
                public_url='https://fax.example')
    result = T.CredentialTransport({}).validate(_scope('https', (b'cookie', b'__Host-faxbot_session=opaque')),
        public_url='https://fax.example')
    assert (result.secure, result.cookie_name) == (True, '__Host-faxbot_session')
    opted_in = T.CredentialTransport({'FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS': 'true',
        'FAXBOT_CONSOLE_ORIGINS': 'http://10.8.0.1:8080'})
    result = opted_in.validate(_scope('http', cookie), public_url='https://fax.example')
    # Never a __Host- or Secure cookie over plain HTTP.
    assert (result.secure, result.cookie_name) == (False, 'faxbot_session')
    assert opted_in.validate(_scope('https', cookie), public_url='https://fax.example').cookie_name == '__Host-faxbot_session'
    for value in ('false', 'TRUE', '1', ''):
        with pytest.raises(T.TransportError):
            T.CredentialTransport({'FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS': value}).validate(
                _scope('http', cookie), public_url='https://fax.example')


def test_insecure_session_opt_in_keeps_exact_origin_for_browser_operations():
    policy = T.CredentialTransport({'FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS': 'true',
        'FAXBOT_CONSOLE_ORIGINS': 'http://10.8.0.1:8080'})
    good = (b'origin', b'http://10.8.0.1:8080')
    login = policy.validate(_scope('http', good), public_url='https://fax.example', require_origin=True)
    assert (login.secure, login.cookie_name) == (False, 'faxbot_session')
    for origin in (None, b'null', b'http://10.8.0.1:8081', b'https://10.8.0.1:8080', b'http://evil.example'):
        headers = [(b'cookie', b'faxbot_session=opaque')] + ([(b'origin', origin)] if origin else [])
        with pytest.raises(T.TransportError):
            policy.validate(_scope('http', *headers), public_url='https://fax.example', require_origin=True)


def test_browser_login_applies_session_rules_even_with_a_stray_key_header():
    policy = T.CredentialTransport({'FAXBOT_CONSOLE_ORIGINS': 'https://fax.example'})
    stray = (b'x-api-key', b'fbk_live_synthetic')
    with pytest.raises(T.TransportError):
        policy.validate(_scope('http', stray, (b'origin', b'https://fax.example')),
            public_url='https://fax.example', require_origin=True)
    with pytest.raises(T.TransportError):
        policy.validate(_scope('https', stray, (b'origin', b'null')),
            public_url='https://fax.example', require_origin=True)
    assert policy.validate(_scope('https', stray, (b'origin', b'https://fax.example')),
        public_url='https://fax.example', require_origin=True).secure


def test_missing_credential_is_left_to_authentication_on_any_transport():
    # A plain-HTTP request with neither key nor session cookie gets 401, not 403.
    policy = T.CredentialTransport({})
    result = policy.validate(_scope('http'), public_url='https://fax.example')
    assert result.secure is False
    assert T.credential_source(_scope('http'), result.cookie_name) == (None, None)


@pytest.mark.parametrize('origin', ['https://evil.example', 'null', 'https://fax.example.evil',
    'https://fax.example/path', 'https://user@fax.example', 'https://fax.example?x=1',
    'https://fax.example#fragment', ' https://fax.example', 'https://fax.example\x00',
    'https://fax.example:443', 'HTTPS://fax.example', 'https://FAX.example'])
def test_browser_origin_must_be_exact_serialized_trusted_origin(origin):
    policy = T.CredentialTransport({})
    with pytest.raises(T.TransportError):
        policy.validate({'scheme': 'https', 'headers': [(b'origin', origin.encode())]},
            public_url='https://fax.example/callback')


def test_private_console_override_and_required_origin():
    policy = T.CredentialTransport({'FAXBOT_CONSOLE_ORIGINS': 'https://Console.Example:443,https://second.example:8443'})
    scope = {'scheme': 'https', 'headers': [(b'origin', b'https://console.example')]}
    assert policy.validate(scope, public_url='https://provider.example', require_origin=True).secure
    with pytest.raises(T.TransportError):
        policy.validate({'scheme': 'https', 'headers': []}, public_url='https://provider.example', require_origin=True)
    with pytest.raises(T.TransportError):
        policy.validate({'scheme': 'https', 'headers': [(b'origin', b'https://provider.example')]},
            public_url='https://provider.example')


@pytest.mark.parametrize('configured', ['', '*', 'null', 'https://fax.example/path',
    'https://fax.example/', 'https://user@fax.example', 'https://fax.example,',
    'https://fax.example?x=1', 'https://fax.example#', 'https://fax.example:99999',
    'https://fax.example\\evil', 'https://fax.example\n'])
def test_invalid_origin_override_rejected_at_construction(configured):
    with pytest.raises(T.TransportError):
        T.CredentialTransport({'FAXBOT_CONSOLE_ORIGINS': configured})


def test_duplicate_origins_are_rejected_even_if_identical():
    with pytest.raises(T.TransportError):
        T.CredentialTransport({}).validate({'scheme':'https', 'headers':[
            (b'origin', b'https://fax.example'), (b'origin', b'https://fax.example')]},
            public_url='https://fax.example')


def test_loopback_exception_requires_launcher_provenance_not_forwarded_peer():
    policy = T.CredentialTransport({'FAXBOT_ALLOW_INSECURE_LOOPBACK':'true',
        'FAXBOT_CONSOLE_ORIGINS':'http://127.0.0.1:8080'})
    scope = {'scheme':'http', 'client':('127.0.0.1', 234), 'headers':[
        (b'host', b'127.0.0.1:8080'), (b'origin', b'http://127.0.0.1:8080'),
        (b'cookie', b'faxbot_dev_session=opaque')]}
    with pytest.raises(T.TransportError):
        policy.validate(scope, public_url='https://provider.example')
    scope['faxbot.direct_loopback'] = True
    with pytest.raises(T.TransportError):
        policy.validate(scope, public_url='https://provider.example')
    scope['faxbot.direct_loopback'] = T._DIRECT_LOOPBACK
    result = policy.validate(scope, public_url='https://provider.example')
    assert result.cookie_name == 'faxbot_dev_session' and result.secure is False
    with pytest.raises(T.TransportError):
        T.CredentialTransport({}).validate(scope, public_url='https://provider.example')
    for peer in ['10.0.0.1', 'invalid']:
        with pytest.raises(T.TransportError):
            policy.validate(dict(scope, client=(peer, 123)), public_url='https://provider.example')
    with pytest.raises(T.TransportError):
        policy.validate(dict(scope, headers=[(b'host', b'evil.example'), (b'cookie', b'faxbot_dev_session=x')]), public_url='https://provider.example')


def test_explicit_header_wins_without_cookie_fallback_and_duplicates_deny():
    headers = [(b'x-api-key', b''), (b'cookie', b'__Host-faxbot_session=good')]
    assert T.credential_source({'headers':headers}, '__Host-faxbot_session') == ('key', '')
    assert T.credential_source({'headers':headers[1:]}, '__Host-faxbot_session') == ('session', 'good')
    assert T.credential_source({'headers':headers[1:]}, 'faxbot_dev_session') == (None, None)
    for invalid in [headers + [(b'x-api-key', b'another')],
        [(b'cookie', b'__Host-faxbot_session=one; __Host-faxbot_session=two')],
        [(b'cookie', b'__Host-faxbot_session=one'), (b'cookie', b'__Host-faxbot_session=two')]]:
        with pytest.raises(T.TransportError):
            T.credential_source({'headers':invalid}, '__Host-faxbot_session')
