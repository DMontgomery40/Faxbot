"""Captured provider frames construct isolated services without account fallback."""
import json

import httpx
import pytest

from api.app.config_profiles import ProviderConfiguration, ProviderProfile
from api.app.phaxio_service import PhaxioFaxService
from api.app.signalwire_service import SignalWireFaxService
from api.app.sinch_service import SinchFaxService


def profile(identity, *, credentials=None, settings=None, manifest=None, revision='original'):
    return ProviderProfile(revision, 'account-' + revision, ProviderConfiguration(identity,
        credentials=credentials, settings=settings, manifest=manifest))


def service_from_profile(bound):
    from api.app.provider_execution import service_from_profile as construct
    return construct(bound)


@pytest.mark.parametrize('identity, credentials, settings, expected_type, expected_fields', [
    ('phaxio', {'api_key': 'original-key', 'api_secret': 'original-secret'},
     {'callback_url': 'https://original.invalid/callback'}, PhaxioFaxService,
     {'api_key': 'original-key', 'api_secret': 'original-secret',
      'status_callback_url': 'https://original.invalid/callback'}),
    ('sinch', {'api_key': 'original-key', 'api_secret': 'original-secret'},
     {'project_id': 'original-project', 'base_url': 'https://original.invalid/v3'}, SinchFaxService,
     {'api_key': 'original-key', 'api_secret': 'original-secret', 'project_id': 'original-project',
      'base_url': 'https://original.invalid/v3'}),
    ('signalwire', {'api_token': 'original-token'},
     {'space_url': 'original.signalwire.invalid', 'project_id': 'original-project',
      'fax_from_e164': '+15551230001', 'callback_url': 'https://original.invalid/callback'}, SignalWireFaxService,
     {'api_token': 'original-token', 'space_url': 'original.signalwire.invalid',
      'project_id': 'original-project', 'from_number': '+15551230001',
      'status_callback_url': 'https://original.invalid/callback'}),
])
def test_builtin_service_uses_captured_frame_after_input_and_environment_change(
        monkeypatch, identity, credentials, settings, expected_type, expected_fields):
    bound = profile(identity, credentials=credentials, settings=settings)
    credentials.update({name: 'replacement-secret' for name in credentials})
    settings.update({name: 'replacement-setting' for name in settings})
    monkeypatch.setenv('PHAXIO_API_KEY', 'environment-key')
    monkeypatch.setenv('SINCH_BASE_URL', 'https://environment.invalid/v3')
    monkeypatch.setenv('SIGNALWIRE_API_TOKEN', 'environment-token')
    first = service_from_profile(bound)
    assert isinstance(first, expected_type)
    assert first.is_configured()
    assert {name: getattr(first, name) for name in expected_fields} == expected_fields
    for name in expected_fields:
        setattr(first, name, 'mutated-service')
    rebuilt = service_from_profile(bound)
    assert rebuilt is not first
    assert {name: getattr(rebuilt, name) for name in expected_fields} == expected_fields


@pytest.mark.parametrize('identity', ['phaxio', 'sinch', 'signalwire', 'documo'])
def test_missing_captured_credentials_never_borrow_current_environment(monkeypatch, identity):
    for name in ('PHAXIO_API_KEY', 'PHAXIO_API_SECRET', 'SINCH_API_KEY', 'SINCH_API_SECRET',
                 'SINCH_PROJECT_ID', 'SIGNALWIRE_API_TOKEN', 'SIGNALWIRE_SPACE_URL', 'SIGNALWIRE_PROJECT_ID',
                 'DOCUMO_API_KEY', 'DOCUMO_BASE_URL', 'DOCUMO_SANDBOX'):
        monkeypatch.setenv(name, 'current-environment-value')
    assert not service_from_profile(profile(identity)).is_configured()


def manifest(identity, endpoint):
    return {'id': identity, 'auth': {'scheme': 'api_key_header'}, 'allowed_domains': ['provider.invalid'],
        'actions': {'send_fax': {'method': 'POST', 'url': endpoint,
            'body': {'kind': 'json', 'template': '{"tag":"{{settings.tag}}","to":"{{to}}"}'}}}}


