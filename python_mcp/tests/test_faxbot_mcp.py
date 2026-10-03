"""Tool contract, per-caller identity and stdio cleanliness for the Python MCP servers."""
import asyncio
import base64
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.types.version import LATEST_HANDSHAKE_VERSION, LATEST_PROTOCOL_VERSION

from python_mcp import faxbot_tools, http_server, server, stdio_server

TOOLS = {'send_fax', 'get_fax_status', 'get_fax', 'list_inbound', 'get_inbound_pdf'}
STDIO = str(Path(__file__).resolve().parents[1] / 'stdio_server.py')
PDF_B64 = base64.b64encode(b'%PDF-1.4 outbound').decode()
PDF_B64 = PDF_B64[:8] + '\n' + PDF_B64[8:]  # line-wrapped base64 is accepted


def _http_client(headers):
    return httpx2.AsyncClient(headers=headers, timeout=10)


@pytest.mark.parametrize('module', [http_server, server])
@pytest.mark.asyncio
async def test_every_transport_lists_the_same_tool_names(module):
    application = module.create_app(api_base_url='http://faxbot.invalid', require_oauth=False)
    async with application.router.lifespan_context(application):
        tools = {tool.name: tool for tool in await application.state.mcp.list_tools()}
    assert set(tools) == TOOLS
    send = tools['send_fax']
    assert send.input_schema['required'] == ['to', 'fileContent', 'fileName']
    # Network transports never read files or URLs on the MCP host.
    assert not {'filePath', 'fileUrl'} & set(send.input_schema['properties'])
    assert send.output_schema['properties'].keys() >= {'id', 'status', 'operationId'}
    assert 'operationId' not in tools['get_fax_status'].output_schema['properties']
    assert 'same number and document' in send.input_schema['properties']['operationId']['description']
    assert send.annotations.idempotent_hint is False
    assert tools['list_inbound'].output_schema['properties'].keys() == {'items'}


@pytest.mark.parametrize('module', [http_server, server])
def test_factories_do_not_change_the_environment(module):
    before = dict(os.environ)
    module.create_app(api_base_url='http://faxbot.invalid', api_key='synthetic-key', require_oauth=False)
    assert dict(os.environ) == before


@pytest.mark.asyncio
async def test_stdio_tools_offer_local_files_and_an_inbound_pdf_resource():
    tools = {tool.name: tool for tool in await stdio_server.mcp.list_tools()}
    assert set(tools) == TOOLS
    assert {'filePath', 'fileUrl'} <= set(tools['send_fax'].input_schema['properties'])
    assert tools['send_fax'].input_schema['required'] == ['to']
    assert 'operationId' in tools['send_fax'].input_schema['properties']
    templates = await stdio_server.mcp.list_resource_templates()
    assert [template.uri_template for template in templates] == ['faxbot://inbound/{inbound_id}/pdf']


async def _exercise(transport, caller, mode):
    """Run every tool and the resource as one caller; return the structured results."""
    async with Client(transport, cache=None, mode=mode) as client:
        sent = await client.call_tool('send_fax', {'to': '+15551234567', 'fileContent': PDF_B64,
                                                   'fileName': 'letter.pdf'})
        status = await client.call_tool('get_fax_status', {'jobId': f'status-{caller}'})
        details = await client.call_tool('get_fax', {'id': 'a1b2c3'})
        missing = await client.call_tool('get_fax', {'id': 'missing'})
        inbound = await client.call_tool('list_inbound', {'limit': 5})
        pdf = await client.call_tool('get_inbound_pdf', {'inboundId': 'a1b2c3', 'asBase64': True})
        link = await client.call_tool('get_inbound_pdf', {'inboundId': 'a1b2c3'})
        resource = await client.read_resource('faxbot://inbound/a1b2c3/pdf')
        assert missing.is_error and '404' in missing.content[0].text
        return client.protocol_version, sent, status, details, inbound, pdf, link, resource


