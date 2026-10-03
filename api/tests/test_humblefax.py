"""HumbleFax captured adapter against a loopback fake of the documented API.

Requests travel over real sockets to a local server that speaks the wire
shapes published at https://api.humblefax.com/ (QuickSendFax and GetSentFax).
The production origin stays fixed; only the test transport changes the socket.
"""
import asyncio
import base64
from dataclasses import FrozenInstanceError
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from api.app.config_profiles import ProviderConfiguration, ProviderProfile
from api.app.config_values import ConfigurationValueError, ConfigurationValues
from api.app.humblefax_service import (
    USER_AGENT, HumbleFaxCredentialsError, HumbleFaxFaxService, humblefax_number,
)
from api.app.outbound_polling import OutboundPoller
from api.app.outbound_transport import CapturedTransport
from api.app.outbound_worker import OutboundWorker
from api.tests.test_outbound_store import installation, accept
from api.tests.test_outbound_transport import Runtime
from api.tests.test_schema import database


ACCESS = 'synthetic-access-key'
SECRET = 'synthetic.secret+/=='
BASIC = 'Basic ' + base64.b64encode((ACCESS + ':' + SECRET).encode()).decode()
DESTINATION = '+12025550123'
PDF = b'%PDF-1.4\nsynthetic fax document\n%%EOF\n'
PRIVATE = 'synthetic-private-provider-detail'
LOGIN_FAILURE = {'result': 'failure', 'error': 'loginFailure'}


def created(fax_id=123456, status='in progress', container='fax'):
    return {'data': {'result': 'Fax sent to queue', container: {
        'id': fax_id, 'status': status, 'senderEmail': PRIVATE, 'numPages': 1,
        'recipients': [{'status': status, 'toNumber': 12025550123}]}}}


def sent(fax_id='123456', status='success'):
    return {'data': {'sentFax': {'id': fax_id, 'status': status, 'senderEmail': PRIVATE,
        'numSuccesses': 1, 'numFailures': 0}}}


class FakeHumbleFax:
    """Scripted loopback server recording each request it receives."""

    def __init__(self):
        self.requests, self.replies, self.lock = [], [], threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else b''
                with fake.lock:
                    fake.requests.append({'method': self.command, 'path': self.path,
                        'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': body})
                    reply = fake.replies.pop(0) if fake.replies else (599, {'error': 'unscripted'}, 0.0, {})
                status, payload, delay, headers = reply
                if delay:
                    time.sleep(delay)
                content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(content)))
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(content)
                except OSError:
                    pass  # The client already abandoned a deliberately slow reply.

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def reply(self, status, payload, *, delay=0.0, headers=None):
        self.replies.append((status, payload, delay, headers or {}))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class LoopbackTransport(httpx.AsyncBaseTransport):
    """Route the fixed HTTPS production origin to the loopback fake."""

    def __init__(self, port, *, read_timeout=None):
        self.port, self.read_timeout, self.urls = port, read_timeout, []

    async def handle_async_request(self, request):
        self.urls.append(str(request.url))
        assert request.url.scheme == 'https' and request.url.host == 'api.humblefax.com'
        request.url = request.url.copy_with(scheme='http', host='127.0.0.1', port=self.port)
        if self.read_timeout is not None:
            request.extensions = {**request.extensions,
                'timeout': {**request.extensions.get('timeout', {}), 'read': self.read_timeout}}
        async with httpx.AsyncHTTPTransport() as inner:
            response = await inner.handle_async_request(request)
            content = await response.aread()
        return httpx.Response(response.status_code, headers=response.headers, content=content)


@pytest.fixture
def fake():
    server = FakeHumbleFax()
    yield server
    server.close()


@pytest.fixture
def document(tmp_path):
    path = tmp_path / 'private-original-name.pdf'
    path.write_bytes(PDF)
    return str(path)


def service(fake, *, from_number='', read_timeout=None):
    transport = LoopbackTransport(fake.port, read_timeout=read_timeout)
    return HumbleFaxFaxService(ACCESS, SECRET, from_number, transport=transport), transport


def multipart(request):
    mime = BytesParser(policy=default).parsebytes(
        b'Content-Type: ' + request['headers']['content-type'].encode('ascii')
        + b'\r\nMIME-Version: 1.0\r\n\r\n' + request['body'])
    assert mime.is_multipart()
    return {part.get_param('name', header='content-disposition'): part for part in mime.iter_parts()}


