"""InterFAX wire protocol using synthetic HTTPX transports, never real services."""
import asyncio
import base64
from dataclasses import FrozenInstanceError
from email.parser import BytesParser
from email.policy import default
import os
import re

import httpx
import pytest


USERNAME = 'synthetic-user'
PASSWORD = 'synthetic-password+/=='
AUTHORIZATION = 'Basic ' + base64.b64encode(
    b'synthetic-user:synthetic-password+/==').decode('ascii')
DESTINATION = '+12025550123'
PDF = b'%PDF-1.4\r\nsynthetic\x00\xff document\r\n%%EOF\n'
PRIVATE = 'synthetic-private-provider-detail'
SID = '9007199254740993'
MAX_SID = '9223372036854775807'


def service_class():
    from api.app.interfax_service import InterfaxFaxService
    return InterfaxFaxService


def service_with_response(*, status_code=201, location='/outbound/faxes/' + SID,
                          headers=None, content=b'', payload=None, base_url=None):
    requests = []

    def handle(request):
        requests.append(request)
        response_headers = headers if headers is not None else (
            [('Location', location)] if location is not None else [])
        if payload is not None:
            return httpx.Response(status_code, headers=response_headers, json=payload)
        return httpx.Response(status_code, headers=response_headers, content=content)

    options = {'transport': httpx.MockTransport(handle)}
    if base_url is not None:
        options['base_url'] = base_url
    return service_class()(USERNAME, PASSWORD, **options), requests


@pytest.fixture
def document(tmp_path):
    path = tmp_path / 'private-original-name.pdf'
    path.write_bytes(PDF)
    return str(path)


def pdf_part(request):
    mime = BytesParser(policy=default).parsebytes(
        b'Content-Type: ' + request.headers['content-type'].encode('ascii')
        + b'\r\nMIME-Version: 1.0\r\n\r\n' + request.content)
    assert mime.get_content_type() == 'multipart/mixed'
    parts = list(mime.iter_parts())
    assert len(parts) == 1
    return parts[0]


def assert_safe(error):
    for private in (USERNAME, PASSWORD, AUTHORIZATION, PRIVATE):
        assert private not in str(error)
        assert private not in repr(error)
    assert error.__suppress_context__


def test_create_posts_one_mixed_pdf_with_encoded_destination_and_literal_basic_auth(document, monkeypatch):
    service, requests = service_with_response(content=b'not required JSON')
    monkeypatch.setenv('INTERFAX_USERNAME', 'unrelated-current-user')
    monkeypatch.setenv('INTERFAX_PASSWORD', 'unrelated-current-password')
    monkeypatch.setenv('INTERFAX_BASE_URL', 'https://unrelated.invalid')

    result = asyncio.run(service.send_fax_file(DESTINATION, document))

    assert result == {'provider_sid': SID, 'status': 'in_progress'}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == 'POST'
    assert request.url.path == '/outbound/faxes'
    assert request.url.host == 'rest.interfax.net'
    assert dict(request.url.params) == {'faxNumber': DESTINATION}
    assert b'faxNumber=%2B12025550123' in request.url.query
    assert request.headers['authorization'] == AUTHORIZATION
    assert int(request.headers['content-length']) == len(request.content)
    assert 'content-location' not in request.headers
    assert 'idempotency-key' not in request.headers
    part = pdf_part(request)
    assert part.get_content_type() == 'application/pdf'
    assert part.get_payload(decode=True) == PDF
    assert part.get_filename() is None
    assert part['Content-Location'] is None
    assert b'private-original-name' not in request.content