@pytest.mark.parametrize('transport', ['streamable-http', 'sse'])
@pytest.mark.asyncio
async def test_each_caller_key_is_forwarded_and_the_environment_key_never_is(
        transport, fake_faxbot, serve, monkeypatch):
    monkeypatch.setenv('API_KEY', 'environment-key-must-not-be-used')
    module = http_server if transport == 'streamable-http' else server
    calls = []

    def provider():
        calls.append(1)
        return faxbot_tools.APIConfiguration(fake_faxbot.url, 'installation-key-must-not-be-used')

    application = module.create_app(api_config_provider=provider, api_key='installation-key-must-not-be-used',
                                    require_oauth=False)
    with serve(application) as running:
        def connect(headers):
            if transport == 'sse':
                return sse_client(running.url + '/sse', headers=headers)
            return streamable_http_client(running.url + '/mcp', http_client=_http_client(headers))

        alice, bob = await asyncio.gather(
            _exercise(connect({'X-API-Key': 'alice-key'}), 'alice', 'auto'),
            _exercise(connect({'Authorization': 'Bearer bob-key'}), 'bob', 'legacy'),
        )

    # alice negotiates the current revision; bob is an initialize-handshake (2025) client.
    for caller, key, expected, result in (('alice', 'alice-key', LATEST_PROTOCOL_VERSION, alice),
                                          ('bob', 'bob-key', LATEST_HANDSHAKE_VERSION, bob)):
        version, sent, status, details, inbound, pdf, link, resource = result
        assert version == expected
        operation = next(post['operation'] for post in fake_faxbot.posts() if post['key'] == key)
        assert sent.structured_content == {'id': f'job-1-for-{key}', 'status': 'queued', 'operationId': operation}
        assert status.structured_content['id'] == f'status-{caller}'
        assert details.structured_content['direction'] == 'inbound'  # /fax/a1b2c3 is 404, so /inbound is used
        assert inbound.structured_content['items'][0]['id'] == 'a1b2c3'
        assert base64.b64decode(pdf.content[0].resource.blob).startswith(b'%PDF')
        assert link.content[0].uri == 'faxbot://inbound/a1b2c3/pdf'
        assert base64.b64decode(resource.contents[0].blob).startswith(b'%PDF')
    status_keys = {request['path']: request['key'] for request in fake_faxbot.requests
                   if request['path'].startswith('/fax/status-')}
    assert status_keys == {'/fax/status-alice': 'alice-key', '/fax/status-bob': 'bob-key'}
    assert set(fake_faxbot.keys()) == {'alice-key', 'bob-key'}
    assert len(calls) == 14, 'the provider supplies the base URL once per Faxbot-bound call'


@pytest.mark.parametrize('module, path', [(http_server, '/mcp'), (server, '/sse')])
@pytest.mark.asyncio
async def test_requests_without_a_caller_key_are_refused_before_faxbot(module, path, fake_faxbot, monkeypatch):
    monkeypatch.setattr(module, 'FAX_API_URL', fake_faxbot.url)
    monkeypatch.setenv('API_KEY', 'environment-key-must-not-be-used')
    application = module.create_app(api_base_url=fake_faxbot.url, api_key='ignored', require_oauth=False)
    async with application.router.lifespan_context(application):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=application),
                                      base_url='http://127.0.0.1') as client:
            for headers in ({}, {'X-API-Key': ''}, {'Authorization': 'Bearer '}):
                response = await client.post(path, headers=headers, json={
                    'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'name': 'get_fax_status', 'arguments': {'jobId': 'x'}}})
                assert response.status_code == 401
                assert response.json() == {'error': 'Unauthorized'}
                assert response.headers['WWW-Authenticate'].startswith('Bearer')
            browser = await client.post(path, headers={'X-API-Key': 'k', 'Origin': 'https://evil.invalid'}, json={})
            assert browser.status_code == 403
            assert (await client.get('/health')).json()['status'] == 'ok'
    assert fake_faxbot.requests == []


@pytest.mark.asyncio
async def test_host_allowlist_is_off_by_default_and_enforced_when_configured():
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}}
    headers = {'X-API-Key': 'k', 'Host': 'faxbot.lan:3004', 'Accept': 'application/json, text/event-stream'}
    for allowed, expected in ((None, 200), (['mcp.example.test'], 421), (['faxbot.lan:*'], 200)):
        application = http_server.create_app(require_oauth=False, allowed_hosts=allowed or [])
        async with application.router.lifespan_context(application):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=application),
                                          base_url='http://faxbot.lan:3004') as client:
                assert (await client.post('/mcp', headers=headers, json=body)).status_code == expected


