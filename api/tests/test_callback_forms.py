"""Synthetic ASGI form streams only; no application routes or HTTP calls."""
import asyncio
import importlib
import importlib.util

import pytest
from starlette.datastructures import UploadFile
from starlette.requests import Request
import starlette.formparsers


MiB = 1024 * 1024
BOUNDARY = b'synthetic-callback-boundary'


def module():
    assert importlib.util.find_spec('api.app.callback_forms') is not None, 'callback form module missing'
    return importlib.import_module('api.app.callback_forms')


def request(body=b'', *, content_type='application/x-www-form-urlencoded', chunks=None, content_length=None):
    pieces = list(chunks if chunks is not None else [body])
    received = []
    async def receive():
        index = len(received)
        received.append(index)
        return {'type': 'http.request', 'body': pieces[index], 'more_body': index < len(pieces) - 1}
    headers = [(b'content-type', content_type.encode())]
    if content_length is not None:
        headers.append((b'content-length', str(content_length).encode()))
    return Request({'type': 'http', 'method': 'POST', 'path': '/synthetic-callback', 'headers': headers}, receive), received


def multipart(parts, *, close=True):
    result = bytearray()
    for name, value, filename in parts:
        result.extend(b'--' + BOUNDARY + b'\r\nContent-Disposition: form-data; name="' + name.encode() + b'"')
        if filename is not None:
            result.extend(b'; filename="' + filename.encode() + b'"')
        result.extend(b'\r\n\r\n' + value + b'\r\n')
    if close:
        result.extend(b'--' + BOUNDARY + b'--\r\n')
    return bytes(result)


def media_type(*, charset=None):
    value = 'multipart/form-data; boundary=' + BOUNDARY.decode()
    return value if charset is None else value + '; charset=' + charset


def assert_error(req, status):
    parser = module()
    with pytest.raises(parser.CallbackFormError) as failure:
        asyncio.run(parser.read_callback_form(req))
    assert failure.value.status_code == status
    assert 'private-synthetic' not in str(failure.value)
    assert failure.value.__suppress_context__


def track_temporary_files(monkeypatch):
    original = starlette.formparsers.SpooledTemporaryFile
    created = []
    def tracked(*args, **kwargs):
        # Exercise real disk-backed temporary cleanup even for tiny fixtures.
        kwargs['max_size'] = 1
        handle = original(*args, **kwargs)
        created.append(handle)
        return handle
    monkeypatch.setattr(starlette.formparsers, 'SpooledTemporaryFile', tracked)
    return created


def test_urlencoded_preserves_duplicate_blank_and_unicode_wire_order():
    req, _ = request(chunks=[b'tag=one&blank=&tag=', b'two&bare&note=caf%C3%A9&literal=caf\xc3\xa9&=empty-name'])
    assert asyncio.run(module().read_callback_form(req)) == (
        [('tag', 'one'), ('blank', ''), ('tag', 'two'), ('bare', ''),
         ('note', 'café'), ('literal', 'café'), ('', 'empty-name')], [])


def test_urlencoded_honors_valid_declared_charset_without_replacing_bytes():
    req, _ = request(b'note=caf%E9', content_type='application/x-www-form-urlencoded; charset=iso-8859-1')
    assert asyncio.run(module().read_callback_form(req)) == ([('note', 'café')], [])


def test_empty_urlencoded_form_is_empty():
    req, _ = request()
    assert asyncio.run(module().read_callback_form(req)) == ([], [])


def test_multipart_keeps_field_names_file_bytes_and_duplicate_order_then_closes(monkeypatch):
    created = track_temporary_files(monkeypatch)
    body = multipart([('tag', b'one', None), ('attachment', b'\x00\xffPDF\r\n', 'first-private-synthetic.pdf'),
                      ('blank', b'', None), ('tag', b'two', None), ('attachment', b'', 'second.pdf')])
    req, _ = request(chunks=[body[:71], body[71:]], content_type=media_type())
    assert asyncio.run(module().read_callback_form(req)) == (
        [('tag', 'one'), ('blank', ''), ('tag', 'two')],
        [('attachment', b'\x00\xffPDF\r\n'), ('attachment', b'')])
    assert len(created) == 2 and all(handle.closed for handle in created)


@pytest.mark.parametrize('content_type', ['application/json', 'text/plain', '', 'application/octet-stream'])
def test_unknown_media_is_rejected_before_consuming_body(content_type):
    req, received = request(b'private-synthetic-payload', content_type=content_type)
    assert_error(req, 415)
    assert received == []


@pytest.mark.parametrize('body', [b'x=%GG', b'x=%A', b'x=%FF', b'x=\xff', b'%FF=value'])
def test_urlencoded_malformed_encoding_fails_closed(body):
    req, _ = request(body)
    assert_error(req, 400)


@pytest.mark.parametrize('content_type', ['application/x-www-form-urlencoded; charset=not-a-private-synthetic-codec',
                                        'multipart/form-data; boundary=synthetic-callback-boundary; charset=not-a-codec'])
def test_invalid_charset_is_safe_error(content_type):
    req, _ = request(b'private-synthetic-data', content_type=content_type)
    assert_error(req, 400)


def test_content_type_header_is_bounded_before_option_parsing_or_body_read():
    req, received = request(b'private-synthetic', content_type='multipart/form-data; boundary=' + 'x' * 9000)
    assert_error(req, 413)
    assert received == []


