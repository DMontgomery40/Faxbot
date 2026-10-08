"""Documo v1 multipart adapter using only captured account configuration.

Official collection TzRa5ia9 (published 2025-01-03), verified 2026-10-03:
https://docs.documo.com/#4c501802-7d0b-4e70-98ef-64249d3606fb
https://docs.documo.com/#c2dc1026-15d7-42ae-ac90-05c3757635f8
Sandbox: https://help.documo.com/hc/en-us/articles/7789817420571
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import urlsplit

import httpx

from .routing.numbers import canonical_number


_PRODUCTION_ORIGIN = 'https://api.documo.com'
_SANDBOX_ORIGIN = 'https://api.sandbox.documo.com'
_UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')


def _origin(base_url: str, sandbox: bool) -> str:
    candidate = base_url or _PRODUCTION_ORIGIN
    if (any(character.isspace() or ord(character) < 33 or ord(character) == 127
            for character in candidate) or any(character in candidate for character in '\\?#')):
        raise ValueError
    parsed = urlsplit(candidate)
    if (parsed.scheme != 'https' or not parsed.hostname or '@' in parsed.netloc
            or '%' in parsed.netloc or parsed.path not in {'', '/'}
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
        raise ValueError
    # HTTPX validates the wire URL; root paths and standard ports are canonicalized.
    origin = str(httpx.URL(candidate).copy_with(path=''))
    if sandbox:
        if (parsed.hostname not in {'api.documo.com', 'api.sandbox.documo.com'}
                or parsed.port not in {None, 443}):
            raise ValueError
        return _SANDBOX_ORIGIN
    return origin


def _valid_identity(value: object) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _receipt(response: httpx.Response, *, requested_sid: str | None = None) -> dict[str, str]:
    if not 200 <= response.status_code < 300:
        raise ValueError
    payload = response.json()
    if not isinstance(payload, dict) or not _valid_identity(payload.get('messageId')):
        raise ValueError
    provider_sid = payload['messageId'].lower()
    if requested_sid is not None and provider_sid != requested_sid:
        raise ValueError
    if 'status' not in payload and requested_sid is None:
        # async=true explicitly permits a messageId-only create acknowledgement.
        status = 'in_progress'
    else:
        wire_status = payload.get('status')
        if not isinstance(wire_status, str):
            raise ValueError
        status = {'processing': 'in_progress', 'success': 'success', 'failed': 'failed'}.get(wire_status)
        if status is None:
            raise ValueError
    receipt = {'provider_sid': provider_sid, 'status': status}
    if status == 'failed':
        # Whether the call ended before any fax data, by Documo's result code (routing/predata.py).
        from .routing.predata import documo as before_fax_data
        receipt['before_fax_data'] = before_fax_data(payload.get('resultCode'))
        # Documo's own count of pages completed and in the fax (routing/continuation.py), kept as evidence.
        from .routing.continuation import documo_pages
        sent, total = documo_pages(payload)
        if sent is not None:
            receipt['pages_sent'], receipt['pages_total'] = sent, total
    return receipt


@dataclass(frozen=True, slots=True)
class DocumoFaxService:
    """A captured Documo account; one create attempt and no endpoint fallback."""

    api_key: str = field(repr=False)
    base_url: str
    sandbox: bool
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False,
        compare=False, kw_only=True)

    def __post_init__(self) -> None:
        try:
            if (not isinstance(self.api_key, str) or not isinstance(self.base_url, str)
                    or type(self.sandbox) is not bool
                    or any(not 33 <= ord(character) <= 126 for character in self.api_key)
                    or (self.transport is not None
                        and not isinstance(self.transport, httpx.AsyncBaseTransport))):
                raise ValueError
            object.__setattr__(self, 'base_url', _origin(self.base_url, self.sandbox))
        except (TypeError, ValueError, httpx.InvalidURL):
            raise ValueError('Documo configuration is invalid.') from None

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def send_fax_file(self, to_number: str, file_path: str) -> dict[str, str]:
        """Submit a prepared PDF once; an unusable reply cannot prove noncreation."""
        if not self.is_configured():
            raise ValueError('Documo is not configured.')
        try:
            to_number = canonical_number(to_number)  # Documo takes the E.164 form unchanged
        except ValueError:
            raise ValueError('Documo destination is invalid.') from None
        try:
            async with httpx.AsyncClient(timeout=60.0, transport=self.transport,
                    follow_redirects=False, trust_env=False) as client:
                with open(file_path, 'rb') as handle:
                    response = await client.post(self.base_url + '/v1/faxes',
                        headers={'Authorization': 'Basic ' + self.api_key},
                        data={'faxNumber': to_number, 'coverPage': 'false', 'async': 'true'},
                        files={'attachments': ('document.pdf', handle, 'application/pdf')})
            return _receipt(response)
        except (httpx.HTTPError, httpx.InvalidURL, OSError, TypeError, ValueError):
            raise RuntimeError('Documo create request failed.') from None

    async def get_fax_status(self, provider_sid: str) -> dict[str, str]:
        """Read the original account's fax; require matching identity and status."""
        if not self.is_configured():
            raise ValueError('Documo is not configured.')
        if not _valid_identity(provider_sid):
            raise ValueError('Documo provider identity is invalid.')
        provider_sid = provider_sid.lower()
        try:
            async with httpx.AsyncClient(timeout=15.0, transport=self.transport,
                    follow_redirects=False, trust_env=False) as client:
                response = await client.get(self.base_url + '/v1/fax/' + provider_sid + '/info',
                    headers={'Authorization': 'Basic ' + self.api_key})
            return _receipt(response, requested_sid=provider_sid)
        except (httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError):
            raise RuntimeError('Documo status request failed.') from None
