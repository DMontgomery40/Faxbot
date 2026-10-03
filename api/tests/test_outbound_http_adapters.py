"""Internal one-attempt wire contracts; no application routes or real providers."""
import asyncio
import importlib
import importlib.util
import json
from urllib.parse import parse_qs

import httpx
import pytest

from api.app.phaxio_service import PhaxioFaxService
from api.app.signalwire_service import SignalWireFaxService
from api.app.sinch_service import SinchFaxService
from api.app.plugins.http_provider import HttpManifest, HttpProviderRuntime


PRIVATE = 'private-synthetic-response-or-exception'


def intercept(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    # The old retry loop must fail by request count, without waiting in RED.
    async def immediate(_delay):
        pass
    monkeypatch.setattr(asyncio, 'sleep', immediate)


def builtin(identity, callback=None):
    if identity == 'phaxio':
        return PhaxioFaxService('synthetic-key', 'synthetic-secret', callback)
    return SignalWireFaxService('synthetic.signalwire.invalid', 'synthetic-project',
                                'synthetic-token', '+15550000001', callback)


@pytest.mark.parametrize('identity', ['phaxio', 'signalwire'])
@pytest.mark.parametrize('failure_kind', ['timeout', 'http', 'redirect', 'json', 'shape', 'nested-id'])
def test_create_failure_is_one_request_and_sanitized(monkeypatch, caplog, identity, failure_kind):
    calls = []
    def handle(request):
        calls.append(request)
        if failure_kind == 'timeout':
            raise httpx.ReadTimeout(PRIVATE, request=request)
        if failure_kind == 'http':
            return httpx.Response(503, text=PRIVATE)
        if failure_kind == 'redirect':
            payload = {'success': True, 'data': {'id': 42, 'status': 'queued'}} if identity == 'phaxio' else {'sid': 'remote-42'}
            return httpx.Response(302, json=payload)
        if failure_kind == 'json':
            return httpx.Response(200, text=PRIVATE)
        if failure_kind == 'nested-id':
            payload = ({'success': True, 'data': {'id': {'secret': PRIVATE}, 'status': 'queued'}} if identity == 'phaxio'
                       else {'sid': {'secret': PRIVATE}, 'status': 'queued'})
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=[PRIVATE])
    intercept(monkeypatch, handle)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(builtin(identity).send_fax('+15550000002', 'https://document.invalid/private', 'job-42'))
    assert len(calls) == 1
    assert PRIVATE not in str(failure.value)
    assert failure.value.__suppress_context__
    assert '+15550000002' not in caplog.text
    assert PRIVATE not in caplog.text


def locator():
    assert importlib.util.find_spec('api.app.callback_locator') is not None, 'callback locator module missing'
    return importlib.import_module('api.app.callback_locator').callback_url_with_locators


def test_callback_locator_preserves_encoded_query_and_replaces_reserved_fields_once():
    construct = locator()
    original = 'https://faxbot.invalid/callback?tag=a%2Bb&job_id=stale&blank=&tag=two&attempt_id=stale&%6Aob_id=duplicate'
    assert construct(original, 'job /42', 'attempt+7') == (
        'https://faxbot.invalid/callback?tag=a%2Bb&blank=&tag=two&job_id=job+%2F42&attempt_id=attempt%2B7')
    assert construct(original, 'job-42') == (
        'https://faxbot.invalid/callback?tag=a%2Bb&blank=&tag=two&job_id=job-42')


@pytest.mark.parametrize('url', ['https://user:pass@faxbot.invalid/cb',
                               'https://faxbot.invalid/cb#', 'https://faxbot.invalid/cb#part',
                               'https://faxbot.invalid:bad/cb', 'ftp://faxbot.invalid/cb'])
def test_callback_locator_rejects_non_public_url(url):
    with pytest.raises(ValueError) as failure:
        locator()(url, 'job-42', 'attempt-7')
    assert 'pass' not in str(failure.value)


@pytest.mark.parametrize('identity', ['phaxio', 'signalwire'])
def test_builtin_create_passes_locator_and_preserves_response_mapping(monkeypatch, identity):
    calls = []
    def handle(request):
        calls.append(parse_qs(request.content.decode()))
        payload = ({'success': True, 'data': {'id': 42, 'status': 'success'}} if identity == 'phaxio'
                   else {'sid': 'remote-42', 'status': 'delivered'})
        return httpx.Response(200, json=payload)
    intercept(monkeypatch, handle)
    result = asyncio.run(builtin(identity, 'https://faxbot.invalid/cb?tag=a%2Bb&job_id=old').send_fax(
        '+15550000002', 'https://document.invalid/synthetic.pdf', 'job-42', attempt_id='attempt-7'))
    field = 'callback_url' if identity == 'phaxio' else 'StatusCallback'
    assert calls[0][field] == ['https://faxbot.invalid/cb?tag=a%2Bb&job_id=job-42&attempt_id=attempt-7']
    assert len(calls) == 1
    assert result == {'provider_sid': '42' if identity == 'phaxio' else 'remote-42', 'status': 'SUCCESS'}