@pytest.mark.parametrize(('base_url', 'location', 'expected'), [
    ('https://rest.interfax.net/', '/outbound/faxes/42', 'https://rest.interfax.net'),
    ('https://REST.INTERFAX.NET:443/', 'https://rest.interfax.net/outbound/faxes/42', 'https://rest.interfax.net'),
    ('https://captured.invalid:9443/', 'https://captured.invalid:9443/outbound/faxes/42', 'https://captured.invalid:9443'),
    ('https://captured.invalid:9443', '/outbound/faxes/42', 'https://captured.invalid:9443'),
    ('https://[::1]:9443/', '/outbound/faxes/42', 'https://[::1]:9443'),
])
def test_origin_override_stays_captured_and_canonical(document, base_url, location, expected):
    service, requests = service_with_response(base_url=base_url, location=location)
    assert asyncio.run(service.send_fax_file(DESTINATION, document)) == {
        'provider_sid': '42', 'status': 'in_progress'}
    assert service.base_url == expected
    assert str(requests[0].url).split('?')[0] == expected + '/outbound/faxes'


@pytest.mark.parametrize('base_url', [
    '', 'http://captured.invalid', 'https://', 'https://user:secret@captured.invalid',
    'https://captured.invalid?key=secret', 'https://captured.invalid?',
    'https://captured.invalid#secret', 'https://captured.invalid#',
    'https://captured.invalid/outbound', 'https://captured.invalid//',
    'https://captured.invalid:bad', 'https://captured.invalid:',
    'https://captured.invalid:0', 'https://captured.invalid:65536',
    'https://captured.invalid:+443', 'https://captured.invalid:-443',
    'https://captured.invalid\\evil', ' https://captured.invalid',
    'https://captured.invalid\n', 'https://captured.invalid\x7f',
    'https://captured.invalid/\u00a0', 'https://captured%2einvalid', None, 42, True,
])
def test_invalid_origin_fails_safely_before_io(base_url):
    with pytest.raises(ValueError) as failure:
        service_class()(USERNAME, PASSWORD, base_url)
    assert_safe(failure.value)
    assert 'configuration' in str(failure.value).lower()


@pytest.mark.parametrize(('username', 'password'), [
    (None, PASSWORD), (USERNAME, None), (True, PASSWORD), (USERNAME, 42),
    ('bad:user', PASSWORD), ('bad\nuser', PASSWORD), (USERNAME, 'bad\rpassword'),
    (USERNAME, 'bad\x00password'), (USERNAME, 'bad\x7fpassword'),
])
def test_invalid_basic_credentials_fail_without_disclosure(username, password):
    with pytest.raises(ValueError) as failure:
        service_class()(username, password)
    assert_safe(failure.value)


@pytest.mark.parametrize(('username', 'password'), [('', PASSWORD), (USERNAME, ''), ('', '')])
def test_missing_captured_credentials_are_unconfigured_and_never_request(document, username, password):
    requests = []
    service = service_class()(username, password,
        transport=httpx.MockTransport(lambda request: requests.append(request)))
    assert service.is_configured() is False
    with pytest.raises(ValueError, match='not configured'):
        asyncio.run(service.send_fax_file(DESTINATION, document))
    with pytest.raises(ValueError, match='not configured'):
        asyncio.run(service.get_fax_status(SID))
    assert requests == []


def test_configuration_is_frozen_and_repr_excludes_credentials_and_transport():
    service, _ = service_with_response()
    assert service.is_configured() is True
    assert USERNAME not in repr(service)
    assert PASSWORD not in repr(service)
    assert 'MockTransport' not in repr(service)
    for name, value in [('username', 'new-user'), ('password', 'new-password'),
                        ('base_url', 'https://new-account.invalid'), ('transport', None)]:
        with pytest.raises(FrozenInstanceError):
            setattr(service, name, value)


def test_invalid_transport_configuration_is_sanitized():
    with pytest.raises(ValueError) as failure:
        service_class()(USERNAME, PASSWORD, transport=PRIVATE)
    assert_safe(failure.value)


