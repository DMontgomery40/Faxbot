"""Embedded MCP instances own immutable settings without process env changes."""
import asyncio
import base64
import os
import time

import httpx
import pytest

from python_mcp import http_server, server


@pytest.mark.parametrize('module, names', [
    (server, {'send_fax', 'get_fax_status', 'list_inbound', 'get_fax', 'get_inbound_pdf'}),
    (http_server, {'send_fax', 'get_fax_status'}),
])
@pytest.mark.asyncio
async def test_factories_keep_tool_contracts_and_do_not_change_environment(module, names):
    before = dict(os.environ)
    application = module.create_app(api_base_url='http://example.invalid/api', api_key='synthetic-key',
                                    require_oauth=False)
    assert dict(os.environ) == before
    async with application.router.lifespan_context(application):
        tools = await application.state.mcp.list_tools()
        assert {tool.name for tool in tools} == names
        send = next(tool for tool in tools if tool.name == 'send_fax')
        assert 'to' in send.inputSchema['required']
        if module is server:
            assert {'filePath', 'fileUrl'} <= set(send.inputSchema['properties'])
    assert dict(os.environ) == before


@pytest.mark.parametrize('module', [server, http_server])
@pytest.mark.asyncio
async def test_api_configuration_isolated_per_app_and_resolved_once_per_tool(module, monkeypatch):
    from python_mcp.transport_config import APIConfiguration

    original_client = httpx.AsyncClient
    requests = []

    async def response(request):
        requests.append((str(request.url), request.headers.get('X-API-Key')))
        await asyncio.sleep(0)
        return httpx.Response(200, json={'id': 'synthetic-job', 'status': 'completed'})

    def client(*args, **kwargs):
        return original_client(*args, transport=httpx.MockTransport(response), **kwargs)

    monkeypatch.setattr(module.httpx, 'AsyncClient', client)
    current = APIConfiguration('http://first.invalid', 'synthetic-first-key')
    calls = []

    def configuration():
        calls.append(current)
        return current

    first = module.create_app(api_config_provider=configuration, require_oauth=False)
    second = module.create_app(api_base_url='http://second.invalid', api_key='synthetic-second-key',
                               require_oauth=False)
    async with first.router.lifespan_context(first), second.router.lifespan_context(second):
        await asyncio.gather(
            first.state.mcp.call_tool('get_fax_status', {'jobId': 'synthetic-job'}),
            second.state.mcp.call_tool('get_fax_status', {'jobId': 'synthetic-job'}),
        )
        current = APIConfiguration('http://first.invalid', 'synthetic-rotated-key')
        await first.state.mcp.call_tool('get_fax_status', {'jobId': 'synthetic-job'})
    assert requests == [
        ('http://first.invalid/fax/synthetic-job', 'synthetic-first-key'),
        ('http://second.invalid/fax/synthetic-job', 'synthetic-second-key'),
        ('http://first.invalid/fax/synthetic-job', 'synthetic-rotated-key'),
    ]
    assert len(calls) == 2


@pytest.mark.parametrize('module', [server, http_server])
@pytest.mark.asyncio
async def test_explicit_empty_api_key_does_not_reuse_module_environment_key(module, monkeypatch):
    original_client = httpx.AsyncClient
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(200, json={'id': 'synthetic-job', 'status': 'completed'})

    monkeypatch.setattr(module, 'API_KEY', 'synthetic-imported-key')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *args, **kwargs:
                        original_client(*args, transport=httpx.MockTransport(response), **kwargs))
    application = module.create_app(api_base_url='http://explicit.invalid/', api_key='', require_oauth=False)
    async with application.router.lifespan_context(application):
        await application.state.mcp.call_tool('get_fax_status', {'jobId': 'synthetic-job'})
    assert str(requests[0].url) == 'http://explicit.invalid/fax/synthetic-job'
    assert 'X-API-Key' not in requests[0].headers


@pytest.mark.parametrize('module, path', [(server, '/sse'), (http_server, '/mcp')])
@pytest.mark.asyncio
async def test_explicit_oauth_rejects_missing_and_malformed_tokens_for_both_transports(module, path):
    application = module.create_app(require_oauth=True, oauth_issuer='https://issuer.invalid',
                                    oauth_audience='synthetic-audience')
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url='http://127.0.0.1') as client:
            for headers in ({}, {'Authorization': 'Bearer synthetic-invalid-token'}):
                response = await client.get(path, headers=headers)
                assert response.status_code == 401
                assert response.json() == {'error': 'Unauthorized'}
            assert (await client.get('/health')).status_code == 200


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
