"""eFax Enterprise API adapter against a fake built from eFax's published request and reply shapes.

The fake answers the documented calls (POST /tokens, POST /faxes,
GET /faxes/{id}/transmission-details, POST /faxes/{id}/cancel, GET /health)
with synthetic values. The production host stays fixed; only the transport
changes. Nothing here contacts eFax.
"""
import asyncio
import base64
from dataclasses import FrozenInstanceError
from datetime import datetime
import json
import sys
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from api.app import efax_service
from api.app.config_profiles import ProviderConfiguration, ProviderProfile
from api.app.config_values import ConfigurationValueError, ConfigurationValues
from api.app.efax_service import (
    EfaxBusy, EfaxCredentialsError, EfaxError, EfaxFaxService, EfaxNotFound, efax_destination,
    transmission_status,
)
from api.app.outbound_polling import OutboundPoller
from api.app.outbound_transport import CapturedTransport
from api.app.outbound_worker import OutboundWorker
from api.tests.test_outbound_store import installation, accept  # noqa: F401 - fixture
from api.tests.test_outbound_transport import Runtime
from api.tests.test_schema import database  # noqa: F401 - fixture


APP_ID = 'synthetic-app-id-0001'
API_KEY = 'synthetic-api-key+/=='
USER_ID = 'synthetic-user-0001'
BASIC = 'Basic ' + base64.b64encode((APP_ID + ':' + API_KEY).encode()).decode()
DESTINATION = '+12025550123'
PDF = b'%PDF-1.4\nsynthetic fax document\n%%EOF\n'
FAX_ID = '7e029808-237b-413e-a051-53bf445e8b49'
OTHER_ID = '057be5c6-c358-4b37-bd46-c2a5c33b9f89'
PRIVATE = 'synthetic-private-provider-detail'


@pytest.fixture(autouse=True)
def fresh_tokens():
    for name in ('api.app.efax_service', 'app.efax_service'):
        module = sys.modules.get(name)
        if module is not None:
            module.clear_tokens()
    yield
    for name in ('api.app.efax_service', 'app.efax_service'):
        module = sys.modules.get(name)
        if module is not None:
            module.clear_tokens()


class FakeEfax:
    """Scripted eFax API: tokens are issued on request, other calls answer in order."""

    def __init__(self):
        self.requests, self.replies, self.issued = [], [], 0
        self.token_replies = []

    def reply(self, status, payload=None, *, raw=None, headers=None, error=None):
        self.replies.append((status, payload, raw, headers or {}, error))

    def token_reply(self, status, payload=None):
        self.token_replies.append((status, payload))

    def handler(self, request):
        body = request.read()
        self.requests.append({'method': request.method, 'path': request.url.path, 'query': request.url.query,
                              'host': request.url.host, 'scheme': request.url.scheme,
                              'headers': {k.lower(): v for k, v in request.headers.items()}, 'body': body})
        if request.url.path == '/tokens':
            if self.token_replies:
                status, payload = self.token_replies.pop(0)
                return httpx.Response(status, json=payload)
            self.issued += 1
            return httpx.Response(200, json={'access_token': f'synthetic-token-{self.issued}',
                                             'token_type': 'bearer', 'expires_in': 86399, 'scope': 'read write'})
        if not self.replies:
            return httpx.Response(599, json={'error': 'unscripted'})
        status, payload, raw, headers, error = self.replies.pop(0)
        if error is not None:
            raise error
        if raw is not None:
            return httpx.Response(status, content=raw, headers=headers)
        return httpx.Response(status, json=payload, headers=headers)

    def transport(self):
        return httpx.MockTransport(self.handler)

    def calls(self):
        return [(request['method'], request['path']) for request in self.requests]


@pytest.fixture
def fake():
    return FakeEfax()


@pytest.fixture
def document(tmp_path):
    path = tmp_path / 'private-original-name.pdf'
    path.write_bytes(PDF)
    return str(path)


def service(fake, **overrides):
    values = {'app_id': APP_ID, 'api_key': API_KEY, 'user_id': USER_ID, **overrides}
    return EfaxFaxService(**values, transport=fake.transport())