@pytest.mark.parametrize(('location', 'expected'), [
    ('/outbound/faxes/1', '1'), ('/outbound/faxes/00042', '42'),
    ('/outbound/faxes/' + SID, SID), ('/outbound/faxes/' + MAX_SID, MAX_SID),
    ('https://rest.interfax.net/outbound/faxes/' + SID, SID),
    ('https://rest.interfax.net:443/outbound/faxes/' + SID, SID),
])
@pytest.mark.parametrize('status_code', [200, 201, 202, 204, 206, 299])
def test_final_2xx_with_one_valid_location_acknowledges_without_a_followup_get(
        document, location, expected, status_code):
    service, requests = service_with_response(location=location, status_code=status_code)
    assert asyncio.run(service.send_fax_file(DESTINATION, document)) == {
        'provider_sid': expected, 'status': 'in_progress'}
    assert len(requests) == 1 and requests[0].method == 'POST'


@pytest.mark.parametrize('location', [
    None, '', '42', 'outbound/faxes/42', '//rest.interfax.net/outbound/faxes/42',
    'http://rest.interfax.net/outbound/faxes/42',
    'https://wrong-origin.invalid/outbound/faxes/42',
    'https://rest.interfax.net:9443/outbound/faxes/42',
    'https://rest.interfax.net:/outbound/faxes/42',
    'https://private@rest.interfax.net/outbound/faxes/42',
    'https://rest.interfax.net/outbound/faxes/42?token=private',
    '/outbound/faxes/42?', '/outbound/faxes/42#',
    '/outbound/faxes/42/status', '/outbound/faxes/42/',
    '/outbound/faxes/42/../43', '/outbound/faxes//42',
    '/outbound/faxes/42%2fstatus', '/outbound/faxes/%34%32',
    '/outbound/faxes/42\\status', '/outbound/faxes/0',
    '/outbound/faxes/-1', '/outbound/faxes/+42', '/outbound/faxes/４２',
    '/outbound/faxes/9223372036854775808', '/outbound/faxes/' + ('0' * 10000) + '42',
    '/outbound/faxes/42, /outbound/faxes/42', '/outbound/faxes/42\r\nInjected: value',
    ' /outbound/faxes/42', '/outbound/faxes/42 ', '/outbound/faxes/42\x7f',
])
def test_unusable_location_is_ambiguous_and_never_followed_or_retried(document, location):
    service, requests = service_with_response(location=location, content=PRIVATE.encode())
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document))
    assert 'uncertain' in str(failure.value)
    assert_safe(failure.value)
    assert len(requests) == 1 and requests[0].method == 'POST'


@pytest.mark.parametrize('headers', [
    [('Location', '/outbound/faxes/42'), ('Location', '/outbound/faxes/42')],
    [('Location', '/outbound/faxes/42'), ('location', '/outbound/faxes/43')],
])
def test_duplicate_location_headers_are_rejected_even_when_identical(document, headers):
    service, requests = service_with_response(headers=headers)
    with pytest.raises(RuntimeError, match='uncertain') as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document))
    assert_safe(failure.value)
    assert len(requests) == 1


@pytest.mark.parametrize('status_code', [199, 300, 302, 401, 403, 429, 500, 503])
def test_nonfinal_or_unsuccessful_create_never_uses_body_or_location_as_receipt(document, status_code):
    service, requests = service_with_response(status_code=status_code,
        payload={'id': int(SID), 'status': 0, 'private': PRIVATE})
    with pytest.raises(RuntimeError, match='uncertain') as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document))
    assert_safe(failure.value)
    assert len(requests) == 1


def test_correlation_is_bounded_metadata_and_distinguishes_attempts_without_deduplication(document):
    service, requests = service_with_response()
    for attempt in ['b' * 32, 'b' * 32, 'c' * 32]:
        asyncio.run(service.send_fax_file(DESTINATION, document, 'a' * 32, attempt_id=attempt))
    references = [request.url.params['reference'] for request in requests]
    assert len(requests) == 3 and all(request.method == 'POST' for request in requests)
    assert all(re.fullmatch(r'faxbot-[0-9a-f]{32}', value) for value in references)
    assert references[0] == references[1] and references[0] != references[2]
    assert all('a' * 32 not in value and 'b' * 32 not in value for value in references)
    assert all('idempotency-key' not in request.headers for request in requests)


