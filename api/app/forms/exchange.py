"""The exact-raster promise between enrolled partners (research M22).

A sender that holds a registered form sends a partner only the form's
address, the renderer version, the resolution, the typed values and the
SHA-256 of each page it would have faxed (``renderer.page_hash``). The
payload travels over the encrypted, signed direct delivery channel as the
``form`` kind (``application/vnd.faxbot.form+json``).

The receiver draws the pages itself and compares every hash:

- all match: it files the pages it drew, with the values attached, and signs
  the usual receipt; the sender records the form as delivered;
- any differ: nothing is filed. It answers with a signed ``form_mismatch``
  refusal, which asks for the full pages. On the sender a person decides
  whether to send them as an ordinary fax; Faxbot never does so by itself.

A receiver without the form fetches it once from the sender by its address
(``GET /forms/partner/forms/{address}``), checks the address and every page,
and keeps it. ``GET /forms/partner/holdings`` answers, signed, which form
versions an installation holds.

Uncertain outcomes follow the direct delivery rule: an answer that was lost
is reconciled by asking the partner (``GET /direct/deliveries/{id}``), never
by sending again.
"""
import asyncio
import concurrent.futures
from datetime import timedelta
import hashlib
import json
import re
from uuid import uuid4

import httpx

from ..config_runtime import run_lifecycle_step
from ..direct.crypto import (DirectProtocolError, check_signed, parse_timestamp, seal, signed, timestamp, verify)
from ..routing.database import utcnow
from . import model, renderer
from .store import FormStore, unpack_backgrounds


PAYLOAD = 1
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
REQUEST_SKEW = timedelta(minutes=5)
FETCH_TIMEOUT = 30.0
_HEX64 = re.compile(r'[a-f0-9]{64}')

MISMATCH = ('The pages this installation drew from your form data do not match yours, so nothing was filed. '
            'Send the full pages as a fax if they are still needed.')
STATE_TEXT = {
    'sending': 'Sending to the partner.',
    'delivered': "Delivered: the partner's Faxbot drew identical pages and filed them.",
    'mismatch': "The partner's pages did not match, so nothing was filed. Send the pages as a fax if they are needed.",
    'refused': 'The partner did not accept it, so nothing was filed.',
    'not_sent': 'The partner could not be reached; nothing was sent.',
    'uncertain': 'Waiting for the partner to confirm whether it arrived.',
    'not_received': 'The partner confirmed it never arrived; nothing was filed.',
    'faxed': 'Sent as a fax.',
    'matched': 'Received: the pages matched and were filed with their values.',
}


class FormSendError(RuntimeError):
    """A send that could not start; one plain sentence."""


def payload(form_address, values, rendered):
    return model.canonical({'faxbot_form_payload': PAYLOAD, 'form': form_address, 'renderer': rendered.renderer,
                            'resolution': rendered.resolution, 'values': values, 'pages': list(rendered.hashes)})


def parse_payload(data):
    try:
        if not isinstance(data, bytes) or len(data) > MAX_PAYLOAD_BYTES:
            raise ValueError
        document = json.loads(data)
        if model.canonical(document) != data:
            raise ValueError
        if (set(document) != {'faxbot_form_payload', 'form', 'renderer', 'resolution', 'values', 'pages'}
                or document['faxbot_form_payload'] != PAYLOAD or not _HEX64.fullmatch(str(document['form']))
                or not isinstance(document['renderer'], str) or len(document['renderer']) > 40
                or document['resolution'] not in renderer.RESOLUTIONS or not isinstance(document['values'], dict)
                or not isinstance(document['pages'], list) or not 0 < len(document['pages']) <= model.MAX_PAGES
                or any(not isinstance(page, str) or not _HEX64.fullmatch(page) for page in document['pages'])):
            raise ValueError
        return document
    except (ValueError, TypeError, RecursionError):
        raise model.FormError('The form data is not in the registered form format.') from None