@pytest.mark.parametrize('module, path', [(http_server, '/mcp'), (server, '/sse')])
@pytest.mark.asyncio
async def test_explicit_oauth_rejects_missing_and_malformed_tokens(module, path):
    application = module.create_app(require_oauth=True, oauth_issuer='https://issuer.invalid',
                                    oauth_audience='synthetic-audience')
    async with application.router.lifespan_context(application):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=application),
                                      base_url='http://127.0.0.1') as client:
            for headers in ({}, {'Authorization': 'Bearer synthetic-invalid-token'}, {'X-API-Key': 'raw-key'}):
                response = await client.get(path, headers=headers)
                assert response.status_code == 401
                assert response.json() == {'error': 'Unauthorized'}
            assert (await client.get('/health')).status_code == 200


def _rsa_jwks():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    numbers = key.public_key().public_numbers()

    def encoded(value):
        return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, 'big')).rstrip(b'=').decode()

    return private, {'keys': [{'kty': 'RSA', 'kid': 'kid-1', 'use': 'sig', 'n': encoded(numbers.n),
                               'e': encoded(numbers.e)}]}


@pytest.mark.asyncio
async def test_oauth_subject_maps_to_its_stored_faxbot_key(fake_faxbot, serve, tmp_path):
    from jose import jwt

    private, fake_faxbot.jwks = _rsa_jwks()
    keys_file = tmp_path / 'subject-keys.json'
    keys_file.write_text(json.dumps({'alice@example.test': 'alice-faxbot-key'}))
    application = http_server.create_app(
        api_base_url=fake_faxbot.url, require_oauth=True, oauth_issuer=fake_faxbot.url,
        oauth_audience='faxbot-mcp', subject_keys_file=str(keys_file),
        resource_url='https://mcp.example.test/mcp')

    def token(subject):
        claims = {'sub': subject, 'iss': fake_faxbot.url, 'aud': 'faxbot-mcp', 'exp': int(time.time()) + 300}
        return jwt.encode(claims, private, algorithm='RS256', headers={'kid': 'kid-1'})

    with serve(application) as running:
        headers = {'Authorization': f'Bearer {token("alice@example.test")}', 'X-API-Key': 'ignored-client-key'}
        async with Client(streamable_http_client(running.url + '/mcp', http_client=_http_client(headers)),
                          cache=None) as client:
            result = await client.call_tool('get_fax_status', {'jobId': 'oauth-job'})
        assert result.structured_content['id'] == 'oauth-job'
        async with httpx2.AsyncClient() as raw:
            unmapped = await raw.post(running.url + '/mcp', json={},
                                      headers={'Authorization': f'Bearer {token("mallory@example.test")}'})
            challenge = await raw.post(running.url + '/mcp', json={})
            advertised = challenge.headers['WWW-Authenticate'].split('resource_metadata="')[1].rstrip('"')
            # A client follows the advertised URL; serve it at that path behind this origin.
            metadata = await raw.get(running.url + urlsplit(advertised).path)
    assert unmapped.status_code == 403
    assert challenge.headers['WWW-Authenticate'] == (
        'Bearer resource_metadata="https://mcp.example.test/mcp/.well-known/oauth-protected-resource"')
    assert metadata.status_code == 200
    assert metadata.json()['authorization_servers'] == [fake_faxbot.url]
    assert fake_faxbot.keys() == ['alice-faxbot-key']


@pytest.mark.asyncio
async def test_list_inbound_accepts_a_bare_list_or_an_items_envelope(fake_faxbot, stdio_env):
    params = StdioServerParameters(command=sys.executable, args=[STDIO], env=stdio_env)
    async with Client(params, cache=None) as client:
        bare = await client.call_tool('list_inbound', {})
        fake_faxbot.inbound_envelope = True
        wrapped = await client.call_tool('list_inbound', {})
    assert bare.structured_content == wrapped.structured_content
    assert bare.structured_content['items'][0]['fr'] == '+15550001111'
    assert faxbot_tools.inbound_items([{'id': 'x'}]) == faxbot_tools.inbound_items({'items': [{'id': 'x'}]})