def assert_safe(error, *values):
    for value in (PRIVATE, ACCESS, SECRET, *values):
        assert value not in str(error)


# Settings, redaction and captured profile wiring

def test_settings_use_documented_aliases_and_redact_both_keys():
    from api.app.config_store import ConfigurationRevision, ConfigurationSnapshot
    from api.app.config_views import project_admin_settings
    values = ConfigurationValues.from_environment({'HUMBLEFAX_ACCESS_KEY': ACCESS,
        'HUMBLEFAX_SECRET_KEY': SECRET, 'HUMBLEFAX_FROM_NUMBER': '13035550199'})
    assert (values.humblefax_access_key, values.humblefax_secret_key,
            values.humblefax_from_number) == (ACCESS, SECRET, '13035550199')
    assert ACCESS not in repr(values) and SECRET not in repr(values)
    private = values.to_environment()
    assert private['HUMBLEFAX_ACCESS_KEY'] == ACCESS and private['HUMBLEFAX_SECRET_KEY'] == SECRET
    redacted = values.to_environment(redact_secrets=True)
    assert redacted['HUMBLEFAX_ACCESS_KEY'] == redacted['HUMBLEFAX_SECRET_KEY'] == '***'
    assert redacted['HUMBLEFAX_FROM_NUMBER'] == '13035550199'
    view = project_admin_settings(ConfigurationSnapshot('installation', 1,
        ConfigurationRevision('active', values), None))
    assert view['humblefax'] == {'access_key': '***', 'secret_key': '***',
                                 'from_number': '13035550199', 'configured': True}
    assert ACCESS not in json.dumps(view) and SECRET not in json.dumps(view)
    empty = project_admin_settings(ConfigurationSnapshot('installation', 1,
        ConfigurationRevision('active', ConfigurationValues.from_environment({})), None))
    assert empty['humblefax'] == {'access_key': '', 'secret_key': '', 'from_number': '', 'configured': False}


@pytest.mark.parametrize('number', ['+2025550123', '303-555-0199', '22025550123', '0025550123', '+442071234567', ' 3035550199'])
def test_caller_number_setting_requires_us_or_canadian_digits(number):
    with pytest.raises(ConfigurationValueError) as failure:
        ConfigurationValues.from_environment({'HUMBLEFAX_FROM_NUMBER': number})
    assert [issue['field'] for issue in failure.value.issues] == ['HUMBLEFAX_FROM_NUMBER']
    assert number not in str(failure.value)


def test_masked_humblefax_secret_cannot_replace_the_stored_key():
    values = ConfigurationValues.from_environment({'HUMBLEFAX_SECRET_KEY': SECRET})
    with pytest.raises(ConfigurationValueError) as failure:
        values.with_patch({'humblefax_secret_key': '***'})
    assert failure.value.issues == ({'field': 'HUMBLEFAX_SECRET_KEY', 'reason': 'masked_secret'},)


@pytest.mark.parametrize(('environment', 'configured'), [
    ({'HUMBLEFAX_ACCESS_KEY': ACCESS, 'HUMBLEFAX_SECRET_KEY': SECRET, 'HUMBLEFAX_FROM_NUMBER': '+13035550199'}, True),
    ({'HUMBLEFAX_ACCESS_KEY': ACCESS}, False),
    ({}, False),
])
def test_selected_provider_compiles_a_captured_profile_and_readiness_needs_both_keys(
        tmp_path, monkeypatch, environment, configured):
    from api.app.config_activation import compile_profiles
    from api.app.config_bootstrap import default_plugin_state
    from api.app.config_paths import provider_traits_path
    from api.app.provider_catalog import ProviderCatalog
    from api.app.provider_execution import service_from_profile
    values = ConfigurationValues.from_environment({'FAX_OUTBOUND_BACKEND': 'humblefax', **environment})
    catalog = ProviderCatalog.load(provider_traits_path(), tmp_path / 'providers')
    profile = compile_profiles(values, catalog, default_plugin_state(values))['outbound']
    assert profile.provider_id == 'humblefax' and profile.manifest is None
    assert profile.traits['supports_inbound'] is False and profile.traits['requires_tiff'] is False
    monkeypatch.setenv('HUMBLEFAX_ACCESS_KEY', 'unrelated-current-key')
    monkeypatch.setenv('HUMBLEFAX_SECRET_KEY', 'unrelated-current-secret')
    adapter = service_from_profile(ProviderProfile('profile', 'account', profile))
    assert isinstance(adapter, HumbleFaxFaxService)
    assert adapter.is_configured() is configured
    assert adapter.access_key == environment.get('HUMBLEFAX_ACCESS_KEY', '')
    assert adapter.secret_key == environment.get('HUMBLEFAX_SECRET_KEY', '')
    assert adapter.from_number == environment.get('HUMBLEFAX_FROM_NUMBER', '')