@pytest.mark.parametrize('identity', ['phaxio', 'signalwire'])
def test_builtin_status_rejects_nested_remote_identity_without_exposing_payload(monkeypatch, identity):
    payload = ({'data': {'id': {'private': PRIVATE}, 'status': 'queued'}} if identity == 'phaxio'
               else {'sid': {'private': PRIVATE}, 'status': 'queued'})
    intercept(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(builtin(identity).get_fax_status('remote-42'))
    assert PRIVATE not in str(failure.value)
    assert failure.value.__suppress_context__


def runtime(*, kind='json', template='{"to":"{{to}}"}', headers=None, get_status=False):
    action = {'method': 'POST', 'url': 'https://provider.invalid/faxes',
              'headers': headers or {}, 'body': {'kind': kind, 'template': template},
              'response': {'job_id': 'id', 'status': 'status', 'error': 'error'}}
    actions = {'send_fax': action}
    if get_status:
        actions['get_status'] = {'method': 'GET', 'url': 'https://provider.invalid/faxes/{remote}',
            'path_params': [{'name': 'remote', 'source': 'provider_sid'}],
            'headers': {'X-Local-Job': '{{job_id}}'}, 'body': {'kind': 'none'},
            'response': {'job_id': 'id', 'status': 'status', 'error': 'error'}}
    return HttpProviderRuntime(HttpManifest.from_dict({'id': 'custom', 'actions': actions,
        'allowed_domains': ['provider.invalid'], 'auth': {'scheme': 'none'}}),
        {'api_key': 'captured-key'}, {'tag': 'captured-setting'})


def test_manifest_locator_templates_cannot_override_operation_context(monkeypatch):
    calls = []
    def handle(request):
        calls.append((json.loads(request.content), dict(request.headers)))
        return httpx.Response(200, json={'id': 'remote-42', 'status': 'queued'})
    intercept(monkeypatch, handle)
    adapter = runtime(template=json.dumps({'to': '{{to}}', 'url': '{{file_url}}', 'from': '{{from}}',
        'job': '{{job_id}}', 'attempt': '{{attempt_id}}', 'key': '{{creds.api_key}}', 'tag': '{{settings.tag}}'}),
        headers={'Authorization': 'Bearer {{creds.api_key}}', 'X-Attempt': '{{attempt_id}}'})
    result = asyncio.run(adapter.send_fax(to='+15550000002', file_url='https://document.invalid/real',
        from_number='+15550000001', extra={'job_id': 'job-42', 'attempt_id': 'attempt-7',
            'to': 'override', 'from': 'override', 'file_url': 'override',
            'creds': {'api_key': 'override'}, 'settings': {'tag': 'override'}}))
    assert calls[0][0] == {'to': '+15550000002', 'url': 'https://document.invalid/real',
        'from': '+15550000001', 'job': 'job-42', 'attempt': 'attempt-7',
        'key': 'captured-key', 'tag': 'captured-setting'}
    assert calls[0][1]['authorization'] == 'Bearer captured-key'
    assert calls[0][1]['x-attempt'] == 'attempt-7'
    assert result['job_id'] == 'remote-42'


@pytest.mark.parametrize('action', ['send', 'status'])
def test_manifest_form_fields_stay_in_body_and_auth_query_stays_in_query(monkeypatch, action):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 'remote-42', 'status': 'queued'})
    intercept(monkeypatch, handle)
    adapter = runtime(kind='form', template='to={{to}}&document={{file_url}}', get_status=True)
    adapter.m.auth = {'scheme': 'api_key_query', 'query_name': 'auth_key'}
    if action == 'status':
        adapter.m.actions['get_status'].method = 'POST'
        adapter.m.actions['get_status'].body_kind = 'form'
        adapter.m.actions['get_status'].body_template = 'remote={{provider_sid}}&local={{job_id}}'
    asyncio.run(adapter.send_fax(to='+15550000002', file_url='https://document.invalid/private-token')
                if action == 'send' else adapter.get_status(job_id='local-42', provider_sid='remote-42'))
    assert parse_qs(calls[0].url.query.decode()) == {'auth_key': ['captured-key']}
    expected = ({'to': ['+15550000002'], 'document': ['https://document.invalid/private-token']} if action == 'send'
                else {'remote': ['remote-42'], 'local': ['local-42']})
    assert parse_qs(calls[0].content.decode()) == expected


def test_manifest_multipart_local_file_wins_over_download(monkeypatch, tmp_path):
    pdf = tmp_path / 'synthetic.pdf'
    pdf.write_bytes(b'%PDF-local-document')
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 'remote-42'})
    intercept(monkeypatch, handle)
    asyncio.run(runtime(kind='multipart', template='to={{to}}&file={{file_path}}').send_fax(
        to='+15550000002', file_path=str(pdf), file_url='https://download.invalid/private-token'))
    assert len(calls) == 1
    assert calls[0].method == 'POST'
    assert b'%PDF-local-document' in calls[0].content
    assert b'name="file"' in calls[0].content


