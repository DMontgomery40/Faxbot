"""HumbleFax QuickSendFax adapter using only captured account configuration.

Official API reference, verified 2026-10-03: https://api.humblefax.com/
QuickSendFax is ``POST /quickSendFax`` (multipart ``jsonData`` plus a file);
GetSentFax is ``GET /sentFax/{sentFaxId}``; GetUser is ``GET /user`` (the API
user's own settings: ``id``, ``inboundAccess``, ``assignedFaxNumber`` and
``allFaxNumbersCanAccess``, the numbers it can send from; read 2026-10-04 and
2026-10-07). Authentication is HTTP Basic with the account's API access key and
secret key.

Receiving (read 2026-10-07 from the same reference) uses three read calls and
nothing else: GetIncomingFaxes ``GET /incomingFaxes?timeFrom&timeTo`` (Unix
seconds; the last 30 days by default; no paging; ``data.incomingFaxIds`` and
``data.incomingFaxes`` with ``id``, ``status`` ("success" or "partial fax
received"), ``time``, ``toNumber``, ``fromNumber``, ``numPages``),
GetIncomingFax ``GET /incomingFax/{id}`` (``data.incomingFax``) and
DownloadIncomingFax ``GET /incomingFax/{id}/download?fileFormat=pdf`` (the
document bytes). HumbleFax has no "mark as read" call; DeleteIncomingFax exists
and Faxbot never calls it, so every received fax stays in the HumbleFax account.
HumbleFax blocks an address for 60 seconds after more than 5 requests in a
second, so the receiving calls are spaced ``RECEIVE_GAP`` apart.
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


class HumbleFaxError(RuntimeError):
    """A receiving request failed; the message is one plain sentence without keys or addresses."""


class HumbleFaxBusy(HumbleFaxError):
    """HumbleFax asked Faxbot to slow down (HTTP 429); it blocks an address for 60 seconds."""

    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__('HumbleFax asked Faxbot to slow down; Faxbot will check again shortly.')
        self.retry_after = max(60, retry_after or 0)


class HumbleFaxNotFound(HumbleFaxError):
    """HumbleFax has no received fax with this ID for these keys."""


# At most two receiving requests a second, well under HumbleFax's five, so sending keeps its share.
RECEIVE_GAP = 0.5
_RECEIVE_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_DOWNLOAD_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
_MAX_LISTING_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
_REDIRECTS = 3
# Tests give a fake transport here for the receiving calls.
RECEIVE_TRANSPORT: httpx.AsyncBaseTransport | None = None


class Pace:
    """Spaces requests at least ``gap`` seconds apart across every thread and event loop."""

    def __init__(self, gap: float, *, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        self.gap, self.clock, self.sleep = gap, clock, sleep
        self._lock = threading.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        with self._lock:
            now = self.clock()
            slot = max(now, self._next)
            self._next = slot + self.gap
        if slot > now:
            await self.sleep(slot - now)


RECEIVE_PACE = Pace(RECEIVE_GAP)


def _retry_after(response: httpx.Response) -> int | None:
    value = (response.headers.get('retry-after') or '').strip()
    return int(value) if value.isdigit() else None


def _incoming(fax: object) -> dict | None:
    """One listed or fetched incoming fax with a usable ID, numbers in international form."""
    if not isinstance(fax, dict):
        return None
    try:
        fax_id = _identity(fax.get('id'))
    except ValueError:
        return None
    pages = fax.get('numPages')
    if isinstance(pages, str) and pages.strip().isdigit():
        pages = int(pages.strip())
    status = fax.get('status')
    return {'id': fax_id, 'status': status.strip().lower()[:45] if isinstance(status, str) else None,
            'time': fax.get('time') if isinstance(fax.get('time'), (int, str)) and not isinstance(
                fax.get('time'), bool) else None,
            'to_number': _account_number(fax.get('toNumber')), 'from_number': _account_number(fax.get('fromNumber')),
            'pages': pages if isinstance(pages, int) and not isinstance(pages, bool) and 0 <= pages <= 100000 else None,
            'transmission_seconds': fax.get('transmissionTime') if isinstance(fax.get('transmissionTime'), int)
            and not isinstance(fax.get('transmissionTime'), bool) else None,
            'bit_rate': str(fax.get('bitRate'))[:10] if isinstance(fax.get('bitRate'), (int, str))
            and not isinstance(fax.get('bitRate'), bool) else None,
            'sender_station': fax.get('fromNameIdentity')[:245] if isinstance(fax.get('fromNameIdentity'), str)
            else None}


def _same_origin(location: str) -> str | None:
    """A redirect HumbleFax's API gives to itself (https://api.humblefax.com/...); anything else is None."""
    try:
        target = httpx.URL(_ORIGIN + '/').join(location)
    except (httpx.InvalidURL, TypeError, ValueError):
        return None
    if target.scheme != 'https' or target.host != 'api.humblefax.com' or target.port not in (None, 443) \
            or target.userinfo:
        return None
    return str(target)


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