def _blocking(make_coroutine):
    """Run an async request from synchronous code, whether or not this thread has an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_coroutine())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(make_coroutine())).result()


def request_headers(identity, method, path):
    moment = timestamp()
    return {'X-Faxbot-Direct-Key': identity.signing_key, 'X-Faxbot-Direct-Time': moment,
            'X-Faxbot-Direct-Signature': identity.sign(f'{method} {path} {moment}'.encode('ascii'))}


def check_bundle(bundle, address):
    """A fetched bundle whose content and pages match the address asked for; returns (bundle, backgrounds)."""
    if (not isinstance(bundle, dict)
            or set(bundle) != {'faxbot_form_bundle', 'address', 'title', 'version', 'content', 'backgrounds'}
            or bundle['faxbot_form_bundle'] != 1 or bundle['address'] != address
            or not isinstance(bundle['title'], str) or type(bundle['version']) is not int
            or not 1 <= bundle['version'] <= 100000 or not isinstance(bundle['backgrounds'], str)):
        raise model.FormError('The partner did not send a Faxbot form.')
    content = model.check_content(bundle['content'])
    if model.address(content) != address:
        raise model.FormError("The form the partner sent does not match its address.")
    return bundle, unpack_backgrounds(content, bundle['backgrounds'])


class FormExchange:
    def __init__(self, direct):
        """``direct`` is the installation's DirectService (keys, partners, transport, settings)."""
        self.direct = direct
        self.store = FormStore(direct.store.engine)

    # Receiving -------------------------------------------------------------------------------
    def receive(self, peer, manifest, document):
        """Check a form delivery; returns (refusal or None, rendered PDF or None).

        A refusal is (status, reason, sentence). Called inside the direct
        delivery receive step, before anything is stored or accepted.
        """
        message_id = manifest['message_id']
        try:
            data = parse_payload(document)
        except model.FormError as error:
            return (400, 'malformed', str(error)), None
        partner = peer['organization']
        if data['renderer'] not in renderer.RENDERERS:
            self._record_mismatch(peer, manifest, data, None)
            return (409, 'form_mismatch', 'This installation draws forms with another renderer version, so nothing '
                                          'was filed. Send the full pages as a fax if they are still needed.'), None
        version = self.store.version(address=data['form'])
        if version is None:
            try:
                version = self._fetch(peer, data['form'])
            except (model.FormError, DirectProtocolError):
                return (409, 'form_unavailable', f'This installation could not get the form from {partner}, so '
                                                 'nothing was filed.'), None
        try:
            values = model.values(version.content, data['values'], drawable=renderer.missing_characters)
        except model.FormError:
            self._record_mismatch(peer, manifest, data, version)
            return (409, 'form_mismatch', MISMATCH), None
        if values != data['values']:
            self._record_mismatch(peer, manifest, data, version)
            return (409, 'form_mismatch', MISMATCH), None
        rendered = renderer.render(version.content, version.backgrounds, values, resolution=data['resolution'],
                                   renderer=data['renderer'])
        if list(rendered.hashes) != data['pages']:
            self._record_mismatch(peer, manifest, data, version)
            return (409, 'form_mismatch', MISMATCH), None
        self.store.record(direction='inbound', route='direct', message_id=message_id, peer_id=peer['id'],
                          partner=partner, form_version_id=version.id, form_address=version.address,
                          renderer=data['renderer'], resolution=data['resolution'],
                          fax_number=manifest['sender']['fax_number'],
                          field_values=model.canonical(values).decode('ascii'),
                          page_hashes=json.dumps(data['pages']), pages=len(data['pages']),
                          digest=manifest['document']['sha256'], state='matched')
        return None, renderer.to_pdf(rendered)

    def _record_mismatch(self, peer, manifest, data, version):
        # The values are not kept: nothing was filed.
        self.store.record(direction='inbound', route='direct', message_id=manifest['message_id'], peer_id=peer['id'],
                          partner=peer['organization'], form_version_id=version.id if version else None,
                          form_address=data['form'], renderer=data['renderer'][:40], resolution=data['resolution'],
                          fax_number=manifest['sender']['fax_number'], field_values=None,
                          page_hashes=json.dumps(data['pages']), pages=len(data['pages']),
                          digest=manifest['document']['sha256'], state='mismatch',
                          detail='The pages drawn here did not match; Faxbot asked the sender for the full pages.')

    def _fetch(self, peer, address):
        """Fetch a form the sender holds, once, by its address; keep it only when everything matches."""
        identity = self.direct.identity()
        path = f'/forms/partner/forms/{address}'
        headers = request_headers(identity, 'GET', path)
        try:
            status, body = _blocking(lambda: asyncio.wait_for(
                self.direct.http.request('GET', peer['endpoint_url'] + path, headers=headers), FETCH_TIMEOUT))
        except (httpx.HTTPError, asyncio.TimeoutError, RuntimeError, OSError):
            raise model.FormError('The partner could not be reached.') from None
        if status != 200:
            raise model.FormError('The partner did not send the form.')
        bundle, _ = check_bundle(body, address)
        return self.store.add_partner_version(bundle, peer=peer)

    # Partner protocol --------------------------------------------------------------------------
    def _partner(self, method, path, signer, request_time, signature, *, now=None):
        now = now or utcnow()
        peer = self.direct.store.peer_by_key(signer) if isinstance(signer, str) else None
        if peer is None or peer['state'] == 'revoked':
            return None
        try:
            verify(peer['signing_key'], f'{method} {path} {request_time}'.encode('ascii'), signature)
            if abs(parse_timestamp(request_time) - now) > REQUEST_SKEW:
                return None
        except (DirectProtocolError, UnicodeEncodeError, TypeError, AttributeError):
            return None
        return peer

    def holdings(self, *, signer, request_time, signature, now=None):
        _, identity = self.direct._enabled_identity()
        peer = self._partner('GET', '/forms/partner/holdings', signer, request_time, signature, now=now)
        if peer is None:
            return 403, {'detail': 'This request is not from an enrolled partner.'}
        return 200, signed(identity, {'type': 'form_holdings', 'recipient': peer['signing_key'],
                                      'forms': self.store.addresses(), 'answered_at': timestamp()})

    def bundle(self, address, *, signer, request_time, signature, now=None):
        self.direct._enabled_identity()
        if not _HEX64.fullmatch(address or ''):
            return 404, {'detail': 'Not found.'}
        peer = self._partner('GET', f'/forms/partner/forms/{address}', signer, request_time, signature, now=now)
        if peer is None:
            return 403, {'detail': 'This request is not from an enrolled partner.'}
        version = self.store.version(address=address)
        if version is None:
            return 404, {'detail': 'This installation does not hold that form.'}
        return 200, version.bundle()

    # Asking a partner ---------------------------------------------------------------------------
    async def partner_holdings(self, peer):
        """The form versions a partner says, signed, that it holds; None when it could not be asked."""
        identity = await run_lifecycle_step(self.direct.identity)
        path = '/forms/partner/holdings'
        try:
            status, body = await self.direct.http.request('GET', peer['endpoint_url'] + path,
                                                           headers=request_headers(identity, 'GET', path))
            statement = check_signed(body, peer['signing_key'])
        except (httpx.HTTPError, DirectProtocolError, RuntimeError, OSError):
            return None
        if status != 200 or statement.get('type') != 'form_holdings' or statement.get('recipient') != identity.signing_key:
            return None
        forms = statement.get('forms')
        if not isinstance(forms, list):
            return None
        return [item for item in forms if isinstance(item, dict) and _HEX64.fullmatch(str(item.get('address')))]

    # Sending ------------------------------------------------------------------------------------
    def prepare(self, version, raw_values, resolution='fine'):
        values = model.values(version.content, raw_values, drawable=renderer.missing_characters)
        return values, renderer.render(version.content, version.backgrounds, values, resolution=resolution)

    def partner_for(self, number):
        """The verified partner a form to ``number`` goes to directly, or None (it goes as a fax)."""
        values = self.direct.values()
        if not values.direct_delivery_enabled or not self.direct.ready():
            return None
        from ..routing.store import RouteStore
        return RouteStore(self.direct.store.engine).verified_peer(number)

    def record_fax(self, version, values, rendered, *, number, job_id, actor_id, actor_name):
        return self.store.record(direction='outbound', route='fax', message_id=None, peer_id=None, partner=None,
                                 form_version_id=version.id, form_address=version.address, renderer=rendered.renderer,
                                 resolution=rendered.resolution, fax_number=number,
                                 field_values=model.canonical(values).decode('ascii'),
                                 page_hashes=json.dumps(list(rendered.hashes)), pages=len(rendered.pages),
                                 digest=None, state='faxed', fax_job_id=job_id, principal_id=actor_id,
                                 principal_name=actor_name)

    async def send_direct(self, version, values, rendered, peer, *, actor_id, actor_name):
        """Offer the values and page hashes to a partner; returns the delivery as recorded."""
        direct = self.direct
        settings = await run_lifecycle_step(direct.values)
        identity = await run_lifecycle_step(direct.identity)
        from ..routing.numbers import InvalidNumber, normalize_number
        try:
            own_number = normalize_number(settings.direct_fax_number, country=settings.fax_default_country)
        except InvalidNumber:
            own_number = None
        document = payload(version.address, values, rendered)
        message_id = uuid4().hex
        manifest, signature, ciphertext = seal(
            identity, message_id=message_id, organization=settings.direct_organization.strip() or 'Faxbot',
            fax_number=own_number, recipient_number=peer['phone_number'], recipient_signing_key=peer['signing_key'],
            recipient_exchange_key=peer['exchange_key'], document=document, pages=len(rendered.pages), form=True)
        digest = hashlib.sha256(document).hexdigest()
        row = await run_lifecycle_step(lambda: self.store.record(
            direction='outbound', route='direct', message_id=message_id, peer_id=peer['id'],
            partner=peer['organization'], form_version_id=version.id, form_address=version.address,
            renderer=rendered.renderer, resolution=rendered.resolution, fax_number=peer['phone_number'],
            field_values=model.canonical(values).decode('ascii'), page_hashes=json.dumps(list(rendered.hashes)),
            pages=len(rendered.pages), digest=digest, state='sending', principal_id=actor_id,
            principal_name=actor_name))
        from ..direct.service import PartnerAddressRefused, PartnerUnreachable
        try:
            status, body = await direct.http.request('POST', peer['endpoint_url'] + '/direct/deliveries', files={
                'manifest': (None, manifest, 'application/json'),
                'signature': (None, signature.encode('ascii'), 'text/plain'),
                'document': ('document.bin', ciphertext, 'application/octet-stream')})
        except PartnerAddressRefused as error:
            return await self._move(row, 'not_sent', detail=str(error)[:300])
        except PartnerUnreachable:
            return await self._move(row, 'not_sent')
        except httpx.HTTPError:
            # It may have arrived; ask the partner later instead of sending again.
            return await self._move(row, 'uncertain')
        except BaseException:
            await self._move(row, 'uncertain')
            raise
        return await self._answer(row, peer, status, body)

    async def _move(self, row, state, *, expected=('sending', 'uncertain'), **values):
        moved = await run_lifecycle_step(lambda: self.store.move(row['id'], state, expected=expected, **values))
        return moved or await run_lifecycle_step(lambda: self.store.delivery(row['id']))

    async def _answer(self, row, peer, status, body):
        try:
            statement = check_signed(body, peer['signing_key'])
        except DirectProtocolError:
            statement = None
        if statement is not None and statement.get('message_id') == row['message_id']:
            if (status == 200 and statement.get('type') == 'receipt' and statement.get('status') == 'accepted'
                    and statement.get('document_sha256') == row['digest']):
                return await self._move(row, 'delivered')
            if statement.get('type') == 'refusal' and 400 <= status < 500:
                reason = statement.get('reason')
                detail = statement.get('detail') if isinstance(statement.get('detail'), str) else None
                return await self._move(row, 'mismatch' if reason == 'form_mismatch' else 'refused',
                                        detail=(detail or '')[:300] or None)
        # The partner may have it; ask instead of sending again.
        return await self._move(row, 'uncertain')

    async def reconcile(self, row):
        """Ask a partner about a form whose answer was lost: delivered, never arrived, or ask again later."""
        direct = self.direct
        peer = await run_lifecycle_step(lambda: direct.store.get_peer(row['peer_id'])) if row['peer_id'] else None
        if peer is None:
            return None
        identity = await run_lifecycle_step(direct.identity)
        path = f"/direct/deliveries/{row['message_id']}"
        try:
            status, body = await direct.http.request('GET', peer['endpoint_url'] + path,
                                                     headers=request_headers(identity, 'GET', path))
            statement = check_signed(body, peer['signing_key'])
        except (httpx.HTTPError, DirectProtocolError, RuntimeError, OSError):
            return None
        if status != 200 or statement.get('type') != 'status' or statement.get('message_id') != row['message_id']:
            return None
        if statement.get('status') == 'accepted':
            try:
                receipt = check_signed(body.get('receipt'), peer['signing_key'])
            except DirectProtocolError:
                return None
            if receipt.get('document_sha256') != row['digest'] or receipt.get('message_id') != row['message_id']:
                return None
            return (await self._move(row, 'delivered'))['state']
        if statement.get('status') == 'not_received':
            return (await self._move(row, 'not_received'))['state']
        return None

    async def step(self):
        """Background: settle forms whose partner answer was lost (older than a minute)."""
        rows = await run_lifecycle_step(self.store.uncertain)
        cutoff = utcnow() - timedelta(minutes=1)
        for row in rows:
            if row['state'] == 'uncertain' or row['updated_at'] < cutoff:
                await self.reconcile(row)
        return False


def receive_form(service, peer, manifest, document):
    """The direct delivery hook for the ``form`` kind; see ``FormExchange.receive``."""
    return FormExchange(service).receive(peer, manifest, document)
