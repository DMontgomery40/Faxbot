"""eFax Enterprise API (sold as eFax Corporate) adapter for sending and receiving faxes.

Sources, read 2026-10-03:

- eFax Corporate Fax Services API specification (Swagger 2.0, version branch v1.2),
  shown at https://consensus.stoplight.io/docs/fax-services and exported from
  https://stoplight.io/api/v1/projects/consensus/fax-services/nodes/reference/fax-services.oas2.yml
- Getting-started pages of the eFax Enterprise API portal:
  https://docs.efaxcorporate.com/API/content/get_started/oauth.htm (credentials),
  .../inbound.htm (receiving), .../outbound.htm (sent fax status),
  .../cancel_a_fax.htm and .../production_faxing_system_characteristics.htm
- eFax's Postman collection: https://www.postman.com/collections/268299a822d69a8a05c5

What this module relies on from those sources:

- One API host, ``https://api.securedocex.com``; no regional host is documented,
  so Faxbot talks to no other address.
- ``POST /tokens`` with HTTP Basic (app ID as user name, API key as password) and
  the form body ``grant_type=client_credentials`` answers ``access_token`` and
  ``expires_in``. Tokens last 24 hours and asking for a new one ends the
  previous one, so one token is shared by every request in this process and
  replaced only when it runs out or eFax answers 401.
- Every other call carries ``Authorization: Bearer`` and the ``user-id`` header;
  ``GET /health`` needs neither.
- ``POST /faxes`` answers 201 with ``[{fax_id, destination_fax_number}]``, one
  entry per destination. Faxbot sends one destination per fax. North American
  numbers are written as 1 plus ten digits, other numbers with a leading +.
- ``GET /faxes/{fax_id}/transmission-details`` gives the live
  ``transmission_status`` (NEW, INPROGRESS, COMPLETE, ERROR, CANCELED).
- ``POST /faxes/{fax_id}/cancel`` answers 202 (the request was logged; it does
  not prove the fax stopped) or 409 (the fax already finished).
- ``GET /faxes/received`` lists received faxes, at most 100 per call, paged with
  the ``pagination-offset`` and ``pagination-limit`` headers; ``image_downloaded=false``
  lists only faxes not downloaded yet.
- ``GET /faxes/{fax_id}/image?desired_format=PDF`` answers ``{fax_id, file_name,
  image}`` with the document in base64, and marks the fax downloaded.
- ``PATCH /faxes/{fax_id}/metadata`` with ``{"image_downloaded": true}``;
  ``DELETE /faxes/{fax_id}`` answers 204.

Not verified against a live eFax account: every request above was built from the
published specification and tested against a fake that speaks those shapes.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import json
import re
import time
import weakref

import httpx


ORIGIN = 'https://api.securedocex.com'
HOSTS = frozenset({'api.securedocex.com'})
_TOKEN_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_CREATE_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_READ_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_IMAGE_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# eFax refuses documents above 100 MB once base64 encoded.
MAX_ENCODED_BYTES = 100 * 1024 * 1024
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
_MAX_IMAGE_REPLY = MAX_DOCUMENT_BYTES * 4 // 3 + 64 * 1024
_MAX_JSON_REPLY = 1024 * 1024
PAGE_LIMIT = 100
# Refresh a token this long before eFax says it ends.
_TOKEN_MARGIN = 300.0
_TOKEN_FALLBACK_SECONDS = 3600.0
_FAX_ID = re.compile(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}', re.ASCII)
_REFERENCE = re.compile(r'[A-Za-z0-9_-]{1,100}', re.ASCII)
_CSID = re.compile(r'[ -~]{0,20}', re.ASCII)
# transmission_status after removing spaces and underscores; the specification
# spells INPROGRESS once as INPROGESS and the portal adds PENDING RETRY.
_STATUSES = {
    'NEW': 'in_progress', 'INPROGRESS': 'in_progress', 'INPROGESS': 'in_progress',
    'PENDINGRETRY': 'in_progress', 'COMPLETE': 'success', 'COMPLETED': 'success',
    'ERROR': 'failed', 'CANCELED': 'cancelled', 'CANCELLED': 'cancelled',
}

# Tests may replace the clock; production reads the monotonic clock.
_clock = time.monotonic
# (app ID, SHA-256 of the API key) -> (token, monotonic time it stops being used).
_TOKENS: dict[tuple[str, str], tuple[str, float]] = {}
_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# Tests replace this with an ``httpx.MockTransport``; production uses the network.
_TRANSPORT = None


class EfaxError(RuntimeError):
    """A plain sentence; never includes credentials, tokens, addresses or document bytes."""


class EfaxCredentialsError(EfaxError):
    def __init__(self) -> None:
        super().__init__('eFax refused the app ID, API key or user ID.')


class EfaxNotFound(EfaxError):
    pass


class EfaxBusy(EfaxError):
    """eFax asked Faxbot to slow down (HTTP 429)."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__('eFax asked Faxbot to wait before asking again.')
        self.retry_after = retry_after


