"""Pure adapter status reads with synthetic HTTP; no application routes."""
import asyncio

import httpx
import pytest

from api.app.outbound_transport import normalize_status
from api.app.phaxio_service import PhaxioFaxService
from api.app.signalwire_service import SignalWireFaxService
from api.app.plugins.http_provider import HttpManifest, HttpProviderRuntime


PRIVATE = 'synthetic-private-provider-detail'


def status_read(monkeypatch, provider, payload):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        assert request.method == 'GET'
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    service = (PhaxioFaxService('synthetic-key', 'synthetic-secret') if provider == 'phaxio'
               else SignalWireFaxService('synthetic.signalwire.invalid',
                   'synthetic-project', 'synthetic-token'))
    return asyncio.run(service.get_fax_status('remote-42')), requests


@pytest.mark.parametrize('provider', ['phaxio', 'signalwire'])
@pytest.mark.parametrize('wire_status', ['cancelled', 'canceled'])
def test_poll_and_callback_agree_on_cancellation(monkeypatch, provider, wire_status):
    payload = ({'data': {'id': 'remote-42', 'status': wire_status}} if provider == 'phaxio'
               else {'sid': 'remote-42', 'status': wire_status})
    result, requests = status_read(monkeypatch, provider, payload)
    assert len(requests) == 1
    assert result['provider_sid'] == 'remote-42'
    assert result['provider_status'] == wire_status
    assert normalize_status(result['status']) == normalize_status(wire_status) == 'cancelled'


@pytest.mark.parametrize('provider', ['phaxio', 'signalwire'])
def test_unknown_explicit_status_reaches_durable_rejection(monkeypatch, provider):
    wire_status = 'provider-added-new-state'
    payload = ({'data': {'id': 'remote-42', 'status': wire_status}} if provider == 'phaxio'
               else {'sid': 'remote-42', 'status': wire_status})
    result, requests = status_read(monkeypatch, provider, payload)
    assert len(requests) == 1
    assert result['status'] == wire_status
    with pytest.raises(ValueError, match='Unrecognized provider status'):
        normalize_status(result['status'])


@pytest.mark.parametrize('identity', [None, '', '   ', True, 1.5, [], {'detail': PRIVATE}])
def test_phaxio_status_refuses_unusable_remote_identity(monkeypatch, identity):
    with pytest.raises(RuntimeError, match='Phaxio status request failed') as failure:
        status_read(monkeypatch, 'phaxio', {'data': {
            'id': identity, 'status': 'success', 'error_message': PRIVATE}})
    assert PRIVATE not in str(failure.value)
    assert failure.value.__suppress_context__


def test_phaxio_status_refuses_missing_remote_identity(monkeypatch):
    with pytest.raises(RuntimeError, match='Phaxio status request failed'):
        status_read(monkeypatch, 'phaxio', {'data': {'status': 'success'}})


@pytest.mark.parametrize('identity', [42, 'remote-42'])
def test_phaxio_status_retains_scalar_remote_identity(monkeypatch, identity):
    result, requests = status_read(monkeypatch, 'phaxio', {'data': {
        'id': identity, 'status': 'success'}})
    assert len(requests) == 1
    assert result['provider_sid'] == str(identity)
    assert normalize_status(result['status']) == 'success'


