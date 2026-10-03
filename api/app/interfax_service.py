"""Captured InterFAX protocol: one mixed-PDF create and minimal status reads.

Wire contract: official InterFAX Node SDK 08a119d12a624a6e569fca2dad8080d0da93e7f5
(src/delivery.js, src/file.js, src/response-handler.js), and generated REST
FaxGetResults schema. Credentials and origin come only from this frozen instance.
The caller owns prepared-artifact size policy; this adapter streams 64 KiB chunks
with an explicit Content-Length rather than copying an entire PDF into a MIME
buffer. It introduces no provider upload limit or document capability URL.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import secrets
import stat
from urllib.parse import urlsplit

import httpx


_DEFAULT_ORIGIN = 'https://rest.interfax.net'
_MAX_ID = 9223372036854775807
_DECIMAL_ID = re.compile(r'[0-9]{1,19}')
_FAX_PATH = re.compile(r'/outbound/faxes/([0-9]{1,19})')
_CHUNK_BYTES = 64 * 1024
_MAX_STATUS_BYTES = 16 * 1024


def _safe_url(value: object) -> str:
    if (not isinstance(value, str) or not value
            or any(character.isspace() or ord(character) < 33 or ord(character) == 127
                   for character in value)
            or any(character in value for character in '\\?#')):
        raise ValueError
    return value


def _origin(value: object) -> str:
    candidate = _safe_url(value)
    parsed = urlsplit(candidate)
    if (parsed.scheme != 'https' or not parsed.hostname or '@' in parsed.netloc
            or '%' in parsed.netloc or parsed.netloc.endswith(':')
            or parsed.path not in {'', '/'}
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
        raise ValueError
    # HTTPX canonicalizes the captured host, standard port, and empty root path.
    return str(httpx.URL(candidate).copy_with(path=''))


def _identity(value: object) -> str:
    # Bound before int conversion; retain a decimal string beyond JS safe integers.
    if not isinstance(value, str) or _DECIMAL_ID.fullmatch(value) is None:
        raise ValueError
    number = int(value)
    if not 0 < number <= _MAX_ID:
        raise ValueError
    return str(number)


def _create_receipt(response: httpx.Response, origin: str) -> dict[str, str]:
    if not 200 <= response.status_code < 300:
        raise ValueError
    locations = response.headers.get_list('location')
    if len(locations) != 1:
        raise ValueError
    location = _safe_url(locations[0])
    parsed = urlsplit(location)
    if location.startswith('/'):
        if location.startswith('//') or parsed.scheme or parsed.netloc:
            raise ValueError
    elif _origin(parsed.scheme + '://' + parsed.netloc) != origin:
        raise ValueError
    match = _FAX_PATH.fullmatch(parsed.path)
    if match is None:
        raise ValueError
    # Never follow Location; only its strictly validated identity is retained.
    return {'provider_sid': _identity(match.group(1)), 'status': 'in_progress'}


def _status_receipt(payload: object, requested_sid: str) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError
    number, wire_status = payload.get('id'), payload.get('status')
    if (type(number) is not int or not 0 < number <= _MAX_ID
            or str(number) != requested_sid or type(wire_status) is not int
            or not -2147483648 <= wire_status <= 2147483647):
        raise ValueError
    if wire_status < 0:
        normalized = 'in_progress'
    elif wire_status == 0:
        normalized = 'success'
    elif wire_status == 403:
        normalized = 'cancelled'
    else:
        normalized = 'failed'
    return {'provider_sid': requested_sid, 'status': normalized}


async def _read_status(response: httpx.Response, requested_sid: str) -> dict[str, str]:
    if not 200 <= response.status_code < 300:
        raise ValueError
    encodings = response.headers.get_list('content-encoding')
    if encodings and (len(encodings) != 1 or encodings[0].strip().lower() != 'identity'):
        # Request identity encoding so decompression cannot allocate an unbounded chunk.
        raise ValueError
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > _MAX_STATUS_BYTES - len(body):
            raise ValueError
        body.extend(chunk)
    return _status_receipt(json.loads(body), requested_sid)


def _reference(job_id: object, attempt_id: object) -> str | None:
    if job_id is None and attempt_id is None:
        return None
    for value in (job_id, attempt_id):
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 128
                or any(character.isspace() or ord(character) < 33 or ord(character) == 127
                       for character in value)):
            raise ValueError
    # Correlation metadata only: repeated reference values do not deduplicate fax sends.
    digest = hashlib.sha256(((job_id or '') + '\0' + (attempt_id or '')).encode('utf-8'))
    return 'faxbot-' + digest.hexdigest()[:32]


async def _mixed_pdf(handle, size: int, prefix: bytes, suffix: bytes):
    yield prefix
    remaining = size
    while remaining:
        chunk = handle.read(min(_CHUNK_BYTES, remaining))
        if not chunk:
            raise ValueError
        remaining -= len(chunk)
        yield chunk
    yield suffix


@contextmanager
def _prepared_pdf(file_path: str):
    # Validate the opened object, not a racy pathname stat. O_NONBLOCK prevents
    # an unconnected FIFO from blocking before fstat can reject it.
    descriptor = os.open(file_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        document_stat = os.fstat(descriptor)
        if not stat.S_ISREG(document_stat.st_mode) or document_stat.st_size <= 0:
            raise ValueError
        handle = os.fdopen(descriptor, 'rb')
        descriptor = None  # fdopen now owns the descriptor, including its closure.
        with handle:
            yield handle, document_stat.st_size
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class InterfaxFaxService:
    """An immutable captured account; create outcomes never trigger automatic retries."""

    username: str = field(repr=False)
    password: str = field(repr=False)
    base_url: str = _DEFAULT_ORIGIN
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False,
        compare=False, kw_only=True)

    def __post_init__(self) -> None:
        try:
            if (not isinstance(self.username, str) or not isinstance(self.password, str)
                    or ':' in self.username
                    or any(ord(character) < 32 or ord(character) == 127
                           for credential in (self.username, self.password)
                           for character in credential)
                    or (self.transport is not None
                        and not isinstance(self.transport, httpx.AsyncBaseTransport))):
                raise ValueError
            object.__setattr__(self, 'base_url', _origin(self.base_url))
        except (TypeError, ValueError, httpx.InvalidURL):
            raise ValueError('InterFAX configuration is invalid.') from None

    def is_configured(self) -> bool:
        return bool(self.username and self.password)

    async def send_fax_file(self, to_number: str, file_path: str,
                            job_id: str | None = None, *,
                            attempt_id: str | None = None) -> dict[str, str]:
        """Submit one prepared PDF; any unusable receipt requires reconciliation."""
        if not self.is_configured():
            raise ValueError('InterFAX is not configured.')
        if (not isinstance(to_number, str) or not to_number
                or any(ord(character) < 32 or ord(character) == 127 for character in to_number)):
            raise ValueError('InterFAX destination is invalid.')
        try:
            reference = _reference(job_id, attempt_id)
        except (TypeError, ValueError):
            raise ValueError('InterFAX correlation is invalid.') from None
        params = {'faxNumber': to_number}
        if reference is not None:
            params['reference'] = reference
        try:
            if not isinstance(file_path, str) or not file_path:
                raise ValueError
            boundary = secrets.token_hex(24)
            prefix = ('--' + boundary + '\r\nContent-Type: application/pdf\r\n\r\n').encode('ascii')
            suffix = ('\r\n--' + boundary + '--').encode('ascii')
            # Overall cooperative deadline complements the HTTPX inactivity timeouts.
            async with asyncio.timeout(60.0):
                with _prepared_pdf(file_path) as (handle, document_size):
                    async with httpx.AsyncClient(auth=httpx.BasicAuth(self.username, self.password),
                            timeout=60.0, transport=self.transport, follow_redirects=False,
                            trust_env=False) as client:
                        async with client.stream('POST', self.base_url + '/outbound/faxes', params=params,
                                headers={'Content-Type': 'multipart/mixed; boundary=' + boundary,
                                    'Content-Length': str(len(prefix) + document_size + len(suffix))},
                                content=_mixed_pdf(handle, document_size, prefix, suffix)) as response:
                            # A Location acknowledgement does not depend on its unused body.
                            return _create_receipt(response, self.base_url)
        except asyncio.CancelledError:
            raise asyncio.CancelledError('InterFAX create request cancelled.') from None
        except Exception:
            # Request exceptions may carry response bodies, headers, or credentials.
            raise RuntimeError('InterFAX create request failed; outcome may be uncertain.') from None

    async def get_fax_status(self, provider_sid: str) -> dict[str, str]:
        """Read only the original captured account and require its exact numeric fax ID."""
        if not self.is_configured():
            raise ValueError('InterFAX is not configured.')
        try:
            provider_sid = _identity(provider_sid)
        except (TypeError, ValueError):
            raise ValueError('InterFAX provider identity is invalid.') from None
        try:
            async with asyncio.timeout(15.0):
                async with httpx.AsyncClient(auth=httpx.BasicAuth(self.username, self.password),
                        timeout=15.0, transport=self.transport, follow_redirects=False,
                        trust_env=False) as client:
                    async with client.stream('GET',
                            self.base_url + '/outbound/faxes/' + provider_sid + '/status',
                            headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'}) as response:
                        return await _read_status(response, provider_sid)
        except asyncio.CancelledError:
            raise asyncio.CancelledError('InterFAX status request cancelled.') from None
        except Exception:
            raise RuntimeError('InterFAX status request failed.') from None
