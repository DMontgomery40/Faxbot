"""HumbleFax QuickSendFax adapter using only captured account configuration.

Official API reference, verified 2026-10-03: https://api.humblefax.com/
QuickSendFax is ``POST /quickSendFax`` (multipart ``jsonData`` plus a file);
GetSentFax is ``GET /sentFax/{sentFaxId}``. Authentication is HTTP Basic with
the account's API access key and secret key.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import re

import httpx


_ORIGIN = 'https://api.humblefax.com'
# The API host is fronted by Cloudflare, which can refuse HTTP library agents.
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Faxbot')
_CREATE_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_STATUS_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_NANP = re.compile(r'1?[2-9][0-9]{9}', re.ASCII)
_IDENTITY = re.compile(r'[A-Za-z0-9_-]{1,45}', re.ASCII)
_SUBMISSION = re.compile(r'[A-Za-z0-9_-]{1,100}', re.ASCII)
# GetSentFax documents these summary values. Partial delivery is not success.
_STATUSES = {
    'in progress': 'in_progress', 'scheduled': 'in_progress', 'success': 'success',
    'failure': 'failed', 'image failure': 'failed', 'partial success': 'failed',
    'cancelled': 'cancelled',
}


class HumbleFaxCredentialsError(RuntimeError):
    """HumbleFax refused the captured account credentials."""

    def __init__(self) -> None:
        super().__init__('HumbleFax rejected the account access key or secret key.')


def humblefax_number(value: object) -> int:
    """Return the 11-digit integer HumbleFax accepts for a US or Canadian number."""
    if not isinstance(value, str):
        raise ValueError('HumbleFax fax number is invalid.')
    digits = value[1:] if value.startswith('+') else value
    if _NANP.fullmatch(digits) is None or (value.startswith('+') and len(digits) != 11):
        raise ValueError('HumbleFax fax number is invalid.')
    return int(digits if len(digits) == 11 else '1' + digits)


def humblefax_destination(value: object) -> int:
    """HumbleFax's eleven-digit integer for a canonical +1 destination; nothing else."""
    from .routing.numbers import InvalidNumber, canonical_number
    try:
        number = canonical_number(value)
    except InvalidNumber:
        raise ValueError('HumbleFax fax number is invalid.') from None
    if not number.startswith('+1'):
        raise ValueError('HumbleFax fax number is invalid.')
    return humblefax_number(number)


def _identity(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        value = str(value)
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError
    return value


def _status(value: object) -> str:
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, str):
        raise ValueError
    status = _STATUSES.get(value.lower())
    if status is None:
        raise ValueError
    return status


def _receipt(response: httpx.Response, *, requested_sid: str | None = None) -> dict[str, str]:
    if response.status_code == 401:
        raise HumbleFaxCredentialsError()
    if not 200 <= response.status_code < 300:
        raise ValueError
    payload = response.json()
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError
    # QuickSendFax documents data.fax; GetSentFax documents data.sentFax.
    names = [name for name in (('fax', 'sentFax') if requested_sid is None else ('sentFax',))
             if name in data]
    if len(names) != 1 or not isinstance(data[names[0]], dict):
        raise ValueError
    fax = data[names[0]]
    provider_sid = _identity(fax.get('id'))
    if requested_sid is not None and provider_sid != requested_sid:
        raise ValueError
    if requested_sid is None and fax.get('status') is None:
        # A created fax identity without a summary is queued work, never success.
        return {'provider_sid': provider_sid, 'status': 'in_progress'}
    return {'provider_sid': provider_sid, 'status': _status(fax.get('status'))}


@dataclass(frozen=True, slots=True)
class HumbleFaxFaxService:
    """A captured HumbleFax account; one create attempt and no endpoint fallback."""

    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    from_number: str = ''
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False,
        compare=False, kw_only=True)

    def __post_init__(self) -> None:
        try:
            if (not isinstance(self.access_key, str) or not isinstance(self.secret_key, str)
                    or not isinstance(self.from_number, str)
                    or any(not 33 <= ord(character) <= 126
                           for character in self.access_key + self.secret_key)
                    or ':' in self.access_key
                    or (self.transport is not None
                        and not isinstance(self.transport, httpx.AsyncBaseTransport))):
                raise ValueError
            if self.from_number:
                humblefax_number(self.from_number)
        except (TypeError, ValueError):
            raise ValueError('HumbleFax configuration is invalid.') from None

    def is_configured(self) -> bool:
        return bool(self.access_key and self.secret_key)

    def _headers(self) -> dict[str, str]:
        token = base64.b64encode((self.access_key + ':' + self.secret_key).encode('ascii')).decode('ascii')
        return {'Authorization': 'Basic ' + token, 'User-Agent': USER_AGENT, 'Accept': 'application/json'}

    async def send_fax_file(self, to_number: str, file_path: str, *,
                            uuid: str | None = None) -> dict[str, str]:
        """Submit a prepared PDF once; an unusable reply cannot prove noncreation."""
        if not self.is_configured():
            raise ValueError('HumbleFax is not configured.')
        recipient = humblefax_destination(to_number)
        if uuid is not None and (not isinstance(uuid, str) or _SUBMISSION.fullmatch(uuid) is None):
            raise ValueError('HumbleFax submission identifier is invalid.')
        # The documented coversheet default is on; Faxbot sends only the document.
        body: dict[str, object] = {'recipients': [recipient], 'includeCoversheet': False,
                                   'resolution': 'Fine', 'pageSize': 'Letter'}
        if self.from_number:
            body['fromNumber'] = humblefax_number(self.from_number)
        if uuid is not None:
            body['uuid'] = uuid
        try:
            async with httpx.AsyncClient(timeout=_CREATE_TIMEOUT, transport=self.transport,
                    follow_redirects=False, trust_env=False) as client:
                with open(file_path, 'rb') as handle:
                    response = await client.post(_ORIGIN + '/quickSendFax', headers=self._headers(),
                        data={'jsonData': json.dumps(body, separators=(',', ':'))},
                        files={'file': ('document.pdf', handle, 'application/pdf')})
            return _receipt(response)
        except HumbleFaxCredentialsError:
            raise
        except (httpx.HTTPError, httpx.InvalidURL, OSError, TypeError, ValueError):
            raise RuntimeError('HumbleFax create request failed.') from None

    async def get_fax_status(self, provider_sid: str) -> dict[str, str]:
        """Read the original account's fax; require matching identity and status."""
        if not self.is_configured():
            raise ValueError('HumbleFax is not configured.')
        if not isinstance(provider_sid, str) or _IDENTITY.fullmatch(provider_sid) is None:
            raise ValueError('HumbleFax provider identity is invalid.')
        try:
            async with httpx.AsyncClient(timeout=_STATUS_TIMEOUT, transport=self.transport,
                    follow_redirects=False, trust_env=False) as client:
                response = await client.get(_ORIGIN + '/sentFax/' + provider_sid,
                                            headers=self._headers())
            return _receipt(response, requested_sid=provider_sid)
        except HumbleFaxCredentialsError:
            raise
        except (httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError):
            raise RuntimeError('HumbleFax status request failed.') from None