@pytest.mark.parametrize('provider', ['phaxio', 'signalwire'])
@pytest.mark.parametrize('status', [None, '', '   ', True, [], {'detail': PRIVATE}])
def test_status_read_refuses_unusable_wire_status(monkeypatch, provider, status):
    payload = ({'data': {'id': 'remote-42', 'status': status, 'detail': PRIVATE}}
               if provider == 'phaxio' else {'sid': 'remote-42', 'status': status, 'detail': PRIVATE})
    error = 'Phaxio status request failed' if provider == 'phaxio' else 'Unexpected SignalWire status response'
    with pytest.raises(RuntimeError, match=error) as failure:
        status_read(monkeypatch, provider, payload)
    assert PRIVATE not in str(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize('provider', ['phaxio', 'signalwire'])
def test_status_read_refuses_missing_wire_status(monkeypatch, provider):
    payload = {'data': {'id': 'remote-42'}} if provider == 'phaxio' else {'sid': 'remote-42'}
    error = 'Phaxio status request failed' if provider == 'phaxio' else 'Unexpected SignalWire status response'
    with pytest.raises(RuntimeError, match=error):
        status_read(monkeypatch, provider, payload)


def test_signalwire_status_retains_fax_status_alias_and_requested_sid(monkeypatch):
    result, requests = status_read(monkeypatch, 'signalwire', {'faxStatus': 'delivered'})
    assert len(requests) == 1
    assert result['provider_sid'] == 'remote-42'
    assert normalize_status(result['status']) == 'success'


def test_phaxio_create_still_accepts_acknowledged_id_without_status(monkeypatch):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        assert request.method == 'POST'
        return httpx.Response(200, json={'success': True, 'data': {'id': 42}})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    service = PhaxioFaxService('synthetic-key', 'synthetic-secret')
    result = asyncio.run(service.send_fax('+15550000001', 'https://document.invalid/media', 'job-42'))
    assert len(requests) == 1
    assert result == {'provider_sid': '42', 'status': 'queued'}


def manifest_response(monkeypatch, payload, *, create=False, status_map=None, provider_sid='remote-42'):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    response = {'job_id': 'id', 'status': 'status', 'status_map': status_map or {}}
    manifest = HttpManifest.from_dict({'id': 'synthetic-provider',
        'allowed_domains': ['provider.invalid'], 'auth': {'scheme': 'none'}, 'actions': {
            'send_fax': {'method': 'POST', 'url': 'https://provider.invalid/faxes',
                'body': {'kind': 'none'}, 'response': response},
            'get_status': {'method': 'GET', 'url': 'https://provider.invalid/faxes/{{provider_sid}}',
                'body': {'kind': 'none'}, 'response': response}}})
    runtime = HttpProviderRuntime(manifest, {}, {})
    result = asyncio.run(runtime.send_fax(to='+15550000001') if create
                         else runtime.get_status(job_id='local-42', provider_sid=provider_sid))
    assert len(requests) == 1
    assert requests[0].method == ('POST' if create else 'GET')
    return result


@pytest.mark.parametrize('status', [None, '', '   ', True, [], {'detail': PRIVATE}])
def test_manifest_status_refuses_unusable_wire_status(monkeypatch, status):
    with pytest.raises(RuntimeError, match='Unexpected provider response') as failure:
        manifest_response(monkeypatch, {'status': status, 'detail': PRIVATE})
    assert PRIVATE not in str(failure.value)
    assert failure.value.__suppress_context__


def test_manifest_status_refuses_missing_wire_status(monkeypatch):
    with pytest.raises(RuntimeError, match='Unexpected provider response'):
        manifest_response(monkeypatch, {'id': 'remote-42'})


@pytest.mark.parametrize('mapped', ['', '   '])
def test_manifest_status_refuses_empty_status_mapping(monkeypatch, mapped):
    with pytest.raises(RuntimeError, match='Unexpected provider response'):
        manifest_response(monkeypatch, {'status': 'provider-state'},
                          status_map={'provider-state': mapped})


def test_manifest_status_does_not_promote_empty_wire_value_with_mapping(monkeypatch):
    with pytest.raises(RuntimeError, match='Unexpected provider response'):
        manifest_response(monkeypatch, {'status': ''}, status_map={'': 'success'})


@pytest.mark.parametrize('provider_sid', ['remote-42', None])
def test_manifest_status_retains_requested_sid_without_local_fallback(monkeypatch, provider_sid):
    result = manifest_response(monkeypatch, {'status': 'provider-cancel'},
        status_map={'provider-cancel': 'cancelled'}, provider_sid=provider_sid)
    assert result['job_id'] == (provider_sid or '')
    assert normalize_status(result['status']) == 'cancelled'


def test_manifest_unknown_status_reaches_durable_rejection(monkeypatch):
    result = manifest_response(monkeypatch, {'status': 'provider-added-new-state'})
    with pytest.raises(ValueError, match='Unrecognized provider status'):
        normalize_status(result['status'])


def test_manifest_create_still_accepts_acknowledged_id_without_status(monkeypatch):
    result = manifest_response(monkeypatch, {'id': 'remote-42'}, create=True)
    assert result['job_id'] == 'remote-42'
    assert result['status'] == 'queued'