def test_inbound_selection_is_refused_because_the_adapter_is_outbound_only(tmp_path):
    from api.app.config_activation import ConfigurationActivationError, compile_profiles
    from api.app.config_bootstrap import default_plugin_state
    from api.app.config_paths import provider_traits_path
    from api.app.provider_catalog import ProviderCatalog
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'humblefax', 'INBOUND_ENABLED': 'true'})
    catalog = ProviderCatalog.load(provider_traits_path(), tmp_path / 'providers')
    with pytest.raises(ConfigurationActivationError, match='does not support inbound'):
        compile_profiles(values, catalog, default_plugin_state(values))


# Adapter wire contract

@pytest.mark.parametrize(('from_number', 'expected_from'), [
    ('', None), ('3035550199', 13035550199), ('+13035550199', 13035550199)])
def test_send_is_one_authenticated_quicksend_with_documented_json_and_pdf(
        fake, document, monkeypatch, from_number, expected_from):
    fake.reply(200, created())
    adapter, transport = service(fake, from_number=from_number)
    monkeypatch.setenv('HUMBLEFAX_ACCESS_KEY', 'unrelated-current-key')
    uuid = uuid4().hex

    result = asyncio.run(adapter.send_fax_file(DESTINATION, document, uuid=uuid))

    assert result == {'provider_sid': '123456', 'status': 'in_progress'}
    assert transport.urls == ['https://api.humblefax.com/quickSendFax']
    [request] = fake.requests
    assert request['method'] == 'POST' and request['path'] == '/quickSendFax'
    assert request['headers']['host'] == 'api.humblefax.com'
    assert request['headers']['authorization'] == BASIC
    assert request['headers']['user-agent'] == USER_AGENT
    assert 'python' not in USER_AGENT.lower() and USER_AGENT.startswith('Mozilla/5.0 (')
    parts = multipart(request)
    assert set(parts) == {'jsonData', 'file'}
    body = json.loads(parts['jsonData'].get_payload(decode=True))
    expected = {'recipients': [12025550123], 'includeCoversheet': False,
                'resolution': 'Fine', 'pageSize': 'Letter', 'uuid': uuid}
    if expected_from is not None:
        expected['fromNumber'] = expected_from
    assert body == expected
    assert all(type(value) is int for value in body['recipients'])
    assert parts['file'].get_payload(decode=True) == PDF
    assert parts['file'].get_content_type() == 'application/pdf'
    assert parts['file'].get_filename() == 'document.pdf'
    assert b'private-original-name' not in request['body']
    assert '?' not in request['path']


@pytest.mark.parametrize(('destination', 'recipient'), [
    ('+12025550123', 12025550123), ('12025550123', 12025550123), ('2025550123', 12025550123),
    ('+16135550123', 16135550123)])
def test_us_and_canadian_destinations_use_the_eleven_digit_integer(destination, recipient):
    assert humblefax_number(destination) == recipient


@pytest.mark.parametrize('destination', ['+442071234567', '+2025550123', '22025550123', '1202555012',
                                         '+1202555012a', '12345', '', None, 12025550123])
def test_unsupported_destination_is_refused_before_http(fake, document, destination):
    adapter, _ = service(fake)
    with pytest.raises(ValueError, match='HumbleFax fax number is invalid'):
        asyncio.run(adapter.send_fax_file(destination, document))
    assert fake.requests == []


@pytest.mark.parametrize('payload', [
    created(container='sentFax'), created(status=['in progress']), created(status='In Progress'),
    {'data': {'fax': {'id': '123456'}}}, created(status='scheduled')])
def test_documented_create_variants_yield_the_provider_identity(fake, document, payload):
    fake.reply(200, payload)
    adapter, _ = service(fake)
    assert asyncio.run(adapter.send_fax_file(DESTINATION, document)) == {
        'provider_sid': '123456', 'status': 'in_progress'}
    assert len(fake.requests) == 1