@pytest.mark.parametrize('kind', ['missing', 'mismatch', 'truncated', 'invalid-text'])
def test_multipart_boundary_and_encoding_errors_close_open_files(monkeypatch, kind):
    created = track_temporary_files(monkeypatch)
    body = multipart([('file', b'private-synthetic-file', 'fax.pdf')], close=kind != 'truncated')
    content_type = media_type()
    if kind == 'missing':
        content_type = 'multipart/form-data'
    elif kind == 'mismatch':
        content_type = 'multipart/form-data; boundary=different-boundary'
    elif kind == 'invalid-text':
        body = multipart([('file', b'private-synthetic-file', 'fax.pdf'), ('fax', b'\xff', None)])
    req, _ = request(body, content_type=content_type)
    assert_error(req, 400)
    assert all(handle.closed for handle in created)
    if kind in {'truncated', 'invalid-text'}:
        assert created


@pytest.mark.parametrize('declared', [None, 0, 1, 100 * MiB])
def test_actual_stream_limit_overrides_misleading_content_length(declared):
    req, received = request(chunks=[b'x' * MiB, b'y' * (MiB + 1), b'never-read'], content_length=declared)
    assert_error(req, 413)
    assert len(received) == 2


def test_urlencoded_exact_raw_limit_is_supported():
    # Two individually bounded fields, exactly 2MiB including separators/names.
    req, _ = request(b'a=' + b'x' * (MiB - 2) + b'&b=' + b'y' * (MiB - 3))
    fields, files = asyncio.run(module().read_callback_form(req))
    assert [(name, len(value)) for name, value in fields] == [('a', MiB - 2), ('b', MiB - 3)]
    assert files == []


@pytest.mark.parametrize('kind', ['urlencoded', 'multipart'])
def test_field_count_is_bounded(kind):
    if kind == 'urlencoded':
        req, _ = request(b'&'.join(b'tag=' for _ in range(101)))
    else:
        req, _ = request(multipart([('tag', b'', None)] * 101), content_type=media_type())
    assert_error(req, 413)


def test_file_count_is_bounded_and_all_created_files_close(monkeypatch):
    created = track_temporary_files(monkeypatch)
    req, _ = request(multipart([('file', b'x', 'fax.pdf')] * 5), content_type=media_type())
    assert_error(req, 413)
    assert len(created) == 4 and all(handle.closed for handle in created)


def test_multipart_exact_file_part_limit_is_supported(monkeypatch):
    created = track_temporary_files(monkeypatch)
    req, _ = request(multipart([('file', b'x' * MiB, 'fax.pdf')]), content_type=media_type())
    fields, files = asyncio.run(module().read_callback_form(req))
    assert fields == [] and [(name, len(content)) for name, content in files] == [('file', MiB)]
    assert created and all(handle.closed for handle in created)


def test_maximum_field_and_file_counts_are_supported_together(monkeypatch):
    created = track_temporary_files(monkeypatch)
    req, _ = request(multipart([('tag', b'', None)] * 100 + [('file', b'x', 'fax.pdf')] * 4),
                     content_type=media_type())
    fields, files = asyncio.run(module().read_callback_form(req))
    assert fields == [('tag', '')] * 100 and files == [('file', b'x')] * 4
    assert len(created) == 4 and all(handle.closed for handle in created)


def test_multipart_header_count_limit_is_safe_and_closes_prior_file(monkeypatch):
    created = track_temporary_files(monkeypatch)
    first = multipart([('file', b'x', 'fax.pdf')], close=False)
    extra_headers = b'X-Synthetic: value\r\n' * 8
    body = first + b'--' + BOUNDARY + b'\r\nContent-Disposition: form-data; name="tag"\r\n'
    body += extra_headers + b'\r\nx\r\n--' + BOUNDARY + b'--\r\n'
    req, _ = request(body, content_type=media_type())
    assert_error(req, 413)
    assert created and all(handle.closed for handle in created)


@pytest.mark.parametrize('kind', ['urlencoded', 'multipart-text', 'multipart-file', 'multipart-header'])
def test_part_limits_fail_before_returning_oversized_result(monkeypatch, kind):
    created = track_temporary_files(monkeypatch)
    if kind == 'urlencoded':
        req, _ = request(b'x=' + b'x' * (MiB + 1))
    else:
        filename = 'x' * 9000 if kind == 'multipart-header' else 'fax.pdf' if kind == 'multipart-file' else None
        value = b'x' if kind == 'multipart-header' else b'x' * (MiB + 1)
        req, _ = request(multipart([('value', value, filename)]), content_type=media_type())
    assert_error(req, 413)
    assert all(handle.closed for handle in created)


def test_declared_charset_expansion_cannot_exceed_decoded_result_limit():
    req, _ = request(multipart([('text', b'\xe9' * (400 * 1024), None)] * 3),
                     content_type=media_type(charset='iso-8859-1'))
    assert_error(req, 413)


@pytest.mark.parametrize('phase', ['write', 'read'])
def test_cancellation_propagates_and_closes_every_temporary_file(monkeypatch, phase):
    created = track_temporary_files(monkeypatch)
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(UploadFile, phase, cancelled)
    req, _ = request(multipart([('file', b'private-synthetic-bytes', 'fax.pdf')]), content_type=media_type())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(module().read_callback_form(req))
    assert created and all(handle.closed for handle in created)


def test_disconnect_is_sanitized_without_request_private_state():
    async def receive():
        return {'type': 'http.disconnect'}
    req = Request({'type': 'http', 'method': 'POST', 'path': '/synthetic-callback',
                   'headers': [(b'content-type', b'application/x-www-form-urlencoded')]}, receive)
    assert_error(req, 400)
