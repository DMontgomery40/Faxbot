"""The FHIR route: a fax delivered as a FHIR R4 DocumentReference to the recipient organization's server.

Authentication is SMART Backend Services (SMART App Launch 2.2.0, STU 2.2;
client-confidential-asymmetric): Faxbot signs a one-time JWT (``RS384`` or
``ES384``; ``iss`` and ``sub`` the client ID, ``aud`` the token address,
``exp`` five minutes ahead at most, a fresh ``jti``, ``kid`` in the header)
and trades it at the token address (``grant_type=client_credentials``,
``client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-
bearer``) for an access token. The token address is the account's setting,
else the server's ``.well-known/smart-configuration`` ``token_endpoint``. The
public key set Faxbot serves (``jwks``) is what the recipient's system
registers; JWKS-URL registration is the spec's "strongly preferred" way.

The document is ``POST [base]/DocumentReference`` (``application/fhir+json``)
with the PDF inline as ``content.attachment.data`` (with its size and SHA-1
hash, as R4's Attachment carries them), ``status: current``, an
``identifier`` made from the attempt (``urn:ietf:rfc:3986`` |
``urn:uuid:<attempt>``) and ``If-None-Exist: identifier=...`` (conditional
create): a repeat cannot make a second copy, and an uncertain answer is
settled by searching for that identifier, never by sending again.

No ``subject``: a fax has no patient, and base R4 makes it optional. US Core
(and Epic's and Oracle Health's note writes) require a Patient, so such a
server refuses with a 4xx; that is definite, and the fax goes by its own route
in the same attempt. An integration must agree what the recipient accepts.

Outcomes (HTTP semantics, RFC 9110):
- 201 Created, or 200 for a conditional create that found the document: delivered.
- Nothing sent (address refused, connection or TLS failed, the sign-in refused,
  a 4xx including 429, or 503): definite; ``DirectRefused``.
- The request left and no usable answer came (a timeout or reset after
  sending, another 5xx, 412 for several matches): uncertain.

Endpoints come only from confirmed recipient addresses and must be https; a
private or local address is refused unless private partners are allowed
(``direct/addresses.py``). Not yet run against a real FHIR server or EHR.
"""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import time
import uuid
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from ..routing.database import utcnow
from . import certificates
from .store import DigitalStore


ASSERTION_TYPE = 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer'
IDENTIFIER_SYSTEM = 'urn:ietf:rfc:3986'
FHIR_JSON = 'application/fhir+json'
MAX_INLINE = 20 * 1024 * 1024
logger = logging.getLogger(__name__)


class NotSent(RuntimeError):
    """Nothing reached the server."""


class Lost(RuntimeError):
    """The request left Faxbot and no usable answer came back."""


class FhirRefused(RuntimeError):
    """The server or its sign-in definitely did not take the document; one sentence."""


class FhirUncertain(RuntimeError):
    """The server may have stored the document; one sentence."""


# JWT (JWS compact, RFC 7515) with cryptography ---------------------------------------------------------------------

def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _int_bytes(number, size):
    return number.to_bytes(size, 'big')


def sign_jwt(claims, key, *, algorithm, key_id):
    header = {'alg': algorithm, 'kid': key_id, 'typ': 'JWT'}
    signing_input = (_b64(json.dumps(header, separators=(',', ':')).encode()) + '.'
                     + _b64(json.dumps(claims, separators=(',', ':')).encode())).encode('ascii')
    if algorithm == 'RS384':
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA384())
    elif algorithm == 'ES384':
        r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA384())))
        signature = _int_bytes(r, 48) + _int_bytes(s, 48)   # JOSE wants raw r||s, not DER (RFC 7518 3.4)
    else:
        raise ValueError('Unknown signing method.')
    return signing_input.decode('ascii') + '.' + _b64(signature)


def client_assertion(account, token_url, *, now=None):
    key = certificates.load_private_key(account.secret('signing_key'))
    moment = int(now if now is not None else time.time())
    claims = {'iss': account.setting('client_id'), 'sub': account.setting('client_id'), 'aud': token_url,
              'exp': moment + 240, 'iat': moment, 'jti': uuid.uuid4().hex}
    return sign_jwt(claims, key, algorithm=account.setting('algorithm') or 'RS384',
                    key_id=account.setting('key_id') or 'faxbot')