@pytest.mark.parametrize(('job_id', 'attempt_id'), [(None, 'b' * 32), ('a' * 32, None)])
def test_one_supplied_correlation_id_is_sufficient(document, job_id, attempt_id):
    service, requests = service_with_response()
    asyncio.run(service.send_fax_file(DESTINATION, document, job_id, attempt_id=attempt_id))
    assert re.fullmatch(r'faxbot-[0-9a-f]{32}', requests[0].url.params['reference'])


@pytest.mark.parametrize(('job_id', 'attempt_id'), [('', None), (42, None),
    ('x' * 129, None), ('bad\njob', None), (None, True), (None, 'x' * 129)])
def test_unbounded_or_nonstring_correlation_is_refused_before_http(document, job_id, attempt_id):
    service, requests = service_with_response()
    with pytest.raises(ValueError) as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document, job_id, attempt_id=attempt_id))
    assert PRIVATE not in str(failure.value)
    assert requests == []


@pytest.mark.parametrize('destination', [None, '', True, 42, 'bad\r\ndestination'])
def test_invalid_destination_is_refused_before_http(document, destination):
    service, requests = service_with_response()
    with pytest.raises(ValueError):
        asyncio.run(service.send_fax_file(destination, document))
    assert requests == []


def test_poll_uses_original_origin_json_accept_basic_auth_and_lossless_matching_id():
    service, requests = service_with_response(status_code=200,
        payload={'id': int(MAX_SID), 'status': -22, 'uri': 'https://untrusted.invalid'},
        base_url='https://captured.invalid:9443/')
    assert asyncio.run(service.get_fax_status(MAX_SID)) == {
        'provider_sid': MAX_SID, 'status': 'in_progress'}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == 'GET'
    assert str(request.url) == 'https://captured.invalid:9443/outbound/faxes/' + MAX_SID + '/status'
    assert request.headers['accept'] == 'application/json'
    assert request.headers['authorization'] == AUTHORIZATION
    assert request.content == b''


@pytest.mark.parametrize(('wire_status', 'expected'), [
    (-2147483648, 'in_progress'), (-1000, 'in_progress'), (-22, 'in_progress'),
    (-11, 'in_progress'), (-3, 'in_progress'), (-1, 'in_progress'),
    (0, 'success'), (1, 'failed'), (400, 'failed'), (403, 'cancelled'),
    (404, 'failed'), (2147483647, 'failed'),
])
def test_every_signed_status_class_has_explicit_normalization(wire_status, expected):
    service, requests = service_with_response(status_code=200,
        payload={'id': int(SID), 'status': wire_status, 'private': PRIVATE})
    assert asyncio.run(service.get_fax_status(SID)) == {'provider_sid': SID, 'status': expected}
    assert len(requests) == 1


@pytest.mark.parametrize('payload', [
    {}, {'id': int(SID)}, {'status': 0}, {'id': int(SID) + 1, 'status': 0},
    {'id': True, 'status': 0}, {'id': 42.0, 'status': 0}, {'id': SID, 'status': 0},
    {'id': 0, 'status': 0}, {'id': -1, 'status': 0}, {'id': 9223372036854775808, 'status': 0},
    {'id': int(SID), 'status': True}, {'id': int(SID), 'status': False},
    {'id': int(SID), 'status': '0'}, {'id': int(SID), 'status': None},
    {'id': int(SID), 'status': 0.0}, {'id': int(SID), 'status': -2147483649},
    {'id': int(SID), 'status': 2147483648}, {'id': int(SID), 'status': {}},
    [], [PRIVATE], 0, 403,
])
def test_poll_rejects_unknown_shapes_types_ranges_and_mismatched_ids(payload):
    service, requests = service_with_response(status_code=200, payload=payload)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.get_fax_status(SID))
    assert_safe(failure.value)
    assert len(requests) == 1