def created(fax_id=FAX_ID, number='12025550123'):
    return [{'fax_id': fax_id, 'destination_fax_number': number}]


def details(status='COMPLETE', fax_id=FAX_ID):
    return {'fax_id': fax_id, 'direction': 'OUTBOUND', 'originating_fax_number': '13235552222',
            'destination_fax_number': '12025550123', 'transmission_data': {
                'transmission_status': status, 'error_code': 'SFAX', 'error_message': PRIVATE}}


def assert_safe(error, *values):
    for value in (APP_ID, API_KEY, USER_ID, PRIVATE, 'synthetic-token', *values):
        assert value not in str(error)


# Settings, redaction and captured profile wiring

def test_settings_read_the_documented_variables_and_hide_all_three_credentials():
    from api.app.config_store import ConfigurationRevision, ConfigurationSnapshot
    from api.app.config_views import project_admin_settings
    values = ConfigurationValues.from_environment({
        'EFAX_APP_ID': APP_ID, 'EFAX_API_KEY': API_KEY, 'EFAX_USER_ID': USER_ID,
        'EFAX_CALLER_ID': '+13235551212', 'EFAX_CSID': 'Faxbot Clinic', 'EFAX_POLL_SECONDS': '120',
        'EFAX_DELETE_AFTER_DOWNLOAD': 'true'})
    assert (values.efax_app_id, values.efax_api_key, values.efax_user_id) == (APP_ID, API_KEY, USER_ID)
    for secret in (APP_ID, API_KEY, USER_ID):
        assert secret not in repr(values)
    redacted = values.to_environment(redact_secrets=True)
    assert redacted['EFAX_APP_ID'] == redacted['EFAX_API_KEY'] == redacted['EFAX_USER_ID'] == '***'
    assert redacted['EFAX_CALLER_ID'] == '+13235551212' and redacted['EFAX_CSID'] == 'Faxbot Clinic'
    assert set(ConfigurationValues.environment_credentials({
        'EFAX_APP_ID': APP_ID, 'EFAX_API_KEY': API_KEY, 'EFAX_USER_ID': USER_ID})) == {
        'efax_app_id', 'efax_api_key', 'efax_user_id'}
    view = project_admin_settings(ConfigurationSnapshot('installation', 1,
        ConfigurationRevision('active', values), None))
    assert view['efax'] == {'app_id': '***', 'api_key': '***', 'user_id': '***', 'caller_id': '+13235551212',
                            'csid': 'Faxbot Clinic', 'poll_seconds': 120, 'delete_after_download': True,
                            'configured': True}
    for secret in (APP_ID, API_KEY, USER_ID):
        assert secret not in json.dumps(view)
    empty = project_admin_settings(ConfigurationSnapshot('installation', 1,
        ConfigurationRevision('active', ConfigurationValues.from_environment({})), None))
    assert empty['efax'] == {'app_id': '', 'api_key': '', 'user_id': '', 'caller_id': '', 'csid': '',
                             'poll_seconds': 60, 'delete_after_download': False, 'configured': False}


def test_caller_id_entered_nationally_is_saved_in_international_form():
    values = ConfigurationValues.from_environment({'FAX_DEFAULT_COUNTRY': 'GB'})
    assert values.with_patch({'efax_caller_id': '020 7946 0018'}).efax_caller_id == '+442079460018'
    assert values.with_patch({'efax_caller_id': ''}).efax_caller_id == ''


@pytest.mark.parametrize(('variable', 'value'), [
    ('EFAX_CALLER_ID', '3235551212x'), ('EFAX_CSID', 'x' * 21), ('EFAX_CSID', 'line\nbreak'),
    ('EFAX_POLL_SECONDS', '10'), ('EFAX_POLL_SECONDS', '7200'), ('EFAX_APP_ID', 'has:colon'),
    ('EFAX_API_KEY', 'has space'), ('EFAX_USER_ID', 'é')])
def test_unusable_efax_settings_are_refused_without_echoing_them(variable, value):
    with pytest.raises(ConfigurationValueError) as failure:
        ConfigurationValues.from_environment({variable: value})
    assert [issue['field'] for issue in failure.value.issues] == [variable]
    assert value not in str(failure.value)


