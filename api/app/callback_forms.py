"""Bounded callback forms using installed Starlette parser primitives.

Starlette 1.7.0 / python-multipart 0.0.32 were inspected for file cleanup,
callback ordering and limits. Only public Request headers/stream are used.
No authentication, request/proxy URL reconstruction, logging or storage occurs.
"""
import codecs
import re

from python_multipart.multipart import parse_options_header
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import FormParser, MultiPartParser


MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_PART_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 8192
MAX_FIELDS = 100
MAX_FILES = 4
_CHUNK_BYTES = 64 * 1024
_BAD_ESCAPE = re.compile(rb'%(?![0-9a-fA-F]{2})')


class CallbackFormError(ValueError):
    """Safe parse rejection; caller maps status_code to its HTTP response."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__({400: 'Invalid callback form.', 413: 'Callback form exceeds limits.',
                          415: 'Unsupported callback form media type.'}[status_code])


def _decode(value: bytes | bytearray, charset: str) -> str:
    return bytes(value).decode(charset, errors='strict')


class _Budget:
    def __init__(self):
        self.used = 0

    def add(self, name: str, value: str | bytes):
        name_size = len(name.encode('utf-8'))
        value_size = len(value.encode('utf-8')) if isinstance(value, str) else len(value)
        size = name_size + value_size
        if max(name_size, value_size) > MAX_PART_BYTES or self.used + size > MAX_BODY_BYTES:
            raise CallbackFormError(413) from None
        self.used += size


class _StrictFormParser(FormParser):
    """Keep the primitive's state/count handling with strict raw-value decode."""

    def __init__(self, headers, stream, charset, budget):
        super().__init__(headers, stream, max_fields=MAX_FIELDS, max_part_size=MAX_PART_BYTES)
        self.charset, self.budget = charset, budget
        self.pairs = []
        self.name, self.value = bytearray(), bytearray()

    def on_field_start(self):
        self.name, self.value = bytearray(), bytearray()
        super().on_field_start()

    def _append(self, target, data, start, end):
        if len(self.name) + len(self.value) + end - start > MAX_PART_BYTES:
            raise CallbackFormError(413) from None
        target.extend(data[start:end])

    def on_field_name(self, data, start, end):
        self._append(self.name, data, start, end)
        super().on_field_name(data, start, end)

    def on_field_data(self, data, start, end):
        self._append(self.value, data, start, end)
        super().on_field_data(data, start, end)

    def on_field_end(self):
        from urllib.parse import unquote_to_bytes
        if len(self.pairs) >= MAX_FIELDS:
            raise CallbackFormError(413) from None
        if _BAD_ESCAPE.search(self.name) or _BAD_ESCAPE.search(self.value):
            raise CallbackFormError(400) from None
        name = _decode(unquote_to_bytes(bytes(self.name).replace(b'+', b' ')), self.charset)
        value = _decode(unquote_to_bytes(bytes(self.value).replace(b'+', b' ')), self.charset)
        self.budget.add(name, value)
        self.pairs.append((name, value))
        super().on_field_end()