@pytest.mark.parametrize('provider_sid', [None, '', True, 42, '0', '-1', '+1',
    '４２', '42/status', '9223372036854775808', '0' * 10000 + '42'])
def test_poll_identity_is_bounded_before_numeric_conversion_or_http(provider_sid):
    service, requests = service_with_response(status_code=200, payload={'id': 42, 'status': 0})
    with pytest.raises(ValueError):
        asyncio.run(service.get_fax_status(provider_sid))
    assert requests == []


def test_poll_canonicalizes_bounded_zero_padded_decimal_identity():
    service, requests = service_with_response(status_code=200, payload={'id': 42, 'status': 0})
    assert asyncio.run(service.get_fax_status('00042')) == {'provider_sid': '42', 'status': 'success'}
    assert requests[0].url.path == '/outbound/faxes/42/status'


@pytest.mark.parametrize('status_code', [302, 401, 403, 429, 500])
def test_http_status_is_request_failure_even_with_numeric_cancellation_in_body(status_code):
    service, requests = service_with_response(status_code=status_code,
        payload={'id': int(SID), 'status': 403, 'private': PRIVATE})
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.get_fax_status(SID))
    assert_safe(failure.value)
    assert len(requests) == 1


def test_invalid_poll_json_is_sanitized():
    service, requests = service_with_response(status_code=200, content=PRIVATE.encode())
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.get_fax_status(SID))
    assert_safe(failure.value)
    assert len(requests) == 1


@pytest.mark.parametrize('operation', ['create', 'poll'])
@pytest.mark.parametrize('failure_type', [httpx.ReadTimeout, httpx.ConnectError,
    httpx.RemoteProtocolError, RuntimeError])
def test_transport_failure_is_sanitized_and_never_retried(document, operation, failure_type):
    requests = []

    def handle(request):
        requests.append(request)
        raise failure_type(PASSWORD + ' ' + PRIVATE + ' ' + AUTHORIZATION)

    service = service_class()(USERNAME, PASSWORD, transport=httpx.MockTransport(handle))
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document) if operation == 'create'
                    else service.get_fax_status(SID))
    assert_safe(failure.value)
    assert len(requests) == 1
    if operation == 'create':
        assert 'uncertain' in str(failure.value)


def test_missing_local_document_is_safe_and_does_not_make_a_request(tmp_path):
    service, requests = service_with_response()
    path = str(tmp_path / 'private-missing-document.pdf')
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.send_fax_file(DESTINATION, path))
    assert_safe(failure.value)
    assert path not in str(failure.value)
    assert requests == []


@pytest.mark.parametrize('file_path', [None, '', True, 0, 42])
def test_document_path_cannot_open_or_close_an_unintended_file_descriptor(file_path, monkeypatch):
    from api.app import interfax_service
    opened = []

    def rejected_open(*args, **kwargs):
        opened.append(args)
        raise OSError(PRIVATE)

    monkeypatch.setattr(interfax_service.os, 'open', rejected_open)
    service, requests = service_with_response()
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.send_fax_file(DESTINATION, file_path))
    assert_safe(failure.value)
    assert opened == [] and requests == []


@pytest.mark.parametrize('operation', ['create', 'poll'])
def test_cancellation_preserves_type_sanitizes_detail_and_closes_open_document(document, monkeypatch, operation):
    from api.app import interfax_service
    actual_fdopen = os.fdopen
    opened = []
    requests = []

    def tracked_fdopen(*args, **kwargs):
        handle = actual_fdopen(*args, **kwargs)
        opened.append(handle)
        return handle

    def handle(request):
        requests.append(request)
        raise asyncio.CancelledError(PASSWORD + ' ' + PRIVATE)

    monkeypatch.setattr(interfax_service.os, 'fdopen', tracked_fdopen)
    service = service_class()(USERNAME, PASSWORD, transport=httpx.MockTransport(handle))
    with pytest.raises(asyncio.CancelledError) as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document) if operation == 'create'
                    else service.get_fax_status(SID))
    assert_safe(failure.value)
    assert len(requests) == 1
    assert len(opened) == (1 if operation == 'create' else 0)
    assert all(handle.closed for handle in opened)


