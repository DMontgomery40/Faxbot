"""OAuth verifier instances keep their own configuration and signing-key caches."""
import base64
import time

import httpx
import pytest


@pytest.mark.asyncio
async def test_oauth_configuration_and_jwks_caches_do_not_leak_between_instances(monkeypatch):
    from python_mcp.transport_config import BearerTokenVerifier, OAuthConfiguration

    original_client = httpx.AsyncClient
    requested_urls = []

    def response(request):
        requested_urls.append(str(request.url))
        return httpx.Response(200, json={'keys': [{'kid': 'synthetic-kid'}]})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs:
                        original_client(*args, transport=httpx.MockTransport(response), **kwargs))
    from jose import jwt
    monkeypatch.setattr(jwt, 'get_unverified_header', lambda token: {'kid': 'synthetic-kid'})
    decoded = []

    def decode(token, key, **kwargs):
        decoded.append((kwargs['issuer'], kwargs['audience']))
        return {'sub': 'synthetic-user'}

    monkeypatch.setattr(jwt, 'decode', decode)
    first = BearerTokenVerifier(OAuthConfiguration('https://first.invalid/', 'first-audience'))
    second = BearerTokenVerifier(OAuthConfiguration('https://second.invalid', 'second-audience'))
    await first.verify('Bearer synthetic-token')
    await second.verify('Bearer synthetic-token')
    await first.verify('Bearer synthetic-token')
    assert requested_urls == ['https://first.invalid/.well-known/jwks.json',
                              'https://second.invalid/.well-known/jwks.json']
    assert decoded == [('https://first.invalid', 'first-audience'),
                       ('https://second.invalid', 'second-audience'),
                       ('https://first.invalid', 'first-audience')]


@pytest.mark.asyncio
async def test_oauth_verifier_checks_real_signature_issuer_audience_and_expiry(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from jose import jwt
    from python_mcp.transport_config import BearerTokenVerifier, OAuthConfiguration

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = key.public_key().public_numbers()

    def encoded(value):
        return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, 'big')).rstrip(b'=').decode()

    jwks = {'keys': [{'kty': 'RSA', 'kid': 'synthetic-kid', 'use': 'sig',
                      'n': encoded(public.n), 'e': encoded(public.e)}]}
    verifier = BearerTokenVerifier(OAuthConfiguration('https://issuer.invalid', 'synthetic-audience'))
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs:
                        original_client(*args, transport=httpx.MockTransport(
                            lambda request: httpx.Response(200, json=jwks)), **kwargs))
    claims = {'sub': 'synthetic-user', 'iss': 'https://issuer.invalid',
              'aud': 'synthetic-audience', 'exp': int(time.time()) + 300}

    def token(payload):
        return 'Bearer ' + jwt.encode(payload, private, algorithm='RS256', headers={'kid': 'synthetic-kid'})

    assert (await verifier.verify(token(claims)))['sub'] == 'synthetic-user'
    for changes in ({'iss': 'https://wrong.invalid'}, {'aud': 'wrong-audience'},
                    {'exp': int(time.time()) - 60}):
        with pytest.raises(jwt.JWTError):
            await verifier.verify(token({**claims, **changes}))
