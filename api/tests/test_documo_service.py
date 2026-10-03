"""Captured Documo wire contract against synthetic HTTPX transports only."""
import asyncio
from dataclasses import FrozenInstanceError
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest

from api.app.documo_service import DocumoFaxService


KEY = 'synthetic.literal-key+/=='
SID = 'b2de69f9-8807-457c-8b8b-889020ef5395'
OTHER_SID = '1566570b-2647-4876-8592-ac9e5d413408'
DESTINATION = '+12025550123'
PDF = b'%PDF-1.4\nsynthetic fax document\n%%EOF\n'
PRIVATE = 'synthetic-private-provider-detail'


def service_with_response(payload, *, status_code=200, base_url='', sandbox=False):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status_code, json=payload)

    service = DocumoFaxService(KEY, base_url, sandbox,
        transport=httpx.MockTransport(handle))
    return service, requests


@pytest.fixture
def document(tmp_path):
    path = tmp_path / 'private-original-name.pdf'
    path.write_bytes(PDF)
    return str(path)


def multipart_fields(request):
    mime = BytesParser(policy=default).parsebytes(
        b'Content-Type: ' + request.headers['content-type'].encode('ascii')
        + b'\r\nMIME-Version: 1.0\r\n\r\n' + request.content)
    assert mime.is_multipart()
    return {part.get_param('name', header='content-disposition'): part
            for part in mime.iter_parts()}


def test_send_uses_one_literal_authenticated_multipart_create(document, monkeypatch):
    service, requests = service_with_response({'messageId': SID})
    monkeypatch.setenv('DOCUMO_API_KEY', 'unrelated-current-key')
    monkeypatch.setenv('DOCUMO_BASE_URL', 'https://unrelated.invalid')
    monkeypatch.setenv('DOCUMO_USE_SANDBOX', 'true')

    result = asyncio.run(service.send_fax_file(DESTINATION, document))

    assert result == {'provider_sid': SID, 'status': 'in_progress'}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == 'POST'
    assert str(request.url) == 'https://api.documo.com/v1/faxes'
    assert request.headers['authorization'] == 'Basic ' + KEY
    parts = multipart_fields(request)
    assert set(parts) == {'faxNumber', 'coverPage', 'async', 'attachments'}
    assert parts['faxNumber'].get_payload(decode=True).decode() == DESTINATION
    assert parts['coverPage'].get_payload(decode=True) == b'false'
    assert parts['async'].get_payload(decode=True) == b'true'
    assert parts['attachments'].get_payload(decode=True) == PDF
    assert parts['attachments'].get_content_type() == 'application/pdf'
    assert parts['attachments'].get_filename() == 'document.pdf'
    assert b'private-original-name' not in request.content
    assert not request.url.query


@pytest.mark.parametrize(('base_url', 'sandbox', 'expected'), [
    ('', False, 'https://api.documo.com'),
    ('https://api.documo.com/', False, 'https://api.documo.com'),
    ('', True, 'https://api.sandbox.documo.com'),
    ('https://api.documo.com', True, 'https://api.sandbox.documo.com'),
    ('https://api.documo.com/', True, 'https://api.sandbox.documo.com'),
    ('https://api.sandbox.documo.com/', True, 'https://api.sandbox.documo.com'),
    ('https://captured.invalid:9443/', False, 'https://captured.invalid:9443'),
])
def test_captured_origin_and_sandbox_select_the_exact_create_endpoint(
        document, base_url, sandbox, expected):
    service, requests = service_with_response({'messageId': SID},
        base_url=base_url, sandbox=sandbox)
    asyncio.run(service.send_fax_file(DESTINATION, document))
    assert service.base_url == expected
    assert [str(request.url) for request in requests] == [expected + '/v1/faxes']


@pytest.mark.parametrize('base_url', [
    'http://captured.invalid',
    'https://',
    'https://user:private@captured.invalid',
    'https://captured.invalid?key=private',
    'https://captured.invalid#private',
    'https://captured.invalid/v1',
    'https://captured.invalid/custom/',
    'https://captured.invalid:bad',
    'https://captured.invalid:0',
    ' https://captured.invalid',
    'https://captured.invalid\n',
])
def test_invalid_origin_is_refused_before_io(base_url):
    def unexpected(request):
        pytest.fail('Invalid configuration reached HTTP')

    with pytest.raises(ValueError, match='Documo configuration is invalid') as failure:
        DocumoFaxService(KEY, base_url, False, transport=httpx.MockTransport(unexpected))
    assert base_url not in str(failure.value)
    assert PRIVATE not in str(failure.value)