def test_masked_efax_key_cannot_replace_the_stored_key():
    values = ConfigurationValues.from_environment({'EFAX_API_KEY': API_KEY})
    with pytest.raises(ConfigurationValueError) as failure:
        values.with_patch({'efax_api_key': '***'})
    assert failure.value.issues == ({'field': 'EFAX_API_KEY', 'reason': 'masked_secret'},)


@pytest.mark.parametrize(('environment', 'configured'), [
    ({'EFAX_APP_ID': APP_ID, 'EFAX_API_KEY': API_KEY, 'EFAX_USER_ID': USER_ID, 'EFAX_CALLER_ID': '+13235551212',
      'EFAX_CSID': 'Front desk'}, True),
    ({'EFAX_APP_ID': APP_ID, 'EFAX_API_KEY': API_KEY}, False),
    ({}, False),
])
def test_selected_provider_compiles_a_captured_profile(tmp_path, monkeypatch, environment, configured):
    from api.app.config_activation import compile_profiles
    from api.app.config_bootstrap import default_plugin_state
    from api.app.config_paths import provider_traits_path
    from api.app.provider_catalog import ProviderCatalog
    from api.app.provider_execution import service_from_profile
    values = ConfigurationValues.from_environment({'FAX_OUTBOUND_BACKEND': 'efax', **environment})
    catalog = ProviderCatalog.load(provider_traits_path(), tmp_path / 'providers')
    profile = compile_profiles(values, catalog, default_plugin_state(values))['outbound']
    assert profile.provider_id == 'efax' and profile.manifest is None
    assert set(profile.credentials) == {'app_id', 'api_key', 'user_id'}
    assert profile.traits['requires_tiff'] is False
    monkeypatch.setenv('EFAX_API_KEY', 'unrelated-current-key')
    adapter = service_from_profile(ProviderProfile('profile', 'account', profile))
    assert isinstance(adapter, EfaxFaxService)
    assert adapter.is_configured() is configured
    assert adapter.api_key == environment.get('EFAX_API_KEY', '')
    assert adapter.caller_id == environment.get('EFAX_CALLER_ID', '')
    assert adapter.csid == environment.get('EFAX_CSID', '')


# Tokens