def test_pdf_stream_reads_bounded_chunks_and_closes_file_on_failure(tmp_path, monkeypatch):
    from api.app import interfax_service
    path = tmp_path / 'large-prepared-document.pdf'
    large_pdf = PDF + b'x' * (1024 * 1024 + 7)
    path.write_bytes(large_pdf)
    actual_fdopen = os.fdopen
    opened = []
    sizes = []

    class TrackedFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size):
            sizes.append(size)
            assert 0 < size <= 65536
            return self.handle.read(size)

    def tracked_fdopen(*args, **kwargs):
        handle = actual_fdopen(*args, **kwargs)
        opened.append(handle)
        return TrackedFile(handle)

    monkeypatch.setattr(interfax_service.os, 'fdopen', tracked_fdopen)
    service, requests = service_with_response(location=None)
    with pytest.raises(RuntimeError, match='uncertain'):
        asyncio.run(service.send_fax_file(DESTINATION, str(path)))
    assert pdf_part(requests[0]).get_payload(decode=True) == large_pdf
    assert int(requests[0].headers['content-length']) == len(requests[0].content)
    assert len(sizes) > 1 and all(handle.closed for handle in opened)


def test_client_disables_redirects_environment_proxies_and_has_fixed_bounded_timeouts(document, monkeypatch):
    actual_client = httpx.AsyncClient
    policies = []

    def client(**kwargs):
        policies.append(kwargs)
        return actual_client(**kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    service, requests = service_with_response(status_code=200,
        payload={'id': int(SID), 'status': 0})
    asyncio.run(service.send_fax_file(DESTINATION, document))
    asyncio.run(service.get_fax_status(SID))
    assert len(requests) == 2 and len(policies) == 2
    for policy in policies:
        assert policy['follow_redirects'] is False
        assert policy['trust_env'] is False
        timeout = httpx.Timeout(policy['timeout'])
        assert all(value is not None and 0 < value <= 60
                   for value in (timeout.connect, timeout.read, timeout.write, timeout.pool))


class TrackedResponseStream(httpx.AsyncByteStream):
    """A real streaming HTTPX body whose consumption/closure is observable."""

    def __init__(self, chunks=(), *, iteration_error=None):
        self.chunks = chunks
        self.iteration_error = iteration_error
        self.iterations = 0
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        self.iterations += 1
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
        if self.iteration_error is not None:
            raise self.iteration_error

    async def aclose(self):
        self.closed = True


def service_with_stream(stream, *, status_code=200, headers=None):
    requests = []
    responses = []

    def handle(request):
        requests.append(request)
        response = httpx.Response(status_code, headers=headers, stream=stream)
        responses.append(response)
        return response

    return service_class()(USERNAME, PASSWORD, transport=httpx.MockTransport(handle)), requests, responses


def test_create_acknowledges_headers_without_iterating_an_unused_failing_response_body(document):
    stream = TrackedResponseStream(iteration_error=httpx.ReadTimeout(PRIVATE))
    service, requests, responses = service_with_stream(stream,
        status_code=201, headers={'Location': '/outbound/faxes/' + SID})
    assert asyncio.run(service.send_fax_file(DESTINATION, document)) == {
        'provider_sid': SID, 'status': 'in_progress'}
    assert stream.iterations == 0 and stream.closed
    assert len(requests) == 1 and responses[0].is_closed


@pytest.mark.parametrize(('status_code', 'location'), [(201, None), (403, '/outbound/faxes/' + SID)])
def test_unusable_create_headers_close_response_without_reading_error_body(document, status_code, location):
    stream = TrackedResponseStream([PRIVATE.encode()])
    headers = {'Location': location} if location is not None else {}
    service, requests, responses = service_with_stream(stream, status_code=status_code, headers=headers)
    with pytest.raises(RuntimeError, match='uncertain') as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document))
    assert_safe(failure.value)
    assert stream.iterations == 0 and stream.closed
    assert len(requests) == 1 and responses[0].is_closed