def test_sandbox_cannot_be_redirected_to_an_unapproved_override():
    with pytest.raises(ValueError, match='Documo configuration is invalid'):
        DocumoFaxService(KEY, 'https://other-provider.invalid', True)


@pytest.mark.parametrize('sandbox', ['false', 0, None])
def test_sandbox_requires_a_boolean(sandbox):
    with pytest.raises(ValueError, match='Documo configuration is invalid'):
        DocumoFaxService(KEY, '', sandbox)


@pytest.mark.parametrize('key', [' leading', 'trailing ', 'bad\r\nInjected: value', 'nonascii-\u00e9', None])
def test_key_is_not_trimmed_or_allowed_to_inject_headers(key):
    with pytest.raises(ValueError, match='Documo configuration is invalid') as failure:
        DocumoFaxService(key, '', False)
    assert KEY not in str(failure.value)


def test_empty_key_is_unconfigured_and_never_requests_a_provider(document):
    def unexpected(request):
        pytest.fail('Unconfigured service reached HTTP')

    service = DocumoFaxService('', '', False, transport=httpx.MockTransport(unexpected))
    assert not service.is_configured()
    with pytest.raises(ValueError, match='Documo is not configured'):
        asyncio.run(service.send_fax_file(DESTINATION, document))
    with pytest.raises(ValueError, match='Documo is not configured'):
        asyncio.run(service.get_fax_status(SID))


def test_captured_configuration_is_frozen_and_secret_is_not_repr():
    service = DocumoFaxService(KEY, '', False)
    assert service.is_configured()
    assert KEY not in repr(service)
    with pytest.raises(FrozenInstanceError):
        service.api_key = 'new-key'
    with pytest.raises(FrozenInstanceError):
        service.base_url = 'https://new-account.invalid'


@pytest.mark.parametrize(('wire_status', 'expected'), [
    ('processing', 'in_progress'), ('success', 'success'), ('failed', 'failed')])
@pytest.mark.parametrize('operation', ['create', 'poll'])
def test_documented_statuses_and_only_safe_receipt_fields(document, operation, wire_status, expected):
    service, requests = service_with_response({'messageId': SID, 'status': wire_status,
        'resultCode': '0', 'accountId': PRIVATE, 'errorInfo': PRIVATE})
    result = asyncio.run(service.send_fax_file(DESTINATION, document) if operation == 'create'
                         else service.get_fax_status(SID))
    assert result == {'provider_sid': SID, 'status': expected}
    assert len(requests) == 1


def test_poll_uses_original_origin_literal_key_and_matching_uuid():
    service, requests = service_with_response({'messageId': SID, 'status': 'processing'},
        base_url='https://captured.invalid:9443/')
    assert asyncio.run(service.get_fax_status(SID)) == {
        'provider_sid': SID, 'status': 'in_progress'}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == 'GET'
    assert str(request.url) == f'https://captured.invalid:9443/v1/fax/{SID}/info'
    assert request.headers['authorization'] == 'Basic ' + KEY
    assert not request.content


@pytest.mark.parametrize('sid', [None, '', True, 42, 'remote-42', SID + '/info', '{' + SID + '}'])
def test_poll_identity_refused_before_http(sid):
    service, requests = service_with_response({'messageId': SID, 'status': 'success'})
    with pytest.raises(ValueError, match='Documo provider identity is invalid'):
        asyncio.run(service.get_fax_status(sid))
    assert not requests