class _StrictMultipartParser(MultiPartParser):
    """Extend inspected parser callbacks for file/header limits and completion.

    Starlette's current-part parser internals supply raw text/part identity;
    this does not access or mutate private Request state. Every created upload
    is owned here until its public file handle is closed by our finalizer.
    """

    def __init__(self, headers, stream, charset, budget):
        super().__init__(headers, stream, max_files=MAX_FILES, max_fields=MAX_FIELDS, max_part_size=MAX_PART_BYTES)
        self.charset, self.budget = charset, budget
        self.uploads = []
        self.fields, self.files = 0, 0
        self.complete = False
        self.part_bytes, self.header_bytes = 0, 0

    def on_part_begin(self):
        self.part_bytes, self.header_bytes = 0, 0
        super().on_part_begin()

    def _header(self, start, end):
        if self.header_bytes + end - start > MAX_HEADER_BYTES:
            raise CallbackFormError(413) from None
        self.header_bytes += end - start

    def on_header_field(self, data, start, end):
        self._header(start, end)
        super().on_header_field(data, start, end)

    def on_header_value(self, data, start, end):
        self._header(start, end)
        super().on_header_value(data, start, end)

    def on_headers_finished(self):
        disposition, options = parse_options_header(self._current_part.content_disposition)
        if disposition != b'form-data' or b'name' not in options:
            raise CallbackFormError(400) from None
        name = _decode(options[b'name'], self.charset)
        if b'filename' in options:
            self.files += 1
            if self.files > MAX_FILES:
                raise CallbackFormError(413) from None
        else:
            self.fields += 1
            if self.fields > MAX_FIELDS:
                raise CallbackFormError(413) from None
        super().on_headers_finished()
        self._current_part.field_name = name
        if self._current_part.file is not None:
            self.uploads.append(self._current_part.file)

    def on_part_data(self, data, start, end):
        if self.part_bytes + end - start > MAX_PART_BYTES:
            raise CallbackFormError(413) from None
        self.part_bytes += end - start
        super().on_part_data(data, start, end)

    def on_part_end(self):
        if self._current_part.file is None:
            name = self._current_part.field_name
            value = _decode(self._current_part.data, self.charset)
            self.budget.add(name, value)
            self.items.append((name, value))
        else:
            super().on_part_end()

    def on_end(self):
        self.complete = True
        super().on_end()


async def _chunks(body):
    for offset in range(0, len(body), _CHUNK_BYTES):
        yield bytes(body[offset:offset + _CHUNK_BYTES])
    yield b''


async def read_callback_form(request) -> tuple[list[tuple[str, str]], list[tuple[str, bytes]]]:
    """Read URLencoded/multipart forms; duplicate and blank order is retained.

    Actual raw bytes, not Content-Length, govern the 2MiB limit. Fields/files
    are limited to 100/4, each part/result to 1MiB and total decoded results to
    2MiB. Multipart headers are capped at 8KiB per part. Temporary uploads are
    closed synchronously in finally, including incomplete parts/cancellation.
    """
    parser = None
    try:
        content_type = request.headers.get('content-type', '')
        if len(content_type) > MAX_HEADER_BYTES:
            raise CallbackFormError(413) from None
        media, options = parse_options_header(content_type)
        if media.lower() not in {b'application/x-www-form-urlencoded', b'multipart/form-data'}:
            raise CallbackFormError(415) from None
        charset = options.get(b'charset', b'utf-8').decode('ascii')
        codecs.lookup(charset)
        if media.lower() == b'multipart/form-data':
            boundary = options.get(b'boundary', b'')
            if (not 1 <= len(boundary) <= 70 or boundary.endswith(b' ')
                    or any(value < 32 or value >= 127 for value in boundary)):
                raise CallbackFormError(400) from None
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                raise CallbackFormError(413) from None
            body.extend(chunk)
        budget = _Budget()
        if media.lower() == b'application/x-www-form-urlencoded':
            parser = _StrictFormParser(request.headers, _chunks(body), charset, budget)
            await parser.parse()
            return parser.pairs, []
        parser = _StrictMultipartParser(request.headers, _chunks(body), charset, budget)
        form = await parser.parse()
        if not parser.complete:
            raise CallbackFormError(400) from None
        fields, files = [], []
        for name, value in form.multi_items():
            if isinstance(value, UploadFile):
                content = await value.read(MAX_PART_BYTES + 1)
                budget.add(name, content)
                files.append((name, content))
            else:
                fields.append((name, value))
        return fields, files
    except CallbackFormError:
        raise
    except Exception as error:
        # python-multipart 0.0.32 has one parse-error type for grammar and
        # limits. Starlette wraps it; these exact installed limit messages
        # distinguish 413 without exposing any original parser/input text.
        cause = error.__cause__
        if isinstance(cause, MultipartParseError) and cause.args in {
                ('Maximum header size exceeded',), ('Maximum header count exceeded',)}:
            raise CallbackFormError(413) from None
        raise CallbackFormError(400) from None
    finally:
        if isinstance(parser, _StrictMultipartParser):
            for upload in parser.uploads:
                upload.file.close()