def clear_tokens() -> None:
    """Forget every cached token (tests, or after credentials change)."""
    _TOKENS.clear()


# One received fax's copy at eFax, when Faxbot could not delete it (see inbound/efax.py).
PENDING_DELETION_NOTE = 'Still stored at eFax; Faxbot will try again to delete it.'
STOPPED_DELETION_NOTE = 'Faxbot stopped trying to delete this fax from eFax; delete it in your eFax account.'


def deletion_sentences(pending: int, stopped: int) -> list[str]:
    """Plain sentences for the eFax section and the command line; none when nothing is left at eFax."""
    sentences = []
    if pending:
        sentences.append('1 received fax is still stored at eFax; Faxbot will try again to delete it.' if pending == 1
                         else f'{pending} received faxes are still stored at eFax; Faxbot will try again to delete them.')
    if stopped:
        sentences.append('Faxbot stopped trying to delete 1 received fax from eFax; delete it in your eFax account.'
                         if stopped == 1 else f'Faxbot stopped trying to delete {stopped} received faxes from eFax; '
                         'delete them in your eFax account.')
    return sentences


def efax_destination(value: object) -> str:
    """eFax's dialable form of a number: 1 plus ten digits for +1, otherwise + and digits."""
    from .routing.numbers import InvalidNumber, canonical_number
    try:
        number = canonical_number(value)
    except InvalidNumber:
        raise ValueError('eFax fax number is invalid.') from None
    return number[1:] if number.startswith('+1') else number


def _caller_digits(value: str) -> str:
    """custom_CallerID: digits only, with the leading 1 for North America."""
    return value[1:] if value.startswith('+') else value


def account_key(app_id: str, user_id: str) -> str:
    """The value whose digest names this eFax account in received-fax records."""
    return app_id + '\x00' + user_id


def _printable(value: object, *, limit: int = 256, colon: bool = True) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= limit
            and all(33 <= ord(character) <= 126 for character in value)
            and (colon or ':' not in value))


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = _LOCKS[loop] = asyncio.Lock()
    return lock


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get('retry-after', '').strip()
    if value.isdigit():
        return min(float(value), 3600.0)
    return None


def _fax_id(value: object) -> str:
    if not isinstance(value, str) or _FAX_ID.fullmatch(value) is None:
        raise ValueError
    return value.lower()


def _digits(value: object) -> str:
    return ''.join(character for character in str(value or '') if character.isdigit())


def _same_number(reported: object, sent: str) -> bool:
    """eFax may echo a North American number with or without its leading 1."""
    got, wanted = _digits(reported), _digits(sent)
    if not got:
        return False
    return got == wanted or (len(got) >= 10 and got[-10:] == wanted[-10:] and wanted.endswith(got))


def transmission_status(value: object) -> str:
    """Faxbot's state for an eFax transmission_status; anything unknown is refused."""
    if not isinstance(value, str):
        raise ValueError('Unusable eFax status.')
    status = _STATUSES.get(value.upper().replace(' ', '').replace('_', ''))
    if status is None:
        raise ValueError('Unusable eFax status.')
    return status