@pytest.mark.parametrize('payload', [
    {}, {'messageId': None}, {'messageId': True}, {'messageId': 'local-job'},
    {'messageId': SID, 'status': None}, {'messageId': SID, 'status': ''},
    {'messageId': SID, 'status': 'scheduled'}, {'messageId': SID, 'status': 'cancelled'},
    {'messageId': SID, 'status': {'detail': PRIVATE}}, [PRIVATE],
])
def test_unusable_create_ack_is_sanitized_without_retry(document, payload):
    service, requests = service_with_response(payload)
    with pytest.raises(RuntimeError, match='Documo create request failed') as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document))
    assert len(requests) == 1
    assert PRIVATE not in str(failure.value)
    assert KEY not in str(failure.value)
    assert document not in str(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize('payload', [
    {'messageId': SID}, {'messageId': SID, 'status': None},
    {'messageId': SID, 'status': 'unknown'}, {'messageId': OTHER_SID, 'status': 'success'},
    {'status': 'success'}, [PRIVATE],
])
def test_poll_requires_real_explicit_status_and_same_identity(payload):
    service, requests = service_with_response(payload)
    with pytest.raises(RuntimeError, match='Documo status request failed') as failure:
        asyncio.run(service.get_fax_status(SID))
    assert len(requests) == 1
    assert PRIVATE not in str(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize('operation', ['create', 'poll'])
def test_invalid_json_is_safe_and_not_retried(document, operation):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=PRIVATE.encode())

    service = DocumoFaxService(KEY, '', False, transport=httpx.MockTransport(handle))
    with pytest.raises(RuntimeError, match=f'Documo {operation if operation == "create" else "status"} request failed') as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document) if operation == 'create'
                    else service.get_fax_status(SID))
    assert len(requests) == 1
    assert PRIVATE not in str(failure.value)


@pytest.mark.parametrize('status_code', [302, 401, 429, 503])
def test_failed_or_redirected_create_is_not_followed_or_retried(document, status_code):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status_code, headers={'Location': 'https://other-account.invalid'},
            json={'messageId': SID, 'status': 'success', 'error': PRIVATE})

    service = DocumoFaxService(KEY, '', False, transport=httpx.MockTransport(handle))
    with pytest.raises(RuntimeError, match='Documo create request failed') as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document))
    assert len(requests) == 1
    assert PRIVATE not in str(failure.value)


@pytest.mark.parametrize('failure_type', [httpx.ReadTimeout, httpx.ConnectError, asyncio.CancelledError])
def test_submission_failure_does_not_retry_and_closes_file(document, monkeypatch, failure_type):
    from api.app import documo_service
    opened = []
    requests = []
    actual_open = open

    def tracked_open(*args, **kwargs):
        handle = actual_open(*args, **kwargs)
        opened.append(handle)
        return handle

    def handle(request):
        requests.append(request)
        raise failure_type(PRIVATE)

    monkeypatch.setattr(documo_service, 'open', tracked_open, raising=False)
    service = DocumoFaxService(KEY, '', False, transport=httpx.MockTransport(handle))
    expected_error = asyncio.CancelledError if failure_type is asyncio.CancelledError else RuntimeError
    with pytest.raises(expected_error) as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document))
    assert len(requests) == 1
    assert len(opened) == 1 and opened[0].closed
    if failure_type is not asyncio.CancelledError:
        assert str(failure.value) == 'Documo create request failed.'
        assert failure.value.__suppress_context__


def test_missing_local_file_is_sanitized_before_provider_request(tmp_path):
    service, requests = service_with_response({'messageId': SID})
    path = str(tmp_path / 'private-nonexistent-file.pdf')
    with pytest.raises(RuntimeError, match='Documo create request failed') as failure:
        asyncio.run(service.send_fax_file(DESTINATION, path))
    assert not requests
    assert path not in str(failure.value)


def test_http_client_does_not_read_environment_proxy_or_follow_redirects(document, monkeypatch):
    actual_client = httpx.AsyncClient
    policies = []

    def client(**kwargs):
        policies.append(kwargs)
        return actual_client(**kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    service, requests = service_with_response({'messageId': SID, 'status': 'success'})
    asyncio.run(service.send_fax_file(DESTINATION, document))
    asyncio.run(service.get_fax_status(SID))
    assert len(requests) == 2
    assert len(policies) == 2
    assert all(policy['trust_env'] is False and policy['follow_redirects'] is False
               and 0 < policy['timeout'] <= 60 for policy in policies)