def test_poll_stops_an_oversized_stream_before_consuming_its_valid_json_tail():
    stream = TrackedResponseStream([b' ' * 4096] * 20
        + [b'{"id":9007199254740993,"status":0}'])
    service, requests, responses = service_with_stream(stream)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.get_fax_status(SID))
    assert_safe(failure.value)
    assert stream.yielded <= 5 and stream.closed
    assert len(requests) == 1 and responses[0].is_closed


@pytest.mark.parametrize(('size', 'accepted'), [(16384, True), (16385, False)])
def test_poll_json_response_ceiling_has_a_precise_boundary(size, accepted):
    payload = b'{"id":9007199254740993,"status":0}'
    stream = TrackedResponseStream([payload + b' ' * (size - len(payload))])
    service, requests, responses = service_with_stream(stream)
    if accepted:
        assert asyncio.run(service.get_fax_status(SID)) == {'provider_sid': SID, 'status': 'success'}
    else:
        with pytest.raises(RuntimeError) as failure:
            asyncio.run(service.get_fax_status(SID))
        assert_safe(failure.value)
    assert stream.closed and responses[0].is_closed and len(requests) == 1


def test_poll_requests_identity_encoding_and_refuses_encoded_bodies_without_decompressing():
    stream = TrackedResponseStream([b'encoded-private-body'])
    service, requests, responses = service_with_stream(stream,
        headers={'Content-Encoding': 'gzip'})
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.get_fax_status(SID))
    assert_safe(failure.value)
    assert requests[0].headers['accept-encoding'] == 'identity'
    assert stream.iterations == 0 and stream.closed and responses[0].is_closed


def test_poll_http_error_closes_stream_without_consuming_its_unbounded_body():
    stream = TrackedResponseStream([PRIVATE.encode()] * 20)
    service, requests, responses = service_with_stream(stream, status_code=403)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.get_fax_status(SID))
    assert_safe(failure.value)
    assert stream.iterations == 0 and stream.closed and responses[0].is_closed
    assert len(requests) == 1


@pytest.mark.parametrize('failure_type', [asyncio.CancelledError, httpx.ReadTimeout])
def test_poll_stream_failure_is_safe_and_closes_the_response(failure_type):
    stream = TrackedResponseStream([b'{"id":'], iteration_error=failure_type(PASSWORD + ' ' + PRIVATE))
    service, requests, responses = service_with_stream(stream)
    expected = asyncio.CancelledError if failure_type is asyncio.CancelledError else RuntimeError
    with pytest.raises(expected) as failure:
        asyncio.run(service.get_fax_status(SID))
    assert_safe(failure.value)
    assert stream.closed and responses[0].is_closed and len(requests) == 1


@pytest.mark.parametrize('operation', ['create', 'poll'])
def test_overall_deadline_cancels_a_peer_that_keeps_activity_timeouts_alive(document, monkeypatch, operation):
    from api.app import interfax_service
    actual_timeout = asyncio.timeout
    deadlines = []
    requests = []
    cancelled = []

    def shortened_timeout(delay):
        deadlines.append(delay)
        return actual_timeout(0.01)

    async def handle(request):
        requests.append(request)
        try:
            await asyncio.sleep(0.03)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return httpx.Response(201, headers={'Location': '/outbound/faxes/' + SID},
            json={'id': int(SID), 'status': 0})

    monkeypatch.setattr(interfax_service.asyncio, 'timeout', shortened_timeout)
    service = service_class()(USERNAME, PASSWORD, transport=httpx.MockTransport(handle))
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document) if operation == 'create'
                    else service.get_fax_status(SID))
    assert_safe(failure.value)
    assert deadlines == [60.0 if operation == 'create' else 15.0]
    assert len(requests) == 1 and cancelled == [True]