@pytest.mark.parametrize('attachment', ['missing', 'unreadable', 'failed-download'])
def test_manifest_required_attachment_failure_never_submits(monkeypatch, tmp_path, attachment):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(503, text=PRIVATE)
    intercept(monkeypatch, handle)
    options = {}
    if attachment == 'unreadable':
        options = {'file_path': str(tmp_path), 'file_url': 'https://download.invalid/private-token'}
    elif attachment == 'failed-download':
        options = {'file_url': 'https://download.invalid/private-token'}
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(runtime(kind='multipart', template='file={{file_path}}').send_fax(to='+15550000002', **options))
    assert not any(request.method == 'POST' for request in calls)
    assert len(calls) == (1 if attachment == 'failed-download' else 0)
    assert PRIVATE not in str(failure.value)
    assert 'private-token' not in str(failure.value)
    assert str(tmp_path) not in str(failure.value)


@pytest.mark.parametrize('action', ['send', 'status'])
@pytest.mark.parametrize('failure_kind', ['timeout', 'http', 'redirect', 'json', 'shape'])
def test_manifest_failures_raise_safe_error_instead_of_terminal_result(monkeypatch, action, failure_kind):
    calls = []
    def handle(request):
        calls.append(request)
        if failure_kind == 'timeout':
            raise httpx.ReadTimeout(PRIVATE, request=request)
        if failure_kind == 'http':
            return httpx.Response(503, json={'id': 'uncertain-id', 'status': 'failed', 'error': PRIVATE})
        if failure_kind == 'redirect':
            return httpx.Response(302, json={'id': 'uncertain-id', 'status': 'queued'})
        if failure_kind == 'json':
            return httpx.Response(200, text=PRIVATE)
        return httpx.Response(200, json=[PRIVATE])
    intercept(monkeypatch, handle)
    adapter = runtime(get_status=True)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(adapter.send_fax(to='+15550000002') if action == 'send'
                    else adapter.get_status(job_id='local-42', provider_sid='remote-42'))
    assert len(calls) == 1
    assert PRIVATE not in str(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize('remote', ['remote-42', None])
def test_manifest_status_does_not_invent_remote_identity(monkeypatch, remote):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'status': 'queued', 'error': PRIVATE})
    intercept(monkeypatch, handle)
    result = asyncio.run(runtime(get_status=True).get_status(job_id='local-42', provider_sid=remote))
    assert result['job_id'] == (remote or '')
    assert calls[0].headers['X-Local-Job'] == 'local-42'
    assert str(calls[0].url).endswith('/' + (remote or ''))
    assert PRIVATE not in result.get('error', '')


@pytest.mark.parametrize('direct', [True, False])
def test_sinch_send_requires_captured_credentials_before_request(monkeypatch, tmp_path, direct):
    path = tmp_path / 'synthetic.pdf'
    path.write_bytes(b'%PDF-synthetic')
    calls = []
    intercept(monkeypatch, lambda request: calls.append(request) or httpx.Response(200, json={'id': 42}))
    adapter = SinchFaxService('project', '', 'secret', 'https://sinch.invalid/v3')
    with pytest.raises(ValueError):
        asyncio.run(adapter.send_fax_file('+15550000002', str(path)) if direct
                    else adapter.send_fax('+15550000002', 42))
    assert calls == []


@pytest.mark.parametrize('failure_kind', ['timeout', 'http', 'redirect', 'json', 'shape'])
def test_sinch_direct_create_is_one_multipart_request_with_safe_failures(monkeypatch, tmp_path, failure_kind):
    path = tmp_path / 'synthetic.pdf'
    path.write_bytes(b'%PDF-synthetic')
    calls = []
    def handle(request):
        calls.append(request)
        if failure_kind == 'timeout':
            raise httpx.ReadTimeout(PRIVATE, request=request)
        if failure_kind == 'http':
            return httpx.Response(503, text=PRIVATE)
        if failure_kind == 'redirect':
            return httpx.Response(302, json={'id': 'uncertain-id', 'status': 'QUEUED'})
        if failure_kind == 'json':
            return httpx.Response(200, text=PRIVATE)
        return httpx.Response(200, json=[PRIVATE])
    intercept(monkeypatch, handle)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(SinchFaxService('project', 'key', 'secret', 'https://sinch.invalid/v3').send_fax_file(
            '+15550000002', str(path)))
    assert len(calls) == 1
    assert calls[0].url.path == '/v3/projects/project/faxes'
    assert b'%PDF-synthetic' in calls[0].content
    assert PRIVATE not in str(failure.value)
    assert failure.value.__suppress_context__


def test_sinch_direct_create_returns_response_without_status_normalization(monkeypatch, tmp_path):
    path = tmp_path / 'synthetic.pdf'
    path.write_bytes(b'%PDF-synthetic')
    response = {'id': 'remote-42', 'status': 'IN_PROGRESS', 'numberOfPages': 3}
    calls = []
    intercept(monkeypatch, lambda request: calls.append(request) or httpx.Response(200, json=response))
    result = asyncio.run(SinchFaxService('project', 'key', 'secret', 'https://sinch.invalid/v3').send_fax_file(
        '+15550000002', str(path)))
    assert result == response
    assert len(calls) == 1