def intercept_http(monkeypatch, handler):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', ['custom', 'phaxio', 'sip', 'freeswitch', 'documo'])
async def test_manifest_runtime_keeps_original_endpoint_credentials_and_settings(monkeypatch, identity):
    from api.app.plugins.http_provider import HttpProviderRuntime
    original_manifest = manifest(identity, 'https://provider.invalid/original')
    original = profile(identity, credentials={'api_key': 'original-key'}, settings={'tag': 'original'},
                       manifest=original_manifest)
    original_manifest['actions']['send_fax']['url'] = 'https://provider.invalid/edited-install'
    replacement = profile(identity, credentials={'api_key': 'replacement-key'}, settings={'tag': 'replacement'},
        manifest=manifest(identity, 'https://provider.invalid/replacement'), revision='replacement')
    requests = []
    def handle(request):
        requests.append((str(request.url), request.headers['X-API-Key'], json.loads(request.content)))
        return httpx.Response(200, json={'id': 'provider-job', 'status': 'queued'})
    intercept_http(monkeypatch, handle)
    first = service_from_profile(original)
    assert isinstance(first, HttpProviderRuntime)
    assert (await first.send_fax(to='+15551230001'))['job_id'] == 'provider-job'
    first.creds['api_key'] = 'mutated-runtime-key'
    first.settings['tag'] = 'mutated-runtime-tag'
    first.m.actions['send_fax'].url = 'https://provider.invalid/mutated-runtime'
    for bound in (replacement, original):
        assert (await service_from_profile(bound).send_fax(to='+15551230001'))['job_id'] == 'provider-job'
    assert [(url, key, data['tag']) for url, key, data in requests] == [
        ('https://provider.invalid/original', 'original-key', 'original'),
        ('https://provider.invalid/replacement', 'replacement-key', 'replacement'),
        ('https://provider.invalid/original', 'original-key', 'original'),
    ]


@pytest.mark.parametrize('identity, captured_manifest', [
    ('sip', None), ('freeswitch', None), ('unknown', None),
])
def test_unsupported_profiles_fail_explicitly_without_telephony_fallback(identity, captured_manifest):
    from api.app.provider_execution import UnsupportedProviderExecutionError
    with pytest.raises(UnsupportedProviderExecutionError):
        service_from_profile(profile(identity, credentials={'api_key': 'private-synthetic-key'},
                                     manifest=captured_manifest))


def test_invalid_captured_manifest_error_is_sanitized():
    from api.app.provider_execution import ProviderExecutionError
    malformed = manifest('custom', 'https://provider.invalid/fax')
    malformed['timeout_ms'] = 'private-synthetic-secret'
    with pytest.raises(ProviderExecutionError) as failure:
        service_from_profile(profile('custom', manifest=malformed))
    assert 'private-synthetic-secret' not in str(failure.value)
    assert failure.value.__suppress_context__


def test_sinch_empty_captured_endpoint_does_not_read_environment(monkeypatch):
    monkeypatch.setenv('SINCH_BASE_URL', 'https://changed-account.invalid/v3')
    service = SinchFaxService('project', 'key', 'secret', base_url='')
    assert service.base_url == SinchFaxService.DEFAULT_BASES[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_kind', ['http', 'transport', 'invalid-response'])
async def test_sinch_upload_failure_stays_at_captured_endpoint_and_sanitizes_error(
        monkeypatch, tmp_path, failure_kind):
    path = tmp_path / 'synthetic.pdf'
    path.write_bytes(b'%PDF-synthetic-test')
    calls = []
    def handle(request):
        calls.append(str(request.url))
        if failure_kind == 'transport':
            raise httpx.ConnectError('private-synthetic-secret', request=request)
        if failure_kind == 'invalid-response':
            return httpx.Response(200, json={'id': 'private-synthetic-secret'})
        return httpx.Response(503, text='private-synthetic-secret')
    intercept_http(monkeypatch, handle)
    service = SinchFaxService('captured-project', 'captured-key', 'captured-secret',
                              base_url='https://captured.invalid/v3')
    with pytest.raises(RuntimeError) as failure:
        await service.upload_file(str(path))
    assert calls == ['https://captured.invalid/v3/projects/captured-project/files']
    assert 'private-synthetic-secret' not in str(failure.value)


@pytest.mark.asyncio
async def test_sinch_upload_preserves_success_interface_and_basic_auth(monkeypatch, tmp_path):
    path = tmp_path / 'synthetic.pdf'
    path.write_bytes(b'%PDF-synthetic-test')
    calls = []
    def handle(request):
        calls.append((str(request.url), request.headers['Authorization'], request.content))
        return httpx.Response(200, json={'data': {'id': '42'}})
    intercept_http(monkeypatch, handle)
    service = SinchFaxService('captured-project', 'captured-key', 'captured-secret',
                              base_url='https://captured.invalid/v3')
    assert await service.upload_file(str(path)) == 42
    assert calls[0][0] == 'https://captured.invalid/v3/projects/captured-project/files'
    assert calls[0][1] == 'Basic Y2FwdHVyZWQta2V5OmNhcHR1cmVkLXNlY3JldA=='
    assert b'%PDF-synthetic-test' in calls[0][2]