@pytest.mark.asyncio
async def test_stdio_uses_its_single_configured_key_and_negotiates_the_current_protocol(
        fake_faxbot, stdio_env, tmp_path):
    document = tmp_path / 'note.txt'
    document.write_text('hello fax')
    params = StdioServerParameters(command=sys.executable, args=[STDIO], env=stdio_env)
    async with Client(params, cache=None) as client:
        assert client.protocol_version == LATEST_PROTOCOL_VERSION
        sent = await client.call_tool('send_fax', {'to': '+15551234567', 'filePath': str(document)})
    assert sent.structured_content == {'id': 'job-1-for-stdio-integration-key', 'status': 'queued',
                                       'operationId': fake_faxbot.requests[0]['operation']}
    assert fake_faxbot.keys() == ['stdio-integration-key']
    assert b'hello fax' in fake_faxbot.requests[0]['body']


def test_stdio_stdout_carries_only_json_rpc(stdio_env):
    """Every byte on stdout must be a JSON-RPC message, through a handshake and a tool error."""
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
         'params': {'name': 'get_fax_status', 'arguments': {'jobId': 'missing'}}},
    ]
    process = subprocess.Popen([sys.executable, STDIO], env=stdio_env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        lines = []
        for message in messages:
            process.stdin.write((json.dumps(message) + '\n').encode())
            process.stdin.flush()
            if 'id' in message:
                lines.append(process.stdout.readline())
    finally:
        process.stdin.close()
        process.wait(10)
    remainder = process.stdout.read()
    process.stdout.close()
    process.stderr.close()
    replies = [json.loads(line) for line in lines]
    assert all(reply['jsonrpc'] == '2.0' for reply in replies)
    assert [reply['id'] for reply in replies] == [1, 2, 3]
    assert {tool['name'] for tool in replies[1]['result']['tools']} == TOOLS
    assert replies[2]['result']['isError'] is True
    for line in remainder.splitlines():
        assert json.loads(line)['jsonrpc'] == '2.0'


def _send_once_client(url, key='mcp-key'):
    """The stdio tool set in process, so the retry settings can be shortened."""
    server = MCPServer('send-once-test', version='0')
    return Client(faxbot_tools.register_tools(
        server, lambda _ctx: faxbot_tools.APIConfiguration(url, key), local_files=True), cache=None)


CONFLICT = 'Idempotency-Key already belongs to a different fax request.'
SEND = {'to': '+15551234567', 'fileContent': base64.b64encode(b'%PDF-1.4 once').decode(), 'fileName': 'once.pdf'}
OTHER_DOCUMENT = base64.b64encode(b'%PDF-1.4 a different document').decode()


@pytest.fixture
def quick_retries(monkeypatch):
    monkeypatch.setattr(faxbot_tools, 'SEND_RETRIES', 2)
    monkeypatch.setattr(faxbot_tools, 'SEND_RETRY_BACKOFF', 0)


@pytest.mark.asyncio
async def test_a_lost_response_is_recovered_with_the_same_operation_id(fake_faxbot, quick_retries):
    fake_faxbot.plan = ['drop']
    async with _send_once_client(fake_faxbot.url) as client:
        sent = await client.call_tool('send_fax', SEND)
    posts = fake_faxbot.posts()
    assert not sent.is_error
    assert [post['operation'] for post in posts] == [sent.structured_content['operationId']] * 2
    assert b'%PDF-1.4 once' in posts[1]['body']
    assert [job['id'] for job in fake_faxbot.jobs] == [sent.structured_content['id']]


@pytest.mark.asyncio
async def test_an_unconfirmed_fax_is_finished_with_its_operation_id(fake_faxbot, quick_retries):
    fake_faxbot.plan = ['uncertain'] * 3
    async with _send_once_client(fake_faxbot.url) as client:
        failed = await client.call_tool('send_fax', SEND)
        operation = fake_faxbot.posts()[0]['operation']
        assert failed.is_error
        assert [post['operation'] for post in fake_faxbot.posts()] == [operation] * 3
        assert failed.content[0].text.endswith(
            f'Faxbot did not confirm this fax (operationId {operation}). Call send_fax again with '
            f'operationId={operation} and the same number and document to finish this same fax '
            'without sending it twice.')
        assert len(fake_faxbot.jobs) == 1
        finished = await client.call_tool('send_fax', {**SEND, 'operationId': operation})
    assert finished.structured_content == {'id': fake_faxbot.jobs[0]['id'], 'status': 'queued',
                                           'operationId': operation}
    assert len(fake_faxbot.jobs) == 1


@pytest.mark.asyncio
async def test_a_send_that_never_reached_faxbot_is_finished_later(fake_faxbot, quick_retries):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        closed = f'http://127.0.0.1:{probe.getsockname()[1]}'
    async with _send_once_client(closed) as client:
        failed = await client.call_tool('send_fax', SEND)
    assert failed.is_error
    operation = re.search(r'operationId=(\S+) ', failed.content[0].text).group(1)
    async with _send_once_client(fake_faxbot.url) as client:
        first = await client.call_tool('send_fax', {**SEND, 'operationId': operation})
        again = await client.call_tool('send_fax', {**SEND, 'operationId': operation})
    assert first.structured_content == again.structured_content
    assert first.structured_content['operationId'] == operation
    assert len(fake_faxbot.jobs) == 1


@pytest.mark.asyncio
async def test_an_operation_id_belongs_to_one_fax(fake_faxbot, quick_retries):
    async with _send_once_client(fake_faxbot.url) as client:
        operation = (await client.call_tool('send_fax', SEND)).structured_content['operationId']
        changed = [await client.call_tool('send_fax', {**SEND, **change, 'operationId': operation})
                   for change in ({'fileContent': OTHER_DOCUMENT}, {'to': '+15559876543'})]
    for result in changed:
        assert result.is_error
        assert f'Fax API error 409: {CONFLICT}' in result.content[0].text
        assert f'operationId {operation} belongs to a different number or document' in result.content[0].text
    assert len(fake_faxbot.posts()) == 3, 'a conflict is never retried'
    assert len(fake_faxbot.jobs) == 1


@pytest.mark.asyncio
async def test_each_send_without_an_operation_id_is_a_new_fax(fake_faxbot, quick_retries):
    async with _send_once_client(fake_faxbot.url) as client:
        first = await client.call_tool('send_fax', SEND)
        second = await client.call_tool('send_fax', SEND)
    assert first.structured_content['id'] != second.structured_content['id']
    assert first.structured_content['operationId'] != second.structured_content['operationId']
    assert len(fake_faxbot.jobs) == 2


@pytest.mark.parametrize('status', [400, 401, 404, 409, 413, 429, 500])
@pytest.mark.asyncio
async def test_only_unconfirmed_answers_are_retried(fake_faxbot, quick_retries, status):
    fake_faxbot.plan = [status]
    async with _send_once_client(fake_faxbot.url) as client:
        result = await client.call_tool('send_fax', SEND)
    assert result.is_error and f'Fax API error {status}: synthetic {status}' in result.content[0].text
    assert len(fake_faxbot.posts()) == 1


@pytest.mark.asyncio
async def test_gateway_failures_are_retried_with_the_same_operation_id(fake_faxbot, quick_retries, tmp_path):
    document = tmp_path / 'letter.pdf'
    document.write_bytes(b'%PDF-1.4 local')
    fake_faxbot.plan = [502, 504]
    async with _send_once_client(fake_faxbot.url) as client:
        sent = await client.call_tool('send_fax', {'to': '+15551234567', 'filePath': str(document)})
    assert [post['operation'] for post in fake_faxbot.posts()] == [sent.structured_content['operationId']] * 3
    assert all(b'%PDF-1.4 local' in post['body'] for post in fake_faxbot.posts())
    assert len(fake_faxbot.jobs) == 1