def test_poll_overall_deadline_stops_successive_small_response_chunks(monkeypatch):
    from api.app import interfax_service
    actual_timeout = asyncio.timeout

    class SlowStatusStream(TrackedResponseStream):
        async def __aiter__(self):
            self.iterations += 1
            for _ in range(20):
                await asyncio.sleep(0.004)
                self.yielded += 1
                yield b' '

    stream = SlowStatusStream()
    service, requests, responses = service_with_stream(stream)
    monkeypatch.setattr(interfax_service.asyncio, 'timeout', lambda delay: actual_timeout(0.01))
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.get_fax_status(SID))
    assert_safe(failure.value)
    assert 0 < stream.yielded < 20
    assert stream.closed and responses[0].is_closed and len(requests) == 1


@pytest.mark.parametrize('kind', ['fifo', 'directory', 'empty'])
def test_nonregular_or_empty_descriptor_is_rejected_without_blocking_or_leaking(tmp_path, monkeypatch, kind):
    from api.app import interfax_service
    path = tmp_path / 'invalid-prepared-document'
    if kind == 'fifo':
        os.mkfifo(path)
    elif kind == 'directory':
        path.mkdir()
    else:
        path.write_bytes(b'')
    actual_os_open = os.open
    descriptors = []
    blocking_open_calls = []

    def reject_blocking_open(*args, **kwargs):
        # Prevent the unfixed implementation from actually hanging this test on a FIFO.
        blocking_open_calls.append(args)
        raise OSError(PRIVATE)

    def tracked_os_open(candidate, flags, *args, **kwargs):
        assert flags & os.O_NONBLOCK
        descriptor = actual_os_open(candidate, flags, *args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(interfax_service, 'open', reject_blocking_open, raising=False)
    monkeypatch.setattr(interfax_service.os, 'open', tracked_os_open)
    service, requests = service_with_response()
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.send_fax_file(DESTINATION, str(path)))
    assert_safe(failure.value)
    assert blocking_open_calls == [] and requests == [] and len(descriptors) == 1
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_path_replacement_with_fifo_is_checked_on_the_same_opened_descriptor(tmp_path, monkeypatch):
    from api.app import interfax_service
    path = tmp_path / 'replaced-prepared-document.pdf'
    path.write_bytes(PDF)
    actual_os_open = os.open
    descriptors = []

    def replaced_os_open(candidate, flags, *args, **kwargs):
        path.unlink()
        os.mkfifo(path)
        assert flags & os.O_NONBLOCK
        descriptor = actual_os_open(candidate, flags, *args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(interfax_service.os, 'open', replaced_os_open)
    service, requests = service_with_response()
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.send_fax_file(DESTINATION, str(path)))
    assert_safe(failure.value)
    assert requests == [] and len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_descriptor_to_file_wrapper_failure_closes_descriptor_before_any_request(document, monkeypatch):
    from api.app import interfax_service
    actual_os_open = os.open
    descriptors = []

    def tracked_os_open(*args, **kwargs):
        descriptor = actual_os_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def failed_fdopen(*args, **kwargs):
        raise OSError(PRIVATE)

    monkeypatch.setattr(interfax_service.os, 'open', tracked_os_open)
    monkeypatch.setattr(interfax_service.os, 'fdopen', failed_fdopen)
    service, requests = service_with_response()
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(service.send_fax_file(DESTINATION, document))
    assert_safe(failure.value)
    assert requests == [] and len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