@pytest.mark.parametrize('operation', ['create', 'poll'])
def test_rejected_credentials_raise_a_clear_error_without_retry(fake, document, operation):
    fake.reply(401, LOGIN_FAILURE)
    adapter, _ = service(fake)
    with pytest.raises(HumbleFaxCredentialsError) as failure:
        asyncio.run(adapter.send_fax_file(DESTINATION, document) if operation == 'create'
                    else adapter.get_fax_status('123456'))
    assert str(failure.value) == 'HumbleFax rejected the account access key or secret key.'
    assert isinstance(failure.value, RuntimeError)
    assert len(fake.requests) == 1
    assert_safe(failure.value, 'loginFailure')


def test_create_timeout_is_ambiguous_and_never_resubmitted(fake, document, monkeypatch):
    from api.app import humblefax_service
    opened, actual_open = [], open

    def tracked_open(*args, **kwargs):
        handle = actual_open(*args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(humblefax_service, 'open', tracked_open, raising=False)
    fake.reply(200, created(), delay=1.0)
    adapter, _ = service(fake, read_timeout=0.2)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(adapter.send_fax_file(DESTINATION, document))
    assert str(failure.value) == 'HumbleFax create request failed.'
    assert not isinstance(failure.value, HumbleFaxCredentialsError)
    assert failure.value.__suppress_context__
    assert len(fake.requests) == 1
    assert len(opened) == 1 and opened[0].closed


@pytest.mark.parametrize(('status', 'payload', 'headers'), [
    (429, {'result': 'failure', 'error': PRIVATE}, {}),
    (403, {'result': 'failure', 'error': PRIVATE}, {}),
    (500, {'result': 'failure', 'error': PRIVATE}, {}),
    (302, created(), {'Location': 'https://other-account.invalid/quickSendFax'}),
    (200, PRIVATE.encode(), {}),
    (200, {'result': 'failure', 'error': PRIVATE}, {}),
    (200, {'data': {'result': 'Fax sent to queue'}}, {}),
    (200, {'data': {'fax': {'id': None, 'status': 'in progress'}}}, {}),
    (200, {'data': {'fax': {'id': True, 'status': 'in progress'}}}, {}),
    (200, {'data': {'fax': {'id': '12/34', 'status': 'in progress'}}}, {}),
    (200, {'data': {'fax': {'id': 123456, 'status': PRIVATE}}}, {}),
    (200, {'data': {'fax': {'id': 123456, 'status': ['in progress', 'success']}}}, {}),
    (200, {'data': {'fax': created()['data']['fax'], 'sentFax': created()['data']['fax']}}, {}),
    (200, [PRIVATE], {}),
])
def test_unusable_create_reply_is_sanitized_and_not_retried(fake, document, status, payload, headers):
    fake.reply(status, payload, headers=headers)
    adapter, _ = service(fake)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(adapter.send_fax_file(DESTINATION, document))
    assert str(failure.value) == 'HumbleFax create request failed.'
    assert failure.value.__suppress_context__
    assert len(fake.requests) == 1
    assert_safe(failure.value, document)


@pytest.mark.parametrize(('wire_status', 'expected'), [
    ('in progress', 'in_progress'), ('scheduled', 'in_progress'), ('success', 'success'),
    ('partial success', 'failed'), ('failure', 'failed'), ('image failure', 'failed'),
    ('cancelled', 'cancelled')])
def test_poll_maps_documented_humblefax_states_to_faxbot_states(fake, wire_status, expected):
    fake.reply(200, sent(status=wire_status))
    adapter, transport = service(fake)
    assert asyncio.run(adapter.get_fax_status('123456')) == {'provider_sid': '123456', 'status': expected}
    [request] = fake.requests
    assert request['method'] == 'GET' and request['path'] == '/sentFax/123456'
    assert request['headers']['authorization'] == BASIC
    assert request['headers']['user-agent'] == USER_AGENT
    assert not request['body']
    assert transport.urls == ['https://api.humblefax.com/sentFax/123456']


@pytest.mark.parametrize('payload', [
    sent(status='unknown'), sent(status=None), sent(status=''), sent(fax_id='654321'),
    {'data': {'sentFax': {'status': 'success'}}}, {'data': {'fax': {'id': '123456', 'status': 'success'}}},
    {'result': 'failure', 'error': PRIVATE}, [PRIVATE]])
def test_poll_requires_documented_status_and_the_same_identity(fake, payload):
    fake.reply(200, payload)
    adapter, _ = service(fake)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(adapter.get_fax_status('123456'))
    assert str(failure.value) == 'HumbleFax status request failed.'
    assert len(fake.requests) == 1
    assert_safe(failure.value)


def test_poll_timeout_is_a_sanitized_status_failure(fake):
    fake.reply(200, sent(), delay=1.0)
    adapter, _ = service(fake, read_timeout=0.2)
    with pytest.raises(RuntimeError, match='HumbleFax status request failed'):
        asyncio.run(adapter.get_fax_status('123456'))
    assert len(fake.requests) == 1


@pytest.mark.parametrize('sid', [None, '', 123456, True, '12/34', '123456?x=1', 'a' * 46])
def test_poll_identity_is_refused_before_http(fake, sid):
    adapter, _ = service(fake)
    with pytest.raises(ValueError, match='HumbleFax provider identity is invalid'):
        asyncio.run(adapter.get_fax_status(sid))
    assert fake.requests == []


@pytest.mark.parametrize(('access', 'secret', 'from_number'), [
    ('user:name', SECRET, ''), (' ' + ACCESS, SECRET, ''), (ACCESS, SECRET + ' ', ''),
    (ACCESS, 'bad\r\nInjected: value', ''), ('nonascii-é', SECRET, ''), (None, SECRET, ''),
    (ACCESS, SECRET, '+442071234567'), (ACCESS, SECRET, None)])
def test_invalid_captured_configuration_is_refused_before_io(access, secret, from_number):
    with pytest.raises(ValueError, match='HumbleFax configuration is invalid') as failure:
        HumbleFaxFaxService(access, secret, from_number)
    assert SECRET not in str(failure.value) and ACCESS not in str(failure.value)


def test_unconfigured_account_never_contacts_humblefax(fake, document):
    adapter = HumbleFaxFaxService(ACCESS, '', transport=LoopbackTransport(fake.port))
    assert not adapter.is_configured()
    with pytest.raises(ValueError, match='HumbleFax is not configured'):
        asyncio.run(adapter.send_fax_file(DESTINATION, document))
    with pytest.raises(ValueError, match='HumbleFax is not configured'):
        asyncio.run(adapter.get_fax_status('123456'))
    assert fake.requests == []


def test_captured_account_is_frozen_and_secrets_are_not_in_repr():
    adapter = HumbleFaxFaxService(ACCESS, SECRET, '13035550199')
    assert adapter.is_configured()
    assert ACCESS not in repr(adapter) and SECRET not in repr(adapter)
    with pytest.raises(FrozenInstanceError):
        adapter.secret_key = 'replacement'


def test_http_client_ignores_environment_proxies_and_redirects(fake, document, monkeypatch):
    actual_client, policies = httpx.AsyncClient, []

    def client(**kwargs):
        policies.append(kwargs)
        return actual_client(**kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    fake.reply(200, created())
    fake.reply(200, sent())
    adapter, _ = service(fake)
    asyncio.run(adapter.send_fax_file(DESTINATION, document))
    asyncio.run(adapter.get_fax_status('123456'))
    assert len(fake.requests) == 2 and len(policies) == 2
    for policy in policies:
        timeout = policy['timeout']
        assert policy['trust_env'] is False and policy['follow_redirects'] is False
        assert 0 < timeout.connect <= 10 and 0 < timeout.read <= 60


# Captured delivery through the worker, poller and operator reconciliation

def humblefax_job(installation, tmp_path, *, credentials=None, to_number=DESTINATION):
    configuration, store, snapshot = installation
    profile = ProviderConfiguration('humblefax',
        credentials={'access_key': ACCESS, 'secret_key': SECRET} if credentials is None else credentials,
        settings={'from_number': ''})
    snapshot = configuration.apply(snapshot, snapshot.active.values.with_patch({'fax_data_dir': str(tmp_path)}),
        actor='test', restart_required=False, providers={'outbound': profile})
    if to_number == DESTINATION:
        job = accept((configuration, store, snapshot))
    else:
        job, now = uuid4().hex, datetime.utcnow()
        configuration.accept_outbound(snapshot.active, {'id': job, 'to_number': to_number,
            'file_name': 'synthetic.txt', 'tiff_path': '', 'status': 'queued', 'pages': 1,
            'created_at': now, 'updated_at': now})
    (tmp_path / (job + '.pdf')).write_bytes(PDF)
    return job, snapshot


def route_to(monkeypatch, transport):
    original_client = httpx.AsyncClient

    def client(**kwargs):
        kwargs['transport'] = transport
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)


def attempt(store, job):
    with store.configuration.engine.connect() as connection:
        return connection.execute(sa.select(store.attempts).where(
            store.attempts.c.job_id == job)).mappings().one()


@pytest.mark.asyncio
async def test_worker_sends_once_and_polling_reaches_success(installation, tmp_path, fake, monkeypatch):
    configuration, store, _ = installation
    job, snapshot = humblefax_job(installation, tmp_path)
    configuration.apply(snapshot, snapshot.active.values, actor='test', restart_required=False,
        providers={'outbound': ProviderConfiguration('humblefax',
            credentials={'access_key': 'replacement-access', 'secret_key': 'replacement-secret'})})
    route_to(monkeypatch, LoopbackTransport(fake.port))
    fake.reply(200, created(fax_id=987654))
    worker = OutboundWorker(store, CapturedTransport(store, Runtime()))
    await worker.step()
    assert store.get(job)['state'] == 'in_progress'
    assert attempt(store, job)['provider_sid'] == '987654'
    [create] = fake.requests
    assert create['headers']['authorization'] == BASIC
    body = json.loads(multipart(create)['jsonData'].get_payload(decode=True))
    assert body['recipients'] == [12025550123]
    assert body['uuid'] == attempt(store, job)['id']
    fake.reply(200, sent(fax_id='987654', status='success'))
    await OutboundPoller(store).refresh(job)
    assert store.get(job)['state'] == 'success'
    await worker.step()
    assert [(request['method'], request['path']) for request in fake.requests] == [
        ('POST', '/quickSendFax'), ('GET', '/sentFax/987654')]
    assert fake.requests[1]['headers']['authorization'] == BASIC


@pytest.mark.asyncio
async def test_timeout_requires_reconciliation_then_bound_identity_polls_original_account(
        installation, tmp_path, fake, monkeypatch):
    _, store, _ = installation
    job, _ = humblefax_job(installation, tmp_path)
    route_to(monkeypatch, LoopbackTransport(fake.port, read_timeout=0.2))
    fake.reply(200, created(), delay=1.0)
    worker = OutboundWorker(store, CapturedTransport(store, Runtime()))
    await worker.step()
    assert store.get(job)['state'] == 'reconciliation_required'
    assert attempt(store, job)['error_category'] == 'transport_ambiguous'
    await worker.step()
    assert len(fake.requests) == 1
    view = store.operator_view(job)
    assert view['can_bind_provider_identity'], view['bind_refusal_reason']
    store.bind_provider_identity(job, expected_version=view['version'], provider_sid='123456', actor='admin')
    route_to(monkeypatch, LoopbackTransport(fake.port))
    fake.reply(200, sent(status='success'))
    await OutboundPoller(store).refresh(job)
    assert store.get(job)['state'] == 'success'
    assert [(request['method'], request['path']) for request in fake.requests] == [
        ('POST', '/quickSendFax'), ('GET', '/sentFax/123456')]


@pytest.mark.asyncio
async def test_rejected_credentials_never_resubmit_through_the_worker(installation, tmp_path, fake, monkeypatch):
    _, store, _ = installation
    job, _ = humblefax_job(installation, tmp_path)
    route_to(monkeypatch, LoopbackTransport(fake.port))
    fake.reply(401, LOGIN_FAILURE)
    worker = OutboundWorker(store, CapturedTransport(store, Runtime()))
    await worker.step()
    await worker.step()
    assert store.get(job)['state'] == 'reconciliation_required'
    assert len(fake.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(('credentials', 'to_number', 'category'), [
    ({}, DESTINATION, 'provider_unavailable'),
    ({'access_key': ACCESS, 'secret_key': ''}, DESTINATION, 'provider_unavailable'),
    (None, '+442071234567', 'preparation_failed'),
])
async def test_unready_account_or_unsupported_destination_fails_before_submission(
        installation, tmp_path, fake, monkeypatch, credentials, to_number, category):
    _, store, _ = installation
    job, _ = humblefax_job(installation, tmp_path, credentials=credentials, to_number=to_number)
    route_to(monkeypatch, LoopbackTransport(fake.port))
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    assert store.get(job)['state'] == 'failed'
    record = attempt(store, job)
    assert record['error_category'] == category and record['submitted_at'] is None
    assert fake.requests == []
