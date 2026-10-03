"""Pure credential transport parsing/policy; no HTTP user-flow acceptance."""
import importlib

import pytest

try:
    T = importlib.import_module('api.app.access.transport')
except ModuleNotFoundError:
    T = None


def test_remote_credentials_require_https_even_without_browser_origin():
    assert T is not None, 'Credential transport policy missing'
    policy = T.CredentialTransport({})
    with pytest.raises(T.TransportError):
        policy.validate({'scheme': 'http', 'client': ('10.0.0.2', 123),
            'headers': [(b'host', b'fax.example')]}, public_url='https://fax.example')
    result = policy.validate({'scheme': 'https', 'client': ('10.0.0.2', 123),
        'headers': [(b'host', b'fax.example')]}, public_url='https://fax.example')
    assert result.cookie_name == '__Host-faxbot_session'
    assert result.secure is True


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
        (b'host', b'127.0.0.1:8080'), (b'origin', b'http://127.0.0.1:8080')]}
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
        policy.validate(dict(scope, headers=[(b'host', b'evil.example')]), public_url='https://provider.example')


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