# HumbleFax's own words for a failed call, read on 2026-10-04 from GetSentFax recipients[].failureReason
# ("No fax machine detected at destination"), turned into Faxbot's sentences; never shown as provider text.
_FAILURES = (
    (('no fax machine', 'no fax tone', 'not a fax', 'voice answered'), 'No fax machine answered at that number.'),
    (('busy',), 'The number was busy each time HumbleFax called.'),
    (('no answer', 'not answer', 'unanswered'), 'Nobody answered the call.'),
    (('invalid', 'not in service', 'disconnected', 'unallocated'), 'The number could not be reached.'),
)


# Every sentence _failure_sentence can give; delivery history shows a reason only from this set.
FAILURE_SENTENCES = frozenset({sentence for _, sentence in _FAILURES} | {
    'HumbleFax sent only some of the pages.', 'HumbleFax could not turn the document into fax pages.',
    'HumbleFax could not deliver the fax.'})


PARTLY_SENT = 'HumbleFax sent only some of the pages.'


def _failure_sentence(fax: dict) -> str:
    """One plain sentence for why HumbleFax could not deliver this fax."""
    status = str(fax.get('status') or '').strip().lower()
    if status == 'partial success':
        return 'HumbleFax sent only some of the pages.'
    if status == 'image failure':
        return 'HumbleFax could not turn the document into fax pages.'
    reasons = ' '.join(str(recipient.get('failureReason') or recipient.get('error') or '')
                       for recipient in fax.get('recipients') or [] if isinstance(recipient, dict)).lower()
    return next((sentence for words, sentence in _FAILURES if any(word in reasons for word in words)),
                'HumbleFax could not deliver the fax.')


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
    if receipt['status'] == 'failed':
        receipt['failure'] = _failure_sentence(fax)
        # Whether the call ended before any fax data, by HumbleFax's documented fields (routing/predata.py).
        from .routing.predata import humblefax as before_fax_data
        receipt['before_fax_data'] = before_fax_data(fax)
        # The most pages any one HumbleFax attempt sent (routing/continuation.py), kept as evidence.
        from .routing.continuation import humblefax_pages
        sent, _ = humblefax_pages(fax)
        if sent is not None:
            receipt['pages_sent'] = sent
        if str(fax.get('status') or '').strip().lower() == 'partial success' or sent:
            # Pages reached the fax machine before it failed: never sent again whole by itself; a person decides.
            receipt['failure'], receipt['failure_category'] = PARTLY_SENT, 'partly_sent'
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

    # Receiving: read, list and download only ------------------------------------
    async def _receive(self, path: str, *, params: dict | None = None, limit: int,
                       timeout: httpx.Timeout = _RECEIVE_TIMEOUT, what: str) -> tuple[int, bytes]:
        """One paced GET to HumbleFax's API host, following only redirects to that same host."""
        if not self.is_configured():
            raise HumbleFaxError('Add the HumbleFax access key and secret key in settings.')
        url, query = _ORIGIN + path, params
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport or RECEIVE_TRANSPORT,
                    follow_redirects=False, trust_env=False) as client:
                for _ in range(_REDIRECTS + 1):
                    await RECEIVE_PACE.wait()
                    async with client.stream('GET', url, params=query, headers=self._headers()) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            url, query = _same_origin(response.headers.get('location') or ''), None
                            if url is None:
                                raise HumbleFaxError(f'HumbleFax sent {what} from another address, which '
                                                     'Faxbot does not follow.')
                            continue
                        if response.status_code == 401:
                            raise HumbleFaxCredentialsError()
                        if response.status_code == 429:
                            raise HumbleFaxBusy(_retry_after(response))
                        declared = response.headers.get('content-length') or ''
                        if declared.isdigit() and int(declared) > limit:
                            raise HumbleFaxError(f'{what.capitalize()} from HumbleFax is larger than Faxbot accepts.')
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > limit:
                                raise HumbleFaxError(f'{what.capitalize()} from HumbleFax is larger than Faxbot accepts.')
                        return response.status_code, bytes(body)
        except (HumbleFaxCredentialsError, HumbleFaxError):
            raise
        except (httpx.HTTPError, httpx.InvalidURL, OSError):
            raise HumbleFaxError('Faxbot could not reach HumbleFax.') from None
        raise HumbleFaxError(f'HumbleFax redirected {what} too many times.')

    @staticmethod
    def _data(status: int, body: bytes) -> dict | None:
        if not 200 <= status < 300:
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        data = payload.get('data') if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else None

    async def user(self) -> dict:
        """This API user's ID and whether it may see received faxes (GetUser). Read-only."""
        status, body = await self._receive('/user', limit=_MAX_LISTING_BYTES // 8, what='the answer')
        user = (self._data(status, body) or {}).get('user')
        if not isinstance(user, dict):
            user = {}
        try:
            identity = _identity(user.get('id'))
        except ValueError:
            raise HumbleFaxError('HumbleFax did not say which user these keys belong to.') from None
        access = user.get('inboundAccess')
        return {'id': identity, 'inbound_access': access if isinstance(access, bool) else None}

    async def list_received(self, *, time_from: int | None = None, time_to: int | None = None) -> list[dict]:
        """The received faxes HumbleFax lists for the window (GetIncomingFaxes), each with a usable ID.

        An ID listed without its details comes back as ``{'id': ...}`` alone; GetIncomingFax reads it.
        """
        params = {name: str(value) for name, value in (('timeFrom', time_from), ('timeTo', time_to))
                  if value is not None}
        status, body = await self._receive('/incomingFaxes', params=params or None, limit=_MAX_LISTING_BYTES,
                                           what='the list of received faxes')
        data = self._data(status, body)
        if data is None:
            raise HumbleFaxError('HumbleFax did not list received faxes.')
        found: dict[str, dict] = {}
        details = data.get('incomingFaxes') if isinstance(data.get('incomingFaxes'), list) else []
        for fax in details:
            item = _incoming(fax)
            if item is not None and item['id'] not in found:
                found[item['id']] = item
        identities = data.get('incomingFaxIds') if isinstance(data.get('incomingFaxIds'), list) else []
        for value in identities:
            try:
                fax_id = _identity(value)
            except ValueError:
                continue
            found.setdefault(fax_id, {'id': fax_id})
        return list(found.values())

    async def get_received(self, fax_id: str) -> dict:
        """One received fax's details (GetIncomingFax); HumbleFaxNotFound when these keys cannot see it."""
        sid = _identity(fax_id)
        status, body = await self._receive('/incomingFax/' + sid, limit=_MAX_LISTING_BYTES // 8, what='the answer')
        if status in (403, 404):
            raise HumbleFaxNotFound('HumbleFax has no received fax with this ID for the keys in settings.')
        item = _incoming((self._data(status, body) or {}).get('incomingFax'))
        if item is None or item['id'] != sid:
            raise HumbleFaxError('HumbleFax did not describe this received fax.')
        return item

    async def download_received(self, fax_id: str) -> bytes:
        """The received document as PDF (DownloadIncomingFax), bounded to 50 MB."""
        sid = _identity(fax_id)
        status, body = await self._receive('/incomingFax/' + sid + '/download', params={'fileFormat': 'pdf'},
                                           limit=MAX_DOCUMENT_BYTES, timeout=_DOWNLOAD_TIMEOUT, what='the document')
        if status in (403, 404):
            raise HumbleFaxNotFound('HumbleFax has no received fax with this ID for the keys in settings.')
        if status != 200:
            raise HumbleFaxError(f'HumbleFax did not send the document (HTTP {status}).')
        if not body:
            raise HumbleFaxError('HumbleFax sent an empty document.')
        return body