async def _bounded(response: httpx.Response, limit: int) -> bytes:
    declared = response.headers.get('content-length')
    if declared and declared.isdigit() and int(declared) > limit:
        raise EfaxError('eFax sent more data than Faxbot accepts.')
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            raise EfaxError('eFax sent more data than Faxbot accepts.')
    return bytes(body)


def _json(body: bytes) -> object:
    try:
        return json.loads(body) if body else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class EfaxFaxService:
    """A captured eFax account: one create attempt per fax, one fixed API host."""

    app_id: str = field(repr=False)
    api_key: str = field(repr=False)
    user_id: str = field(repr=False)
    caller_id: str = ''
    csid: str = ''
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False, compare=False, kw_only=True)

    def __post_init__(self) -> None:
        try:
            for value in (self.app_id, self.api_key, self.user_id):
                if not isinstance(value, str) or (value and not _printable(value)):
                    raise ValueError
            if self.app_id and ':' in self.app_id:
                raise ValueError
            if not isinstance(self.caller_id, str) or not isinstance(self.csid, str):
                raise ValueError
            if self.caller_id and re.fullmatch(r'\+?[1-9][0-9]{6,14}', self.caller_id, re.ASCII) is None:
                raise ValueError
            if _CSID.fullmatch(self.csid) is None:
                raise ValueError
            if self.transport is not None and not isinstance(self.transport, httpx.AsyncBaseTransport):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError('eFax configuration is invalid.') from None

    def is_configured(self) -> bool:
        return bool(self.app_id and self.api_key and self.user_id)

    def account(self) -> str:
        return account_key(self.app_id, self.user_id)

    def _client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self.transport or _TRANSPORT, follow_redirects=False,
                                 trust_env=False)

    def _cache_key(self) -> tuple[str, str]:
        return self.app_id, hashlib.sha256(self.api_key.encode('utf-8')).hexdigest()

    # Tokens -----------------------------------------------------------------
    async def _token(self, *, replace: str | None = None) -> str:
        """The shared bearer token; ``replace`` names one eFax just refused."""
        if not self.is_configured():
            raise ValueError('eFax is not configured.')
        key = self._cache_key()
        async with _lock():
            cached = _TOKENS.get(key)
            if cached is not None and cached[0] != replace and cached[1] > _clock():
                return cached[0]
            _TOKENS.pop(key, None)
            try:
                async with self._client(_TOKEN_TIMEOUT) as client:
                    response = await client.post(ORIGIN + '/tokens', auth=(self.app_id, self.api_key),
                        headers={'Accept': 'application/json',
                                 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'},
                        content=b'grant_type=client_credentials')
                    body = await _bounded(response, _MAX_JSON_REPLY)
            except EfaxError:
                raise
            except (httpx.HTTPError, httpx.InvalidURL):
                raise EfaxError('Faxbot could not reach eFax.') from None
            if response.status_code in (400, 401, 403):
                raise EfaxCredentialsError()
            if response.status_code == 429:
                raise EfaxBusy(_retry_after(response))
            payload = _json(body)
            token = payload.get('access_token') if isinstance(payload, dict) else None
            if (response.status_code != 200 or not isinstance(token, str) or not 0 < len(token) <= 8192
                    or any(not 33 <= ord(character) <= 126 for character in token)):
                raise EfaxError('eFax did not issue a sign-in token.')
            lifetime = payload.get('expires_in')
            if isinstance(lifetime, str) and lifetime.strip().isdigit():
                lifetime = int(lifetime.strip())
            if type(lifetime) is not int or lifetime <= _TOKEN_MARGIN:
                lifetime = _TOKEN_FALLBACK_SECONDS + _TOKEN_MARGIN
            _TOKENS[key] = (token, _clock() + lifetime - _TOKEN_MARGIN)
            return token

    async def authenticate(self) -> None:
        """Have a usable token before work starts; raises with a plain sentence."""
        await self._token()

    def _headers(self, token: str, *, body: bool = False, reference: str | None = None) -> dict[str, str]:
        headers = {'Authorization': 'Bearer ' + token, 'user-id': self.user_id, 'Accept': 'application/json'}
        if body:
            headers['Content-Type'] = 'application/json'
        if reference:
            headers['transaction-id'] = reference
        return headers

    async def _request(self, method: str, path: str, *, timeout: httpx.Timeout, limit: int = _MAX_JSON_REPLY,
                       payload: object = None, params: dict | None = None,
                       extra: dict[str, str] | None = None,
                       reference: str | None = None) -> tuple[httpx.Response, bytes]:
        """One call; a 401 replaces the token and repeats the call exactly once.

        eFax answers 401 at sign-in, before it reads the request, so the
        repeated call is the first one eFax acts on.
        """
        token = await self._token()
        content = None if payload is None else json.dumps(payload, separators=(',', ':')).encode('utf-8')
        for attempt in (0, 1):
            headers = self._headers(token, body=content is not None or method == 'POST', reference=reference)
            headers.update(extra or {})
            async with self._client(timeout) as client:
                async with client.stream(method, ORIGIN + path, headers=headers, params=params,
                                         content=content) as response:
                    if response.status_code == 401 and attempt == 0:
                        await response.aclose()
                        token = await self._token(replace=token)
                        continue
                    body = await _bounded(response, limit)
            if response.status_code == 401:
                raise EfaxCredentialsError()
            if response.status_code == 429:
                raise EfaxBusy(_retry_after(response))
            return response, body
        raise EfaxCredentialsError()  # pragma: no cover - the loop always returns or raises

    # Sending ------------------------------------------------------------------
    async def send_fax_file(self, to_number: str, file_path: str, *,
                            reference: str | None = None) -> dict[str, str]:
        """Submit one prepared PDF to one destination; an unusable reply cannot prove noncreation."""
        if not self.is_configured():
            raise ValueError('eFax is not configured.')
        destination = efax_destination(to_number)
        if reference is not None and (not isinstance(reference, str) or _REFERENCE.fullmatch(reference) is None):
            raise ValueError('eFax reference is invalid.')
        with open(file_path, 'rb') as handle:
            encoded = base64.b64encode(handle.read()).decode('ascii')
        if len(encoded) > MAX_ENCODED_BYTES:
            raise ValueError('eFax accepts documents up to 100 MB.')
        options: dict[str, object] = {'image_resolution': 'FINE', 'include_cover_page': False}
        if self.caller_id:
            options['custom_CallerID'] = _caller_digits(self.caller_id)
        if self.csid:
            options['custom_CSID'] = self.csid
        body: dict[str, object] = {
            'destinations': [{'fax_number': destination}],
            'documents': [{'document_type': 'PDF', 'document_content': encoded}],
            'fax_options': options,
        }
        if reference is not None:
            # Lets a person find this fax in eFax by Faxbot's attempt; eFax does not use it to refuse repeats.
            body['client_data'] = {'client_reference_id': reference}
        try:
            response, raw = await self._request('POST', '/faxes', timeout=_CREATE_TIMEOUT, payload=body,
                                                reference=reference)
            if not 200 <= response.status_code < 300:
                raise ValueError
            payload = _json(raw)
            if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
                raise ValueError
            entry = payload[0]
            provider_sid = _fax_id(entry.get('fax_id'))
            reported = entry.get('destination_fax_number')
            if reported is not None and not _same_number(reported, destination):
                raise ValueError
            # A created fax is queued work, never success.
            return {'provider_sid': provider_sid, 'status': 'in_progress'}
        except EfaxCredentialsError:
            raise
        except (EfaxError, httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError):
            raise EfaxError('eFax create request failed.') from None

    async def get_fax_status(self, provider_sid: str) -> dict[str, str]:
        """The live transmission status of one sent fax in the original account."""
        if not self.is_configured():
            raise ValueError('eFax is not configured.')
        try:
            sid = _fax_id(provider_sid)
        except ValueError:
            raise ValueError('eFax provider identity is invalid.') from None
        try:
            response, raw = await self._request('GET', f'/faxes/{sid}/transmission-details',
                                                timeout=_READ_TIMEOUT)
            if response.status_code != 200:
                raise ValueError
            payload = _json(raw)
            if not isinstance(payload, dict):
                raise ValueError
            if payload.get('fax_id') is not None and _fax_id(payload.get('fax_id')) != sid:
                raise ValueError
            data = payload.get('transmission_data')
            if not isinstance(data, dict):
                raise ValueError
            return {'provider_sid': sid, 'status': transmission_status(data.get('transmission_status'))}
        except EfaxCredentialsError:
            raise
        except (EfaxError, httpx.HTTPError, httpx.InvalidURL, TypeError, ValueError):
            raise EfaxError('eFax status request failed.') from None

    async def cancel_fax(self, provider_sid: str) -> str:
        """Ask eFax to stop a sent fax: 'requested' (check status later) or 'finished' (too late)."""
        if not self.is_configured():
            raise ValueError('eFax is not configured.')
        try:
            sid = _fax_id(provider_sid)
        except ValueError:
            raise ValueError('eFax provider identity is invalid.') from None
        try:
            response, _ = await self._request('POST', f'/faxes/{sid}/cancel', timeout=_READ_TIMEOUT)
        except (EfaxCredentialsError, EfaxBusy):
            raise
        except (EfaxError, httpx.HTTPError, httpx.InvalidURL):
            raise EfaxError('eFax cancel request failed.') from None
        if response.status_code == 202:
            return 'requested'
        if response.status_code == 409:
            return 'finished'
        if response.status_code == 404:
            raise EfaxNotFound('eFax has no sent fax with this ID in the configured account.')
        raise EfaxError('eFax cancel request failed.')

    # Receiving ----------------------------------------------------------------
    async def list_received(self, *, offset: int = 0) -> list[dict[str, object]]:
        """One page (at most 100) of received faxes not downloaded yet, newest first."""
        if not self.is_configured():
            raise ValueError('eFax is not configured.')
        if type(offset) is not int or offset < 0:
            raise ValueError('eFax page offset is invalid.')
        try:
            response, raw = await self._request('GET', '/faxes/received', timeout=_READ_TIMEOUT,
                params={'image_downloaded': 'false'},
                extra={'pagination-offset': str(offset), 'pagination-limit': str(PAGE_LIMIT)})
        except (EfaxCredentialsError, EfaxBusy):
            raise
        except (EfaxError, httpx.HTTPError, httpx.InvalidURL):
            raise EfaxError('Faxbot could not list received faxes in eFax.') from None
        payload = _json(raw)
        faxes = payload.get('faxes') if isinstance(payload, dict) else None
        if response.status_code != 200 or not isinstance(faxes, list):
            raise EfaxError('eFax did not list received faxes.')
        result = []
        for item in faxes[:PAGE_LIMIT]:
            if not isinstance(item, dict):
                continue
            try:
                fax_id = _fax_id(item.get('fax_id'))
            except ValueError:
                continue
            result.append({'fax_id': fax_id, **{name: item.get(name) for name in (
                'completed_timestamp', 'originating_fax_number', 'destination_fax_number',
                'originating_fax_tsid', 'pages', 'size', 'duration', 'image_downloaded')}})
        return result

    async def get_received_fax(self, fax_id: str) -> dict[str, object]:
        """A received fax's details from the configured account; EfaxNotFound when it has none."""
        sid = self._received_id(fax_id)
        try:
            response, raw = await self._request('GET', f'/faxes/{sid}/metadata', timeout=_READ_TIMEOUT)
        except (EfaxCredentialsError, EfaxBusy):
            raise
        except (EfaxError, httpx.HTTPError, httpx.InvalidURL):
            raise EfaxError('Faxbot could not reach eFax.') from None
        if response.status_code in (403, 404):
            raise EfaxNotFound('eFax has no received fax with this ID in the configured account.')
        payload = _json(raw)
        if response.status_code != 200 or not isinstance(payload, dict):
            raise EfaxError('eFax did not describe this fax.')
        try:
            if payload.get('fax_id') is not None and _fax_id(payload.get('fax_id')) != sid:
                raise ValueError
        except ValueError:
            raise EfaxError('eFax described a different fax.') from None
        direction = payload.get('direction')
        if isinstance(direction, str) and direction.upper() != 'INBOUND':
            raise EfaxNotFound('eFax has no received fax with this ID in the configured account.')
        return payload

    async def download_received_fax(self, fax_id: str) -> bytes:
        """The received document as PDF (or TIFF if eFax keeps only that), checked and bounded."""
        sid = self._received_id(fax_id)
        try:
            response, raw = await self._request('GET', f'/faxes/{sid}/image', timeout=_IMAGE_TIMEOUT,
                                                limit=_MAX_IMAGE_REPLY, params={'desired_format': 'PDF'})
        except (EfaxCredentialsError, EfaxBusy):
            raise
        except EfaxError as error:
            if 'more data' in str(error):
                raise EfaxError('eFax sent a document larger than Faxbot accepts.') from None
            raise EfaxError('Faxbot could not reach eFax.') from None
        except (httpx.HTTPError, httpx.InvalidURL):
            raise EfaxError('Faxbot could not reach eFax.') from None
        if response.status_code in (403, 404):
            raise EfaxNotFound('eFax does not have the document for this fax.')
        payload = _json(raw)
        if response.status_code != 200 or not isinstance(payload, dict):
            raise EfaxError(f'eFax did not send the document (HTTP {response.status_code}).')
        try:
            if _fax_id(payload.get('fax_id')) != sid:
                raise ValueError
        except ValueError:
            raise EfaxError('eFax sent the document of a different fax.') from None
        image = payload.get('image')
        if not isinstance(image, str) or not image:
            raise EfaxError('eFax sent an empty document.')
        try:
            data = base64.b64decode(re.sub(r'\s+', '', image), validate=True)
        except (binascii.Error, ValueError):
            raise EfaxError('eFax sent a document Faxbot cannot decode.') from None
        if len(data) > MAX_DOCUMENT_BYTES:
            raise EfaxError('eFax sent a document larger than Faxbot accepts.')
        return data

    async def mark_downloaded(self, fax_id: str) -> None:
        """Record in eFax that this received fax was downloaded."""
        sid = self._received_id(fax_id)
        try:
            response, _ = await self._request('PATCH', f'/faxes/{sid}/metadata', timeout=_READ_TIMEOUT,
                                              payload={'image_downloaded': True})
        except (EfaxCredentialsError, EfaxBusy):
            raise
        except (EfaxError, httpx.HTTPError, httpx.InvalidURL):
            raise EfaxError('Faxbot could not reach eFax.') from None
        if response.status_code != 200:
            raise EfaxError('eFax did not record that the fax was downloaded.')

    async def delete_fax(self, fax_id: str) -> bool:
        """Delete one fax from eFax; False when eFax no longer has it."""
        sid = self._received_id(fax_id)
        try:
            response, _ = await self._request('DELETE', f'/faxes/{sid}', timeout=_READ_TIMEOUT)
        except (EfaxCredentialsError, EfaxBusy):
            raise
        except (EfaxError, httpx.HTTPError, httpx.InvalidURL):
            raise EfaxError('Faxbot could not reach eFax.') from None
        if response.status_code in (200, 204):
            return True
        if response.status_code == 404:
            return False
        raise EfaxError('eFax did not delete the fax.')

    def _received_id(self, fax_id: object) -> str:
        if not self.is_configured():
            raise ValueError('eFax is not configured.')
        try:
            return _fax_id(fax_id)
        except ValueError:
            raise ValueError('eFax fax ID is invalid.') from None

    # General ------------------------------------------------------------------
    async def health(self) -> bool:
        """Whether eFax says its API is up; needs no sign-in, so it never replaces the token."""
        try:
            async with self._client(_READ_TIMEOUT) as client:
                async with client.stream('GET', ORIGIN + '/health', headers={'Accept': 'application/json'}) as response:
                    body = await _bounded(response, _MAX_JSON_REPLY)
        except (EfaxError, httpx.HTTPError, httpx.InvalidURL):
            return False
        payload = _json(body)
        return response.status_code == 200 and isinstance(payload, dict) and payload.get('status') == 'UP'