def test_token_uses_basic_sign_in_and_is_shared_until_it_runs_out(fake, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(efax_service, '_clock', lambda: now[0])
    fake.reply(200, details('INPROGRESS'))
    fake.reply(200, details('INPROGRESS'))
    fake.reply(200, details('COMPLETE'))
    adapter = service(fake)
    asyncio.run(adapter.get_fax_status(FAX_ID))
    asyncio.run(service(fake).get_fax_status(FAX_ID))
    assert fake.calls() == [('POST', '/tokens'), ('GET', f'/faxes/{FAX_ID}/transmission-details'),
                            ('GET', f'/faxes/{FAX_ID}/transmission-details')]
    token = fake.requests[0]
    assert token['headers']['authorization'] == BASIC
    assert token['headers']['content-type'].startswith('application/x-www-form-urlencoded')
    assert parse_qs(token['body'].decode()) == {'grant_type': ['client_credentials']}
    for call in fake.requests[1:]:
        assert call['headers']['authorization'] == 'Bearer synthetic-token-1'
        assert call['headers']['user-id'] == USER_ID
    now[0] += 86399 - 299  # inside the five-minute margin before eFax's 24 hours end
    asyncio.run(adapter.get_fax_status(FAX_ID))
    assert fake.calls()[-2:] == [('POST', '/tokens'), ('GET', f'/faxes/{FAX_ID}/transmission-details')]
    assert fake.requests[-1]['headers']['authorization'] == 'Bearer synthetic-token-2'


def test_refused_token_is_replaced_and_the_call_repeated_exactly_once(fake):
    fake.reply(401, {'errors': [{'error_code': 'UNAUTHORIZED'}]})
    fake.reply(200, details('COMPLETE'))
    assert asyncio.run(service(fake).get_fax_status(FAX_ID)) == {'provider_sid': FAX_ID, 'status': 'success'}
    assert fake.calls() == [('POST', '/tokens'), ('GET', f'/faxes/{FAX_ID}/transmission-details'),
                            ('POST', '/tokens'), ('GET', f'/faxes/{FAX_ID}/transmission-details')]
    assert fake.requests[3]['headers']['authorization'] == 'Bearer synthetic-token-2'


def test_a_second_refusal_is_a_credentials_error_without_more_calls(fake, document):
    fake.reply(401, {})
    fake.reply(401, {})
    with pytest.raises(EfaxCredentialsError) as failure:
        asyncio.run(service(fake).send_fax_file(DESTINATION, document))
    assert str(failure.value) == 'eFax refused the app ID, API key or user ID.'
    assert fake.calls() == [('POST', '/tokens'), ('POST', '/faxes'), ('POST', '/tokens'), ('POST', '/faxes')]
    assert_safe(failure.value)


@pytest.mark.parametrize('status', [400, 401, 403])
def test_refused_sign_in_never_reaches_the_fax_api(fake, document, status):
    fake.token_reply(status, {'error': PRIVATE})
    with pytest.raises(EfaxCredentialsError) as failure:
        asyncio.run(service(fake).send_fax_file(DESTINATION, document))
    assert fake.calls() == [('POST', '/tokens')]
    assert_safe(failure.value)


@pytest.mark.parametrize('payload', [{'token_type': 'bearer'}, {'access_token': ''}, {'access_token': 'a b'}, [PRIVATE]])
def test_unusable_token_reply_is_a_plain_failure(fake, payload):
    fake.token_reply(200, payload)
    with pytest.raises(EfaxError, match='eFax did not issue a sign-in token') as failure:
        asyncio.run(service(fake).authenticate())
    assert_safe(failure.value)


def test_busy_sign_in_says_when_to_ask_again(fake):
    fake.token_replies.append((429, None))
    with pytest.raises(EfaxBusy):
        asyncio.run(service(fake).authenticate())


def test_health_needs_no_sign_in_and_never_replaces_the_token(fake):
    fake.reply(200, {'status': 'UP'})
    fake.reply(200, {'status': 'DOWN'})
    adapter = EfaxFaxService('', '', '', transport=fake.transport())
    assert asyncio.run(adapter.health()) is True
    assert asyncio.run(adapter.health()) is False
    assert fake.calls() == [('GET', '/health'), ('GET', '/health')]
    assert 'authorization' not in fake.requests[0]['headers'] and 'user-id' not in fake.requests[0]['headers']


# Sending

@pytest.mark.parametrize(('caller_id', 'csid', 'options'), [
    ('', '', {'image_resolution': 'FINE', 'include_cover_page': False}),
    ('+13235551212', 'Front desk', {'image_resolution': 'FINE', 'include_cover_page': False,
                                    'custom_CallerID': '13235551212', 'custom_CSID': 'Front desk'}),
    ('+61291234567', '', {'image_resolution': 'FINE', 'include_cover_page': False,
                          'custom_CallerID': '61291234567'}),
])
def test_send_is_one_documented_post_with_one_destination_and_the_pdf(fake, document, caller_id, csid, options):
    fake.reply(201, created())
    reference = uuid4().hex
    result = asyncio.run(service(fake, caller_id=caller_id, csid=csid).send_fax_file(
        DESTINATION, document, reference=reference))
    assert result == {'provider_sid': FAX_ID, 'status': 'in_progress'}
    assert fake.calls() == [('POST', '/tokens'), ('POST', '/faxes')]
    request = fake.requests[1]
    assert request['scheme'] == 'https' and request['host'] == 'api.securedocex.com' and not request['query']
    assert request['headers']['authorization'] == 'Bearer synthetic-token-1'
    assert request['headers']['user-id'] == USER_ID
    assert request['headers']['content-type'] == 'application/json'
    assert request['headers']['transaction-id'] == reference
    body = json.loads(request['body'])
    assert body == {'destinations': [{'fax_number': '12025550123'}],
                    'documents': [{'document_type': 'PDF', 'document_content': base64.b64encode(PDF).decode()}],
                    'fax_options': options, 'client_data': {'client_reference_id': reference}}
    assert b'private-original-name' not in request['body']


@pytest.mark.parametrize(('destination', 'dialable'), [
    ('+12025550123', '12025550123'), ('+16135550123', '16135550123'), ('+442079460018', '+442079460018'),
    ('+61291234567', '+61291234567')])
def test_destinations_use_efax_dialable_form(destination, dialable):
    assert efax_destination(destination) == dialable


@pytest.mark.parametrize('destination', ['', None, 'not a number', '+1202555012a', 12025550123])
def test_unusable_destination_is_refused_before_http(fake, document, destination):
    with pytest.raises(ValueError, match='eFax fax number is invalid'):
        asyncio.run(service(fake).send_fax_file(destination, document))
    assert fake.requests == []


@pytest.mark.parametrize('payload', [created(), created(number='2025550123'), [{'fax_id': FAX_ID.upper()}]])
def test_documented_create_variants_yield_the_fax_id(fake, document, payload):
    fake.reply(201, payload)
    assert asyncio.run(service(fake).send_fax_file(DESTINATION, document)) == {
        'provider_sid': FAX_ID, 'status': 'in_progress'}


@pytest.mark.parametrize(('status', 'payload', 'raw'), [
    (400, {'errors': [{'user_message': PRIVATE}]}, None),
    (403, {'errors': [{'developer_message': PRIVATE}]}, None),
    (429, None, b''),
    (500, {'errors': [{'developer_message': PRIVATE}]}, None),
    (302, None, b''),
    (201, None, PRIVATE.encode()),
    (201, [], None),
    (201, created() + created(OTHER_ID), None),
    (201, [{'fax_id': 'not-a-fax-id'}], None),
    (201, [{'fax_id': '../' + FAX_ID}], None),
    (201, created(number='14155550100'), None),
    (201, {'fax_id': FAX_ID}, None),
])
def test_unusable_create_reply_is_sanitized_and_never_repeated(fake, document, status, payload, raw):
    fake.reply(status, payload, raw=raw, headers={'Location': 'https://elsewhere.invalid/'} if status == 302 else None)
    with pytest.raises(EfaxError) as failure:
        asyncio.run(service(fake).send_fax_file(DESTINATION, document))
    assert str(failure.value) == 'eFax create request failed.'
    assert failure.value.__suppress_context__
    assert fake.calls().count(('POST', '/faxes')) == 1
    assert_safe(failure.value, document)


def test_create_timeout_is_ambiguous_and_never_resubmitted(fake, document):
    fake.reply(0, error=httpx.ReadTimeout('synthetic timeout'))
    with pytest.raises(EfaxError, match='eFax create request failed'):
        asyncio.run(service(fake).send_fax_file(DESTINATION, document))
    assert fake.calls() == [('POST', '/tokens'), ('POST', '/faxes')]


# Status and cancelling

@pytest.mark.parametrize(('wire', 'expected'), [
    ('NEW', 'in_progress'), ('INPROGRESS', 'in_progress'), ('IN PROGRESS', 'in_progress'),
    ('INPROGESS', 'in_progress'), ('PENDING RETRY', 'in_progress'), ('COMPLETE', 'success'),
    ('COMPLETED', 'success'), ('ERROR', 'failed'), ('CANCELED', 'cancelled'), ('cancelled', 'cancelled')])
def test_status_maps_documented_transmission_states(fake, wire, expected):
    fake.reply(200, details(wire))
    assert asyncio.run(service(fake).get_fax_status(FAX_ID)) == {'provider_sid': FAX_ID, 'status': expected}
    assert transmission_status(wire) == expected


@pytest.mark.parametrize(('status', 'payload'), [
    (200, details('UNKNOWN')), (200, details(None)), (200, details('COMPLETE', fax_id=OTHER_ID)),
    (200, {'fax_id': FAX_ID}), (404, {'errors': [{'error_code': 'NOT_FOUND'}]}), (500, {}),
    (200, [PRIVATE])])
def test_unknown_or_missing_status_is_never_a_result(fake, status, payload):
    fake.reply(status, payload)
    with pytest.raises(EfaxError, match='eFax status request failed') as failure:
        asyncio.run(service(fake).get_fax_status(FAX_ID))
    assert_safe(failure.value)


@pytest.mark.parametrize('sid', [None, '', 123, 'abc', FAX_ID + '/cancel', '../' + FAX_ID])
def test_status_identity_is_refused_before_http(fake, sid):
    with pytest.raises(ValueError, match='eFax provider identity is invalid'):
        asyncio.run(service(fake).get_fax_status(sid))
    assert fake.requests == []


def test_cancel_is_a_request_and_a_finished_fax_says_so(fake):
    fake.reply(202, None, raw=b'')
    fake.reply(409, {'errors': [{'developer_message': 'fax already completed messaging'}]})
    fake.reply(404, {})
    adapter = service(fake)
    assert asyncio.run(adapter.cancel_fax(FAX_ID)) == 'requested'
    assert asyncio.run(adapter.cancel_fax(FAX_ID)) == 'finished'
    with pytest.raises(EfaxNotFound):
        asyncio.run(adapter.cancel_fax(FAX_ID))
    assert fake.calls()[1:] == [('POST', f'/faxes/{FAX_ID}/cancel')] * 3
    assert fake.requests[1]['headers']['user-id'] == USER_ID and not fake.requests[1]['body']


# The adapter itself

@pytest.mark.parametrize(('app_id', 'api_key', 'user_id', 'caller_id', 'csid'), [
    ('app:id', API_KEY, USER_ID, '', ''), (' ' + APP_ID, API_KEY, USER_ID, '', ''),
    (APP_ID, 'bad\r\nInjected: value', USER_ID, '', ''), (APP_ID, API_KEY, 'user\nid', '', ''),
    (None, API_KEY, USER_ID, '', ''), (APP_ID, API_KEY, USER_ID, '12345', ''),
    (APP_ID, API_KEY, USER_ID, '', 'x' * 21), (APP_ID, API_KEY, USER_ID, None, '')])
def test_invalid_captured_configuration_is_refused_before_io(app_id, api_key, user_id, caller_id, csid):
    with pytest.raises(ValueError, match='eFax configuration is invalid') as failure:
        EfaxFaxService(app_id, api_key, user_id, caller_id, csid)
    assert_safe(failure.value)


def test_unconfigured_account_never_contacts_efax(fake, document):
    adapter = EfaxFaxService(APP_ID, '', USER_ID, transport=fake.transport())
    assert not adapter.is_configured()
    for call in (adapter.send_fax_file(DESTINATION, document), adapter.get_fax_status(FAX_ID),
                 adapter.cancel_fax(FAX_ID), adapter.authenticate()):
        with pytest.raises(ValueError, match='eFax is not configured'):
            asyncio.run(call)
    assert fake.requests == []


def test_captured_account_is_frozen_and_credentials_are_not_in_repr():
    adapter = EfaxFaxService(APP_ID, API_KEY, USER_ID, '+13235551212')
    for secret in (APP_ID, API_KEY, USER_ID):
        assert secret not in repr(adapter)
    with pytest.raises(FrozenInstanceError):
        adapter.api_key = 'replacement'


def test_http_client_ignores_environment_proxies_and_redirects(fake, document, monkeypatch):
    actual_client, policies = httpx.AsyncClient, []

    def client(**kwargs):
        policies.append(kwargs)
        return actual_client(**kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    fake.reply(201, created())
    fake.reply(200, details())
    adapter = service(fake)
    asyncio.run(adapter.send_fax_file(DESTINATION, document))
    asyncio.run(adapter.get_fax_status(FAX_ID))
    assert len(policies) == 3
    for policy in policies:
        assert policy['trust_env'] is False and policy['follow_redirects'] is False
        assert 0 < policy['timeout'].connect <= 10 and 0 < policy['timeout'].read <= 60


# Captured delivery through the worker and poller

def efax_job(installation, tmp_path, *, credentials=None):
    configuration, store, snapshot = installation
    profile = ProviderConfiguration('efax',
        credentials={'app_id': APP_ID, 'api_key': API_KEY, 'user_id': USER_ID} if credentials is None else credentials,
        settings={'caller_id': '', 'csid': ''})
    snapshot = configuration.apply(snapshot, snapshot.active.values.with_patch({'fax_data_dir': str(tmp_path)}),
        actor='test', restart_required=False, providers={'outbound': profile})
    job = accept((configuration, store, snapshot))
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
    _, store, _ = installation
    job, _ = efax_job(installation, tmp_path)
    route_to(monkeypatch, fake.transport())
    fake.reply(201, created())
    worker = OutboundWorker(store, CapturedTransport(store, Runtime()))
    await worker.step()
    assert store.get(job)['state'] == 'in_progress'
    record = attempt(store, job)
    assert record['provider_sid'] == FAX_ID
    body = json.loads(fake.requests[1]['body'])
    assert body['client_data'] == {'client_reference_id': record['id']}
    assert body['destinations'] == [{'fax_number': '12025550123'}]
    fake.reply(200, details('INPROGRESS'))
    await OutboundPoller(store).refresh(job)
    assert store.get(job)['state'] == 'in_progress'
    fake.reply(200, details('COMPLETE'))
    await OutboundPoller(store).refresh(job)
    assert store.get(job)['state'] == 'success'
    await worker.step()
    assert fake.calls() == [('POST', '/tokens'), ('POST', '/faxes'),
                            ('GET', f'/faxes/{FAX_ID}/transmission-details'),
                            ('GET', f'/faxes/{FAX_ID}/transmission-details')]


@pytest.mark.asyncio
async def test_failed_transmission_is_a_failed_fax(installation, tmp_path, fake, monkeypatch):
    _, store, _ = installation
    job, _ = efax_job(installation, tmp_path)
    route_to(monkeypatch, fake.transport())
    fake.reply(201, created())
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    fake.reply(200, details('ERROR'))
    await OutboundPoller(store).refresh(job)
    assert store.get(job)['state'] == 'failed'


@pytest.mark.asyncio
async def test_refused_sign_in_fails_the_fax_before_anything_is_sent(installation, tmp_path, fake, monkeypatch):
    _, store, _ = installation
    job, _ = efax_job(installation, tmp_path)
    route_to(monkeypatch, fake.transport())
    fake.token_reply(401, {'error': PRIVATE})
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    assert store.get(job)['state'] == 'failed'
    record = attempt(store, job)
    assert record['error_category'] == 'provider_unavailable' and record['submitted_at'] is None
    assert fake.calls() == [('POST', '/tokens')]


@pytest.mark.asyncio
async def test_timeout_requires_confirmation_and_a_bound_id_polls_the_original_account(
        installation, tmp_path, fake, monkeypatch):
    _, store, _ = installation
    job, _ = efax_job(installation, tmp_path)
    route_to(monkeypatch, fake.transport())
    fake.reply(0, error=httpx.ReadTimeout('synthetic timeout'))
    worker = OutboundWorker(store, CapturedTransport(store, Runtime()))
    await worker.step()
    assert store.get(job)['state'] == 'reconciliation_required'
    await worker.step()
    assert fake.calls().count(('POST', '/faxes')) == 1
    view = store.operator_view(job)
    assert view['can_bind_provider_identity'], view['bind_refusal_reason']
    with pytest.raises(ValueError):
        store.bind_provider_identity(job, expected_version=view['version'], provider_sid='not-an-efax-id',
                                     actor='admin')
    store.bind_provider_identity(job, expected_version=view['version'], provider_sid=FAX_ID.upper(), actor='admin')
    assert attempt(store, job)['provider_sid'] == FAX_ID
    fake.reply(200, details('COMPLETE'))
    await OutboundPoller(store).refresh(job)
    assert store.get(job)['state'] == 'success'
    assert fake.calls()[-1] == ('GET', f'/faxes/{FAX_ID}/transmission-details')


@pytest.mark.asyncio
async def test_unready_account_fails_before_submission(installation, tmp_path, fake, monkeypatch):
    _, store, _ = installation
    job, _ = efax_job(installation, tmp_path, credentials={'app_id': APP_ID, 'api_key': '', 'user_id': USER_ID})
    route_to(monkeypatch, fake.transport())
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    assert store.get(job)['state'] == 'failed'
    assert attempt(store, job)['error_category'] == 'provider_unavailable'
    assert fake.requests == []
