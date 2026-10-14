"""Telnyx Programmable Fax (the Fax API), billed by the page, using only captured account configuration.

Telnyx API v2, read 2026-10-13 from developers.telnyx.com and support.telnyx.com:

- **Media storage**: ``POST /v2/media`` (multipart ``media``, ``media_name``, ``ttl_secs``; at most 20 MB) answers
  201 with ``data.media_name``. A fax names stored media with ``media_name``, so no public link to the document
  is needed. https://developers.telnyx.com/api-reference/media-storage-api/upload-media
- **Send**: ``POST /v2/faxes`` (JSON ``connection_id`` = the Fax Application's id, ``from``, ``to``, ``media_name``,
  ``webhook_url``, ``client_state`` in Base64) answers 202 with ``data`` (the fax, ``status`` ``queued``). The page
  documents 422 for a refused request. https://developers.telnyx.com/api-reference/programmable-fax-commands/send-a-fax
- **Status**: ``GET /v2/faxes/{id}``: ``status`` is one of queued, media.processed, originated, sending, delivered,
  failed (and, for received faxes, initiated, receiving, media.processing, received), with ``failure_reason``. It
  carries no page count and no price.
  https://developers.telnyx.com/api-reference/programmable-fax-commands/view-a-fax
- **Failure reasons**: https://support.telnyx.com/en/articles/4967498-fax-api-error-list ("Telnyx does not
  automatically retry faxes with errors"). Telnyx publishes no grouping of reasons into "never connected" and "pages
  partly sent"; the grouping below is Faxbot's reading of each reason's description, and every reason it does not
  list (dropped calls, signalling and communication errors, Telnyx's own failures, and any new reason) is uncertain.
- **Fax Applications**: ``POST /v2/fax_applications`` (``application_name``, ``webhook_event_url``) answers 201
  with ``data.id``. Telnyx's quick start says an outbound voice profile must be assigned to the application before
  it can send. https://developers.telnyx.com/api-reference/programmable-fax-applications/creates-a-fax-application
- **Notifications**: Telnyx signs ``{telnyx-timestamp}|{raw body}`` with Ed25519; the ``telnyx-signature-ed25519``
  header is Base64 and ``telnyx-timestamp`` is Unix seconds, and a notification more than 5 minutes old is refused.
  The public key is in the Telnyx portal under Keys & Credentials.
  https://developers.telnyx.com/docs/messaging/messages/receiving-webhooks

Not yet run against a real Telnyx account: an integration checks one delivered fax, one busy number and one
refused key against these readings.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import re
import time

import httpx

from .routing.numbers import canonical_number


BASE_URL = 'https://api.telnyx.com/v2'
# Telnyx keeps the uploaded document this long, then deletes it; a fax is taken from storage once it starts.
MEDIA_TTL_SECONDS = 24 * 60 * 60
MEDIA_LIMIT_BYTES = 20 * 1024 * 1024
SIGNATURE_TOLERANCE_SECONDS = 300
_UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
_APPLICATION_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')

# Telnyx's answers to a send that mean it did not take the fax: the request was refused as a whole, so nothing was
# sent and another route may send it. Any other answer (5xx, a lost connection after the request left, a reply
# Faxbot cannot read) may hide an accepted fax, so it stays uncertain and is never sent again by itself.
REFUSED = {
    400: 'Telnyx refused the fax request, so nothing was sent.',
    401: 'Telnyx did not accept the API key, so nothing was sent. Check it on the Telnyx Fax API provider page.',
    403: 'Telnyx did not let this API key send faxes, so nothing was sent. Check the key in your Telnyx account.',
    404: 'Telnyx did not find the Fax Application, so nothing was sent. Check its ID on the Telnyx Fax API '
         'provider page.',
    422: 'Telnyx refused the fax number, the Fax Application or the document, so nothing was sent.',
    429: 'Telnyx asked Faxbot to slow down and did not take the fax, so nothing was sent.',
}
UNREACHABLE = 'Faxbot could not reach Telnyx, so nothing was sent.'
NOT_UPLOADED = 'Telnyx did not take the document, so nothing was sent.'

# Why a sent fax failed, from Telnyx's failure_reason. The call-outcome sentences are the same words other routes
# use, so delivery history and the calling-hour advice (routing/schedule.py) read them the same way.
BUSY = 'The number was busy.'
NO_ANSWER = 'Nobody answered the call.'
NO_FAX = 'No fax machine answered the call.'
NOT_IN_SERVICE = 'The number is not in service.'
NOT_CONNECTED = 'Telnyx could not connect the call.'
DECLINED = 'The number declined the call.'
NOT_CONVERTED = 'Telnyx could not turn the document into fax pages.'
NO_OUTBOUND_PROFILE = ('Telnyx did not place the call because the Fax Application has no outbound voice profile. '
                       'In your Telnyx account, assign one to it under Outbound Voice Profiles.')
NOT_ALLOWED = 'Your Telnyx account settings do not allow calls to this number, so nothing was sent.'
INVALID_NUMBER = 'Telnyx could not use this fax number, so nothing was sent.'
LIMIT_REACHED = 'Telnyx did not place the call because a limit on your Telnyx account was reached.'
ACCOUNT_DISABLED = 'Telnyx did not place the call because your Telnyx account is turned off.'
PARTLY_SENT = 'Telnyx sent only some of the pages, so Faxbot did not send the fax again.'
NOT_CONFIRMED = ('The call ended before Telnyx confirmed the fax, so part of it may have arrived; '
                 'Faxbot did not send it again.')

# Reasons that ended before any fax data: the call was never placed, never answered, or answered by no fax machine.
BEFORE_FAX_DATA = {
    'user_busy': BUSY,
    'receiver_no_answer': NO_ANSWER,
    'receiver_no_response': NO_ANSWER,
    'receiver_decline': DECLINED,
    'receiver_unallocated_number': NOT_IN_SERVICE,
    'receiver_incompatible_destination': NOT_CONNECTED,
    'destination_unreachable': NOT_CONNECTED,
    'receiver_recovery_on_timer_expire': NOT_CONNECTED,
    # "The destination did not answer as a fax or sent no fax tones": no fax handshake, so no page.
    'fax_initial_communication_timeout': NO_FAX,
    'file_format_invalid': NOT_CONVERTED,
    'file_download_failed': NOT_CONVERTED,
    'file_size_limit_exceeded': NOT_CONVERTED,
    'page_count_limit_exceeded': NOT_CONVERTED,
    'media_processing_exception': NOT_CONVERTED,
    'no_outbound_profile': NO_OUTBOUND_PROFILE,
    'account_disabled': ACCOUNT_DISABLED,
    'destination_invalid': INVALID_NUMBER,
    'receiver_invalid_number_format': INVALID_NUMBER,
    'destination_not_in_service_plan': NOT_ALLOWED,
    'destination_not_in_countries_whitelist': NOT_ALLOWED,
    'unverified_origination_number': NOT_ALLOWED,
    'unverified_destination_not_allowed': NOT_ALLOWED,
    'user_channel_limit_exceeded': LIMIT_REACHED,
    'outbound_profile_channel_limit_exceeded': LIMIT_REACHED,
    'connection_channel_limit_exceeded': LIMIT_REACHED,
    'outbound_profile_daily_spend_limit_exceeded': LIMIT_REACHED,
}
# Telnyx's refusals of the call itself (as a SIP 404 or 403 would be): a burst of them reads the balance.
REFUSALS = frozenset({'receiver_unallocated_number', 'destination_invalid', 'destination_not_in_service_plan',
                      'destination_not_in_countries_whitelist'})
# Every sentence above; delivery history shows a reason only from such a set.
FAILURE_SENTENCES = frozenset({*REFUSED.values(), UNREACHABLE, NOT_UPLOADED, *BEFORE_FAX_DATA.values(),
                               PARTLY_SENT, NOT_CONFIRMED})
STATUSES = {
    'queued': 'in_progress', 'media.processing': 'in_progress', 'media.processed': 'in_progress',
    'initiated': 'in_progress', 'originated': 'in_progress', 'sending': 'in_progress',
    'delivered': 'success', 'failed': 'failed',
}


class TelnyxFaxRefused(RuntimeError):
    """Telnyx provably did not take the fax; ``sentence`` says why in plain words."""

    def __init__(self, sentence):
        self.sentence = sentence
        super().__init__(sentence)


class TelnyxFaxError(RuntimeError):
    """A setup request (creating a Fax Application) failed; ``sentence`` is one plain sentence, never a secret."""

    def __init__(self, sentence):
        self.sentence = sentence
        super().__init__(sentence)


def failure_outcome(fax: dict) -> tuple[str, str | None, bool | None]:
    """(sentence, error category, ended before fax data) for a fax Telnyx reports as failed.

    A category of None with True means nothing reached the fax machine, so another route may send it. With a page
    count above zero it is ``partly_sent``; every reason not listed in ``BEFORE_FAX_DATA`` may have delivered part of a
    page (``pages_unconfirmed``: uncertain, waiting for a person, as for the fax engine's own calls). Neither is ever
    sent again by itself.
    """
    pages = fax.get('page_count')
    reason = str(fax.get('failure_reason') or '').strip().lower()
    if type(pages) is int and pages > 0:
        return PARTLY_SENT, 'partly_sent', False
    if reason == 'account_disabled':
        # Telnyx turned the account off: its routes stop and the balance is read again (routing/telnyx_balance.py).
        from .routing import telnyx_balance
        telnyx_balance.note_disabled()
    elif reason in REFUSALS:
        # Telnyx refused the call as a SIP 404 or 403 would: a burst of them reads your balance at once.
        from .routing import telnyx_balance
        telnyx_balance.note_refusal()
    if reason in BEFORE_FAX_DATA:
        return BEFORE_FAX_DATA[reason], None, True
    return NOT_CONFIRMED, 'pages_unconfirmed', None


def _fax(response: httpx.Response, *, requested_sid: str | None = None) -> dict:
    """The fax in a Telnyx answer, as Faxbot's receipt: provider_sid, status and, when failed, why."""
    payload = response.json()
    fax = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(fax, dict) or not isinstance(fax.get('id'), str) or _UUID.fullmatch(fax['id']) is None:
        raise ValueError
    sid = fax['id'].lower()
    if requested_sid is not None and sid != requested_sid:
        raise ValueError
    direction = fax.get('direction')
    if direction is not None and direction != 'outbound':
        raise ValueError
    status = STATUSES.get(str(fax.get('status') or '').strip().lower()) if 'status' in fax or requested_sid else \
        'in_progress'
    if status is None:
        # A status Telnyx did not document for a sent fax requires reconciliation, never a guess.
        raise ValueError
    receipt = {'provider_sid': sid, 'status': status}
    pages = fax.get('page_count')
    if type(pages) is int and pages >= 0:
        receipt['pages'] = pages
    if status == 'failed':
        receipt['failure'], category, receipt['before_fax_data'] = failure_outcome(fax)
        if category is not None:
            receipt['failure_category'] = category
        reason = fax.get('failure_reason')
        if isinstance(reason, str) and re.fullmatch(r'[a-z0-9_.]{1,64}', reason):
            receipt['failure_reason'] = reason
    return receipt


def _refusal_sentence(status: int, payload: object) -> str:
    """One plain sentence for a refused setup request; never Telnyx's own text, which may echo input."""
    if status == 401:
        return 'Telnyx did not accept the API key. Check it on the Telnyx Fax API provider page.'
    if status == 403:
        return 'Telnyx did not let this API key create a Fax Application. Check the key in your Telnyx account.'
    if status == 422:
        return 'Telnyx refused the Fax Application name or the address for fax results.'
    if status == 429:
        return 'Telnyx asked Faxbot to slow down. Try again in a minute.'
    return 'Telnyx did not create the Fax Application. Try again, or create one in your Telnyx account.'


def load_public_key(text: str):
    """The Ed25519 public key from the Telnyx portal (Base64: the 32-byte key, or that key in a DER wrapper)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    try:
        raw = base64.b64decode(str(text or '').strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('The Telnyx public key is not Base64.') from None
    if len(raw) == 32:
        return Ed25519PublicKey.from_public_bytes(raw)
    try:
        key = load_der_public_key(raw)
    except (ValueError, TypeError):
        raise ValueError('The Telnyx public key is not an Ed25519 key.') from None
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError('The Telnyx public key is not an Ed25519 key.')
    return key


def verify_signature(public_key: str, *, timestamp: str, signature: str, body: bytes, now: float | None = None) -> bool:
    """Whether Telnyx signed ``{timestamp}|{body}`` with this account's key within the last 5 minutes."""
    from cryptography.exceptions import InvalidSignature
    if not public_key or not isinstance(body, (bytes, bytearray)):
        return False
    try:
        key = load_public_key(public_key)
        stamp = int(str(timestamp or '').strip())
        signed = base64.b64decode(str(signature or '').strip(), validate=True)
    except (ValueError, binascii.Error):
        return False
    current = time.time() if now is None else now
    if abs(current - stamp) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    try:
        key.verify(signed, str(stamp).encode() + b'|' + bytes(body))
    except InvalidSignature:
        return False
    return True


@dataclass(slots=True)
class TelnyxFaxService:
    """A captured Telnyx Fax API account: one upload, one create attempt, no endpoint fallback."""

    api_key: str = field(repr=False)
    connection_id: str
    from_number: str
    base_url: str = BASE_URL
    # Where Telnyx posts this fax's results (outbound_transport sets it when the account has a public key).
    status_callback_url: str | None = field(default=None, repr=False)
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False, compare=False, kw_only=True)

    def __post_init__(self) -> None:
        if (not isinstance(self.api_key, str) or not isinstance(self.connection_id, str)
                or not isinstance(self.from_number, str) or not isinstance(self.base_url, str)
                or any(not 33 <= ord(character) <= 126 for character in self.api_key)
                or (self.connection_id and _APPLICATION_ID.fullmatch(self.connection_id) is None)):
            raise ValueError('Telnyx Fax API configuration is invalid.')

    def is_configured(self) -> bool:
        return bool(self.api_key and self.connection_id and self.from_number)

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self.transport, follow_redirects=False, trust_env=False,
                                 headers={'Authorization': 'Bearer ' + self.api_key, 'Accept': 'application/json'})

    async def upload_document(self, file_path: str, media_name: str) -> str:
        """Put the prepared PDF in Telnyx's media storage; its name, or ``TelnyxFaxRefused`` (no fax exists yet)."""
        try:
            with open(file_path, 'rb') as handle:
                document = handle.read(MEDIA_LIMIT_BYTES + 1)
        except OSError:
            raise TelnyxFaxRefused(NOT_UPLOADED) from None
        if len(document) > MEDIA_LIMIT_BYTES:
            raise TelnyxFaxRefused(NOT_UPLOADED)
        try:
            async with self._client(60.0) as client:
                response = await client.post(self.base_url + '/media',
                    data={'media_name': media_name, 'ttl_secs': str(MEDIA_TTL_SECONDS)},
                    files={'media': ('fax.pdf', document, 'application/pdf')})
        except (httpx.HTTPError, httpx.InvalidURL):
            # The fax is created only after the upload, so whatever happened here, Telnyx has no fax.
            raise TelnyxFaxRefused(UNREACHABLE) from None
        if response.status_code in (401, 403):
            raise TelnyxFaxRefused(REFUSED[response.status_code])
        try:
            if not 200 <= response.status_code < 300:
                raise ValueError
            data = response.json().get('data')
            name = data.get('media_name') if isinstance(data, dict) else None
            if not isinstance(name, str) or not name:
                raise ValueError
            return name
        except (ValueError, TypeError, AttributeError):
            raise TelnyxFaxRefused(NOT_UPLOADED) from None

    async def send_fax_file(self, to_number: str, file_path: str, *, reference: str) -> dict:
        """Upload the PDF, then create the fax once. Raises ``TelnyxFaxRefused`` only when Telnyx provably did not
        take the fax: the upload failed, a documented refusal, or no connection was ever made for the create."""
        if not self.is_configured():
            raise ValueError('Telnyx Fax API is not configured.')
        try:
            to = canonical_number(to_number)
        except ValueError:
            raise ValueError('Telnyx destination is invalid.') from None
        if not isinstance(reference, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', reference) is None:
            raise ValueError('Telnyx fax reference is invalid.')
        media_name = await self.upload_document(file_path, 'faxbot-' + reference)
        body = {'connection_id': self.connection_id, 'from': self.from_number, 'to': to, 'media_name': media_name,
                # The attempt's identity comes back in every notification and lets a person find the fax in Telnyx.
                'client_state': base64.b64encode(reference.encode()).decode()}
        if self.status_callback_url:
            body['webhook_url'] = self.status_callback_url
        try:
            async with self._client(30.0) as client:
                response = await client.post(self.base_url + '/faxes', json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol, httpx.InvalidURL):
            # The connection was never made, so no request reached Telnyx.
            raise TelnyxFaxRefused(UNREACHABLE) from None
        except httpx.HTTPError:
            raise RuntimeError('Telnyx create request failed.') from None
        if response.status_code in REFUSED:
            raise TelnyxFaxRefused(REFUSED[response.status_code])
        try:
            if not 200 <= response.status_code < 300:
                raise ValueError
            return _fax(response)
        except (ValueError, TypeError, AttributeError):
            raise RuntimeError('Telnyx create response requires reconciliation.') from None

    async def get_fax_status(self, provider_sid: str) -> dict:
        """Read the original account's fax; require the same identity and a documented status."""
        if not self.api_key:
            raise ValueError('Telnyx Fax API is not configured.')
        if not isinstance(provider_sid, str) or _UUID.fullmatch(provider_sid) is None:
            raise ValueError('Telnyx fax identity is invalid.')
        sid = provider_sid.lower()
        try:
            async with self._client(15.0) as client:
                response = await client.get(self.base_url + '/faxes/' + sid)
            if response.status_code != 200:
                raise ValueError
            return _fax(response, requested_sid=sid)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError, AttributeError):
            raise RuntimeError('Telnyx status request failed.') from None

    async def create_fax_application(self, name: str, webhook_url: str) -> str:
        """Create a Fax Application whose results go to ``webhook_url``; its id, or ``TelnyxFaxError``."""
        if not self.api_key:
            raise TelnyxFaxError('Enter the Telnyx API key first.')
        body = {'application_name': name, 'webhook_event_url': webhook_url, 'active': True}
        try:
            async with self._client(30.0) as client:
                response = await client.post(self.base_url + '/fax_applications', json=body)
        except (httpx.HTTPError, httpx.InvalidURL):
            raise TelnyxFaxError('Faxbot could not reach Telnyx. Check this server\'s internet connection and try '
                                 'again.') from None
        if not 200 <= response.status_code < 300:
            raise TelnyxFaxError(_refusal_sentence(response.status_code, None))
        try:
            data = response.json().get('data')
            identity = str(data.get('id')) if isinstance(data, dict) and data.get('id') is not None else ''
        except (ValueError, TypeError, AttributeError):
            identity = ''
        if _APPLICATION_ID.fullmatch(identity) is None:
            raise TelnyxFaxError('Telnyx answered without a Fax Application ID. Check your Telnyx account before '
                                 'trying again, so you do not end up with two.')
        return identity
