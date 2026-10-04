"""HumbleFax QuickSendFax adapter using only captured account configuration.

Official API reference, verified 2026-10-03: https://api.humblefax.com/
QuickSendFax is ``POST /quickSendFax`` (multipart ``jsonData`` plus a file);
GetSentFax is ``GET /sentFax/{sentFaxId}``; GetUser is ``GET /user`` (the API
user's own settings: ``assignedFaxNumber`` and ``allFaxNumbersCanAccess``, the
numbers it can send from; read 2026-10-04). Authentication is HTTP Basic with
the account's API access key and secret key.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import threading
import time

import httpx


_ORIGIN = 'https://api.humblefax.com'
# The API host is fronted by Cloudflare, which can refuse HTTP library agents.
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Faxbot')
_CREATE_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_STATUS_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
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
    """The integer HumbleFax uses for a US or Canadian number, such as 16462254444.

    HumbleFax serves only US and Canadian (+1) numbers, and its API examples send
    them as 1 plus ten digits. A saved account number without a country code is
    read as a +1 number for that reason; any other country is refused.
    """
    from .routing.numbers import InvalidNumber, normalize_number
    if not isinstance(value, str):
        raise ValueError('HumbleFax fax number is invalid.')
    try:
        canonical = normalize_number(value, country='US')
    except InvalidNumber:
        raise ValueError('HumbleFax fax number is invalid.') from None
    if not canonical.startswith('+1'):
        raise ValueError('HumbleFax fax number is invalid.')
    return int(canonical[1:])


def humblefax_destination(value: object) -> int:
    """HumbleFax's integer for a canonical +1 destination; other countries are refused."""
    from .routing.numbers import InvalidNumber, canonical_number
    try:
        number = canonical_number(value)
    except InvalidNumber:
        raise ValueError('HumbleFax fax number is invalid.') from None
    if not number.startswith('+1'):
        raise ValueError('HumbleFax fax number is invalid.')
    return humblefax_number(number)


def _account_number(value: object) -> str | None:
    """A number HumbleFax reports (int or text, such as 12015554444) in international form."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return '+' + str(humblefax_number(str(value)))
    except ValueError:
        return None


def _user_numbers(response: httpx.Response) -> tuple[str, ...]:
    """GetUser's numbers: the assigned one first, then every other number the user can send from."""
    if response.status_code == 401:
        raise HumbleFaxCredentialsError()
    if not 200 <= response.status_code < 300:
        raise ValueError
    payload = response.json()
    user = (payload.get('data') or {}).get('user') if isinstance(payload, dict) else None
    if not isinstance(user, dict):
        raise ValueError
    reported = [user.get('assignedFaxNumber'), *(user.get('allFaxNumbersCanAccess') or [])]
    numbers = []
    for value in reported:
        number = _account_number(value)
        if number and number not in numbers:
            numbers.append(number)
    return tuple(numbers)


# The account's own numbers, read with GetUser outside any send and kept an hour
# (five minutes after a failed read), keyed by a digest of the keys, never the keys.
_NUMBERS_SECONDS, _RETRY_SECONDS = 3600, 300
_numbers_lock = threading.Lock()
_numbers: dict[str, tuple[float, tuple[str, ...] | None]] = {}
_reading: dict[str, threading.Thread] = {}
# Tests give a fake transport here; under the test harness no real request is ever made.
NUMBERS_TRANSPORT: httpx.AsyncBaseTransport | None = None


def _account(access_key: str, secret_key: str) -> str:
    return hashlib.sha256((access_key + ':' + secret_key).encode('utf-8')).hexdigest()


def _read_numbers(account: str, access_key: str, secret_key: str) -> None:
    try:
        service = HumbleFaxFaxService(access_key, secret_key, transport=NUMBERS_TRANSPORT)
        found: tuple[str, ...] | None = asyncio.run(service.account_numbers())
    except Exception:
        found = None
    with _numbers_lock:
        known = _numbers.get(account)
        if found is None and known and known[1]:
            found = known[1]  # a failed read keeps what Faxbot already knew
        _numbers[account] = (time.monotonic(), found)


def account_numbers(access_key: str, secret_key: str, *, wait: float = 3.0) -> tuple[str, ...] | None:
    """The HumbleFax account's own fax numbers, or None while Faxbot does not know them yet.

    Read-only and cached: a read starts in the background when nothing fresh is
    known, and the first caller waits up to ``wait`` seconds for it. Never called
    while sending.
    """
    if not access_key or not secret_key:
        return None
    if NUMBERS_TRANSPORT is None and os.environ.get('FAXBOT_TEST_MODE', '').lower() in {'1', 'true', 'yes'}:
        return None
    account = _account(access_key, secret_key)
    with _numbers_lock:
        known = _numbers.get(account)
        fresh = known is not None and time.monotonic() - known[0] < (
            _NUMBERS_SECONDS if known[1] else _RETRY_SECONDS)
        reader = _reading.get(account)
        if not fresh and (reader is None or not reader.is_alive()):
            reader = threading.Thread(target=_read_numbers, args=(account, access_key, secret_key),
                                      name='faxbot-humblefax-numbers', daemon=True)
            _reading[account] = reader
            reader.start()
    if known is None and reader is not None:
        reader.join(wait)
        with _numbers_lock:
            known = _numbers.get(account)
    return known[1] if known else None


def remember_sending_number(access_key: str, secret_key: str, number: object) -> None:
    """Add the number a sent fax went from (GetSentFax's fromNumber) to the account's known numbers."""
    found = _account_number(number)
    if not found or not access_key or not secret_key:
        return
    account = _account(access_key, secret_key)
    with _numbers_lock:
        known = _numbers.get(account)
        numbers = tuple(known[1] or ()) if known else ()
        if found not in numbers:
            _numbers[account] = (known[0] if known else 0.0, (*numbers, found))


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
    receipt = {'provider_sid': provider_sid, 'status': _status(fax.get('status'))}
    sender = _account_number(fax.get('fromNumber'))
    if sender:
        receipt['from_number'] = sender
    return receipt


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
            receipt = _receipt(response, requested_sid=provider_sid)
        except HumbleFaxCredentialsError:
            raise
        except (httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError):
            raise RuntimeError('HumbleFax status request failed.') from None
        if receipt.get('from_number'):
            remember_sending_number(self.access_key, self.secret_key, receipt.pop('from_number'))
        return receipt

    async def account_numbers(self) -> tuple[str, ...]:
        """The numbers this API user can send from (GetUser), assigned number first. Read-only."""
        if not self.is_configured():
            raise ValueError('HumbleFax is not configured.')
        try:
            async with httpx.AsyncClient(timeout=_STATUS_TIMEOUT, transport=self.transport,
                    follow_redirects=False, trust_env=False) as client:
                response = await client.get(_ORIGIN + '/user', headers=self._headers())
            return _user_numbers(response)
        except HumbleFaxCredentialsError:
            raise
        except (httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError):
            raise RuntimeError('HumbleFax user request failed.') from None