def jwks(account):
    """The public key set for the recipient's system to register (RFC 7517), or None without a key."""
    text = account.secret('signing_key')
    if not text:
        return None
    public = certificates.load_private_key(text).public_key()
    kid = account.setting('key_id') or 'faxbot'
    algorithm = account.setting('algorithm') or 'RS384'
    if isinstance(public, rsa.RSAPublicKey):
        numbers = public.public_numbers()
        size = (numbers.n.bit_length() + 7) // 8
        key = {'kty': 'RSA', 'n': _b64(_int_bytes(numbers.n, size)),
               'e': _b64(_int_bytes(numbers.e, (numbers.e.bit_length() + 7) // 8))}
    else:
        numbers = public.public_numbers()
        key = {'kty': 'EC', 'crv': 'P-384', 'x': _b64(_int_bytes(numbers.x, 48)), 'y': _b64(_int_bytes(numbers.y, 48))}
    return {'keys': [{**key, 'kid': kid, 'alg': algorithm, 'use': 'sig', 'key_ops': ['verify']}]}


# HTTP ---------------------------------------------------------------------------------------------------------------

@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        try:
            return json.loads(self.body or b'null')
        except ValueError:
            return None


class Transport:
    """HTTP for FHIR; ``request`` raises NotSent or Lost. Tests pass a fake server's ``send``."""

    def __init__(self, *, send=None, allow_private=lambda: False, resolver=None, timeout=30.0):
        self.send = send
        self.allow_private = allow_private
        self.resolver = resolver
        self.timeout = timeout

    def request(self, method, url, *, headers=None, data=None):
        if not url.startswith('https://') and not (self.send is not None and url.startswith('http://')):
            raise NotSent('Faxbot sends documents only to https addresses.')
        if self.send is not None:
            return self.send(method, url, headers or {}, data)
        import httpx
        from ..direct import addresses
        options = {'headers': headers or {}}
        target = url
        if not self.allow_private():
            try:
                address = addresses.checked_address(url, **({'resolver': self.resolver} if self.resolver else {}))
            except addresses.PartnerAddressError:
                raise NotSent("The FHIR server's address is on a private or local network, or could not be found.") \
                    from None
            target, options = addresses.pinned_request(url, address, options)
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                response = client.request(method, target, content=data, **options)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol,
                httpx.InvalidURL, httpx.ProxyError):
            raise NotSent('The FHIR server could not be reached.') from None
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError,
                httpx.RemoteProtocolError):
            raise Lost('The FHIR server stopped answering after the document was sent.') from None
        return Response(response.status_code, {name.lower(): value for name, value in response.headers.items()},
                        response.content)


def token_url_for(account, base, transport):
    """The token address: the account's, else the server's published SMART configuration's. Raises FhirRefused."""
    if account.setting('token_url'):
        return account.setting('token_url')
    try:
        response = transport.request('GET', base.rstrip('/') + '/.well-known/smart-configuration',
                                     headers={'Accept': 'application/json'})
    except (NotSent, Lost):
        raise FhirRefused("Faxbot could not read the FHIR server's sign-in settings; nothing was sent.") from None
    found = response.json() if response.status == 200 else None
    url = found.get('token_endpoint') if isinstance(found, dict) else None
    if not isinstance(url, str) or not url.startswith('https://'):
        raise FhirRefused('The FHIR server publishes no sign-in address; enter it on the FHIR client.')
    return url


def access_token(account, base, transport, *, now=None):
    """An access token for the server. Raises FhirRefused: nothing was sent yet."""
    token_url = token_url_for(account, base, transport)
    form = urlencode({'grant_type': 'client_credentials',
                      'scope': account.setting('scope') or 'system/DocumentReference.c',
                      'client_assertion_type': ASSERTION_TYPE,
                      'client_assertion': client_assertion(account, token_url, now=now)}).encode('ascii')
    try:
        response = transport.request('POST', token_url, headers={
            'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'}, data=form)
    except (NotSent, Lost):
        raise FhirRefused('The FHIR sign-in could not be reached; nothing was sent.') from None
    found = response.json() if response.status == 200 else None
    token = found.get('access_token') if isinstance(found, dict) else None
    if not isinstance(token, str) or not token:
        error = found.get('error') if isinstance(found, dict) else None
        raise FhirRefused('The FHIR server refused Faxbot\'s sign-in'
                          + (f' ({str(error)[:40]})' if error else '') + '; nothing was sent.')
    return token


def identifier_for(attempt_id):
    return 'urn:uuid:' + str(uuid.UUID(hex=attempt_id) if len(attempt_id) == 32 else uuid.uuid5(
        uuid.NAMESPACE_URL, f'faxbot:{attempt_id}'))


def document_reference(*, identifier, document, pages, organization, author=None, type_code=None, now=None):
    now = now or datetime.now(timezone.utc)
    resource = {
        'resourceType': 'DocumentReference',
        'identifier': [{'system': IDENTIFIER_SYSTEM, 'value': identifier}],
        'status': 'current', 'docStatus': 'final',
        'date': now.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'description': f'Document from {organization}' + (f', {pages} page{"" if pages == 1 else "s"}' if pages else ''),
        'author': [{'display': author or organization}],
        'content': [{'attachment': {
            'contentType': 'application/pdf', 'language': 'en-US',
            'data': base64.b64encode(document).decode('ascii'), 'size': len(document),
            'hash': base64.b64encode(hashlib.sha1(document).digest()).decode('ascii'),  # noqa: S324 - R4 Attachment
            'title': f'Document from {organization}', 'creation': now.isoformat(timespec='seconds')}}],
    }
    if type_code:
        resource['type'] = {'coding': [{'system': 'http://loinc.org', 'code': type_code}]}
    return resource


def _resource_id(response, body):
    if isinstance(body, dict) and isinstance(body.get('id'), str):
        return body['id']
    location = response.headers.get('location') or response.headers.get('content-location') or ''
    parts = [part for part in urlsplit(location).path.split('/') if part]
    if 'DocumentReference' in parts and parts.index('DocumentReference') + 1 < len(parts):
        return parts[parts.index('DocumentReference') + 1]
    return None


def post_document(account, base, resource, identifier, transport, *, now=None):
    """('delivered', resource id) or raises FhirRefused / FhirUncertain."""
    token = access_token(account, base, transport, now=now)
    body = json.dumps(resource, separators=(',', ':')).encode('utf-8')
    try:
        response = transport.request('POST', base.rstrip('/') + '/DocumentReference', headers={
            'Authorization': f'Bearer {token}', 'Content-Type': FHIR_JSON, 'Accept': FHIR_JSON,
            'If-None-Exist': 'identifier=' + f'{IDENTIFIER_SYSTEM}|{identifier}'}, data=body)
    except NotSent as error:
        raise FhirRefused(f'{error} Nothing was sent.') from None
    except Lost:
        raise FhirUncertain('The FHIR server stopped answering after the document was sent, so Faxbot cannot tell '
                            'whether it was stored.') from None
    found = response.json()
    if response.status in (200, 201):
        resource_id = _resource_id(response, found)
        return 'delivered', resource_id
    if 400 <= response.status < 500 and response.status != 412 or response.status == 503:
        reason = _outcome_text(found)
        raise FhirRefused(f'The FHIR server refused the document ({response.status})'
                          + (f': {reason}' if reason else '') + '.')
    raise FhirUncertain(f'The FHIR server answered {response.status} after the document was sent, so Faxbot cannot '
                        'tell whether it was stored.')


def _outcome_text(body):
    """The first diagnostics or details text of an OperationOutcome, short; never document content."""
    if not isinstance(body, dict) or body.get('resourceType') != 'OperationOutcome':
        return None
    for issue in body.get('issue') or ():
        if isinstance(issue, dict):
            text = issue.get('diagnostics') or (issue.get('details') or {}).get('text')
            if isinstance(text, str) and text.strip():
                return text.strip()[:120]
    return None


def find_document(account, base, identifier, transport, *, now=None):
    """The resource id stored under ``identifier``, None when the server holds none. Raises FhirRefused/Lost."""
    token = access_token(account, base, transport, now=now)
    query = urlencode({'identifier': f'{IDENTIFIER_SYSTEM}|{identifier}'})
    try:
        response = transport.request('GET', base.rstrip('/') + f'/DocumentReference?{query}', headers={
            'Authorization': f'Bearer {token}', 'Accept': FHIR_JSON})
    except (NotSent, Lost):
        raise FhirRefused('The FHIR server could not be asked.') from None
    found = response.json()
    if response.status != 200 or not isinstance(found, dict) or found.get('resourceType') != 'Bundle':
        raise FhirRefused('The FHIR server did not answer the search.')
    for entry in found.get('entry') or ():
        resource = entry.get('resource') if isinstance(entry, dict) else None
        if isinstance(resource, dict) and resource.get('resourceType') == 'DocumentReference':
            return resource.get('id') or 'found'
    return None


# The sender ---------------------------------------------------------------------------------------------------------

@dataclass
class FhirSubmission:
    store: DigitalStore
    account: object
    transport: Transport
    row: dict
    base: str
    resource: dict
    identifier: str

    async def submit(self):
        from ..config_runtime import run_lifecycle_step
        from ..outbound_worker import SubmissionReceipt
        from ..routing.transport import DirectRefused
        store, row = self.store, self.row
        try:
            outcome, resource_id = await run_lifecycle_step(lambda: post_document(
                self.account, self.base, self.resource, self.identifier, self.transport))
        except FhirRefused as refusal:
            sentence = str(refusal)
            await run_lifecycle_step(lambda: store.move(row['id'], 'refused', expected=('sending',), kind='refused',
                                                        dedupe=f"{row['id']}:refused", detail=sentence))
            raise DirectRefused(sentence) from None
        except FhirUncertain as uncertain:
            sentence = str(uncertain)
            await run_lifecycle_step(lambda: store.move(row['id'], 'uncertain', expected=('sending',),
                                                        kind='uncertain', dedupe=f"{row['id']}:uncertain",
                                                        detail=sentence))
            raise
        await run_lifecycle_step(lambda: store.move(
            row['id'], 'delivered', expected=('sending',), kind='delivered', dedupe=f"{row['id']}:delivered",
            submitted=True, remote_id=resource_id or None, details={'resource_id': resource_id},
            detail="Delivered: the recipient's system stored it."))
        return SubmissionReceipt(None, 'success')


class FhirSender:
    def __init__(self, store, account, *, transport=None):
        self.store, self.account = store, account
        self.transport = transport or Transport()

    def prepare(self, *, claim, job, view, document, values, now=None):
        account = self.account
        if len(document) > MAX_INLINE:
            from ..routing.transport import DirectRefused
            raise DirectRefused('The document is too large to send to a FHIR server inline.')
        organization = (getattr(values, 'direct_organization', '') or '').strip() or account.label
        identifier = identifier_for(claim.attempt_id)
        resource = document_reference(identifier=identifier, document=document, pages=job.get('pages'),
                                      organization=organization, author=account.setting('author'),
                                      type_code=account.setting('document_type'), now=now)
        row, _ = self.store.begin_message(
            direction='out', kind='fhir', account_key=account.key, address_id=view['id'], job_id=claim.job_id,
            attempt_id=claim.attempt_id, message_id=identifier, counterpart=view['address'], state='sending',
            digest=hashlib.sha256(document).hexdigest(), size=len(document), pages=job.get('pages'))
        return FhirSubmission(self.store, account, self.transport, row, view['address'], resource, identifier)


def reconcile(store, row, account, transport, *, delivery=None, now=None):
    """Settle one uncertain FHIR message by searching for its identifier; never by sending again."""
    moment = now or utcnow()
    view = store.address(row['address_id']) if row['address_id'] else None
    base = view['address'] if view else row['counterpart']
    try:
        found = find_document(account, base, row['message_id'], transport)
    except FhirRefused as refusal:
        store.event(row['id'], 'search_failed', dedupe=f"{row['id']}:search:{moment:%Y%m%d%H%M}",
                    details={'why': str(refusal)[:200]}, now=now)
        return 'unknown'
    if found is None:
        store.event(row['id'], 'not_found', dedupe=f"{row['id']}:not_found:{moment:%Y%m%d%H%M}", now=now)
        return 'not_found'
    if store.move(row['id'], 'delivered', expected=('uncertain',), kind='found', dedupe=f"{row['id']}:found",
                  remote_id=found, details={'resource_id': found},
                  detail="Delivered: the recipient's system holds it.", now=now):
        if delivery is not None and row['job_id'] and row['attempt_id']:
            from .direct_message import _observe
            _observe(delivery, row, 'success', f"digital:{row['id']}:found")
    return 'delivered'
