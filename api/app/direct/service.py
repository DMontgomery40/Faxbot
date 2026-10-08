"""Direct delivery protocol: receive, answer status, confirm codes, send and reconcile.

Receiving never needs an API key: every request is authenticated by an Ed25519
signature from an enrolled partner. Sending keeps the delivery worker's rule:
only a refusal that proves nothing was accepted allows the conventional fax
route in the same attempt; any other failure after upload is reconciled by
asking the partner, never by sending again.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets

import httpx

from ..config_runtime import run_lifecycle_step
from ..outbound_worker import SubmissionReceipt
from ..routing.database import utcnow
from ..routing.transport import DirectRefused
from .addresses import PartnerAddressError, checked_address, pinned_request, resolve
from .crypto import (FAX_IMAGE, FORM, RELAY, DirectProtocolError, capabilities, card, canonical, check_card,
                     check_signed, kind_of, open_document, parse_capabilities, parse_manifest, parse_timestamp, seal,
                     signed, timestamp, verify)
from . import faximage
from .filing import DirectFiling
from .identity import IdentityUnavailable, identity_path, load_identity
from .store import DirectConflict, DirectStore, accepts_fax_images


FRESHNESS = timedelta(hours=24)
REQUEST_SKEW = timedelta(minutes=5)
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


class DirectUnavailable(RuntimeError):
    """Direct delivery is switched off or not set up on this installation."""


class PartnerUnreachable(RuntimeError):
    """Nothing reached the partner: the connection could not be opened."""


class PartnerAddressRefused(PartnerUnreachable):
    """Nothing was sent: the partner's address is not a public Internet address."""


class HttpClient:
    """Production transport to partner installations.

    Unless private partners are allowed, each request first checks that the
    partner's host resolves only to public addresses, then connects to the
    address it checked (see addresses.py).
    """

    def __init__(self, *, timeout=60.0, allow_private=lambda: False, resolver=resolve):
        self.timeout = timeout
        self.allow_private = allow_private
        self.resolver = resolver

    async def request(self, method, url, **kwargs):
        if not self.allow_private():
            try:
                address = await asyncio.to_thread(checked_address, url, resolver=self.resolver)
            except PartnerAddressError as error:
                raise PartnerAddressRefused(str(error)) from None
            url, kwargs = pinned_request(url, address, kwargs)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                response = await client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol, httpx.InvalidURL):
            raise PartnerUnreachable() from None
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body


def _flag(value):
    return value is not None and int(value) == 1


class DirectService:
    def __init__(self, engine, *, values, environment=None, http=None, resolver=resolve, resources=None, access=None):
        """``values`` returns the active configuration values; ``resources()`` the access runtime's inbound
        resources, which filing an arrival as a received fax needs (None until it is ready); ``access()`` the
        access runtime itself, which relaying a partner's fax as this installation's own needs (relay.py)."""
        self.access = access or (lambda: None)
        self.store = DirectStore(engine)
        self.values = values
        self.environment = environment or {}
        self.resolver = resolver
        self.http = http or HttpClient(allow_private=self._allow_private, resolver=resolver)
        self.filing = DirectFiling(self.store, resources or (lambda: None), values=values)

    def _allow_private(self):
        return bool(getattr(self.values(), 'direct_allow_private_peers', False))

    # Identity and card ------------------------------------------------------
    def _path(self):
        return identity_path(self.environment, self.values().fax_data_dir)

    def identity(self, *, create=False):
        return load_identity(self._path(), create=create)

    def ready(self):
        try:
            self.identity()
            return True
        except IdentityUnavailable:
            return False

    def own_card(self):
        values = self.values()
        from ..routing.numbers import InvalidNumber, normalize_number
        if not values.direct_organization.strip():
            raise DirectConflict('Add your organization name for direct delivery to the installation settings first.')
        try:
            number = normalize_number(values.direct_fax_number, country=values.fax_default_country)
        except InvalidNumber:
            raise DirectConflict('Add the fax number partners send to for direct delivery to the installation settings first.') from None
        return card(self.identity(create=True), organization=values.direct_organization.strip(), fax_number=number,
                    endpoint=values.public_api_url.rstrip('/'))

    def enroll(self, document):
        if isinstance(document, str):
            try:
                document = json.loads(document)
            except ValueError:
                raise DirectProtocolError('malformed', 'This is not a Faxbot direct delivery card.') from None
        fields = check_card(document)
        from urllib.parse import urlsplit
        endpoint = urlsplit(fields['endpoint'])
        if (self.values().enforce_public_https and endpoint.scheme != 'https'
                and endpoint.hostname not in {'localhost', '127.0.0.1', '::1'}):
            raise DirectConflict("The partner's address must use HTTPS.")
        if not self._allow_private():
            try:
                checked_address(fields['endpoint'], resolver=self.resolver)
            except PartnerAddressError as error:
                raise DirectConflict(str(error)) from None
        return self.store.add_peer(fields, own_signing_key=self.identity(create=True).signing_key)

    # Receiving --------------------------------------------------------------
    def _enabled_identity(self):
        values = self.values()
        if not values.direct_delivery_enabled:
            raise DirectUnavailable()
        try:
            return values, self.identity()
        except IdentityUnavailable:
            raise DirectUnavailable() from None

    @staticmethod
    def offered(peer):
        """What this installation accepts from ``peer``, as its signed answers tell the partner."""
        return capabilities(fax_images=accepts_fax_images(peer), peer_calls=_flag(peer.get('receive_peer_calls')))

    def _refusal(self, identity, message_id, reason, text, peer=None):
        statement = {'type': 'refusal', 'message_id': message_id, 'reason': reason, 'detail': text}
        if peer is not None:
            statement['capabilities'] = self.offered(peer)
        return signed(identity, statement)

    def _withdrawn(self, identity, message_id, peer=None):
        return self._refusal(identity, message_id, 'withdrawn',
                             'The sender asked about this document before it arrived, so it was not accepted.', peer)

    def heard(self, peer, statement):
        """Keep what a partner says it accepts from us, from any statement it signed."""
        found = parse_capabilities(statement.get('capabilities')) if isinstance(statement, dict) else None
        if found is None:
            return False
        fax_images, peer_calls, said_at = found
        return self.store.note_capabilities(peer['id'], fax_images=fax_images, peer_calls=peer_calls, said_at=said_at)

    def receive(self, manifest_bytes, signature, ciphertext, *, now=None):
        """Verify, decrypt, store unchanged and queue one document; returns (status, body)."""
        now = now or utcnow()
        values, identity = self._enabled_identity()
        try:
            manifest = parse_manifest(manifest_bytes)
        except DirectProtocolError as error:
            return 400, self._refusal(identity, None, error.reason, str(error))
        message_id = manifest['message_id']
        peer = self.store.peer_by_key(manifest['sender']['signing_key'])
        if peer is None or peer['state'] == 'revoked':
            return 403, self._refusal(identity, message_id, 'unknown_sender', 'This installation does not accept documents from this sender.')
        try:
            verify(peer['signing_key'], manifest_bytes, signature)
        except DirectProtocolError as error:
            return 403, self._refusal(identity, message_id, error.reason, str(error))
        from ..routing.numbers import InvalidNumber, normalize_number
        try:
            own_number = normalize_number(values.direct_fax_number, country=values.fax_default_country)
        except InvalidNumber:
            own_number = None
        if manifest['recipient']['signing_key'] != identity.signing_key or manifest['recipient']['fax_number'] != own_number:
            return 403, self._refusal(identity, message_id, 'wrong_recipient', 'This document is addressed to another recipient.', peer)
        existing = self.store.find('inbound', message_id)
        if existing is not None and existing['state'] == 'refused':
            return 409, self._withdrawn(identity, message_id, peer)
        if existing is not None:
            if existing['manifest'].encode('ascii') != manifest_bytes:
                return 409, self._refusal(identity, message_id, 'replay', 'This message id was already used for a different document.', peer)
            return 200, json.loads(existing['receipt'])
        created = parse_timestamp(manifest['created_at'])
        if abs(created - now) > FRESHNESS:
            return 403, self._refusal(identity, message_id, 'stale', 'This document was signed too long ago; send it again.', peer)
        if len(ciphertext) > MAX_DOCUMENT_BYTES:
            return 413, self._refusal(identity, message_id, 'too_large', 'This document is too large.', peer)
        kind = kind_of(manifest)
        if kind == FAX_IMAGE and not accepts_fax_images(peer):
            # Turned off for this partner; this signed refusal proves nothing was accepted, so the sender may fax it.
            return 409, self._refusal(identity, message_id, 'fax_images_off',
                                      'This installation does not accept fax images from you; send the original '
                                      'document instead.', peer)
        try:
            document = open_document(identity, manifest, ciphertext)
        except DirectProtocolError as error:
            return 400, self._refusal(identity, message_id, error.reason, str(error), peer)
        if kind == RELAY:
            # A document to send as a local call for the partner: queued as this installation's own fax within
            # the partner's agreement, never filed as a received fax (relay.py).
            from .relay import RelayService
            return RelayService(self, access=self.access).receive(identity, peer, manifest, document, now=now)
        if kind == FORM:
            # A registered form: the pages drawn here from its data are filed, and only when they match.
            from ..forms.exchange import receive_form
            refusal, document = receive_form(self, peer, manifest, document)
            if refusal is not None:
                return refusal[0], self._refusal(identity, message_id, refusal[1], refusal[2], peer)
        folder = Path(values.fax_data_dir) / 'direct'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if kind == FAX_IMAGE:
            # Every check that can refuse comes before acceptance: the image must match its signed facts
            # and turn into the PDF people read.
            try:
                faximage.check(document, manifest['fax'], manifest['document']['pages'])
                faximage.readable_copy(document, folder)
            except faximage.FaxImageInvalid as error:
                return 400, self._refusal(identity, message_id, 'not_fax_image', str(error), peer)
        elif not document.startswith(b'%PDF'):
            return 400, self._refusal(identity, message_id, 'not_pdf', 'Only PDF documents can be delivered directly.', peer)
        suffix = '.tiff' if kind == FAX_IMAGE else '.pdf'
        path = folder / f'{message_id}-{secrets.token_hex(8)}{suffix}'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())

        def receipt_for(local_id):
            receipt = {'type': 'receipt', 'message_id': message_id, 'status': 'accepted',
                       'document_sha256': manifest['document']['sha256'], 'recipient': manifest['recipient'],
                       'accepted_at': timestamp(), 'capabilities': self.offered(peer)}
            if kind == FAX_IMAGE:
                receipt['kind'] = FAX_IMAGE
            return signed(identity, receipt)
        try:
            row, created_now = self.store.accept_inbound(message_id=message_id, peer=peer, manifest=manifest_bytes,
                                                         receipt_for=receipt_for, document_path=str(path), now=now)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if not created_now:
            path.unlink(missing_ok=True)
            if row['state'] == 'refused':
                return 409, self._withdrawn(identity, message_id, peer)
            if row['manifest'].encode('ascii') != manifest_bytes:
                return 409, self._refusal(identity, message_id, 'replay', 'This message id was already used for a different document.', peer)
        else:
            try:
                self.filing.file(row)
            except Exception:
                # Accepted and stored; the filing step files it in Received shortly.
                logging.getLogger(__name__).warning('A document a partner delivered directly was accepted; Faxbot '
                                                    'files it in Received shortly.')
        return 200, json.loads(row['receipt'])

    def status(self, message_id, *, signer, request_time, signature, now=None):
        """Answer a sender's reconciliation question with a signed statement."""
        now = now or utcnow()
        _, identity = self._enabled_identity()
        peer = self.store.peer_by_key(signer) if isinstance(signer, str) else None
        if peer is None or peer['state'] == 'revoked' or not re.fullmatch(r'[a-f0-9]{32}', message_id or ''):
            return 403, {'detail': 'This request is not from an enrolled partner.'}
        try:
            verify(peer['signing_key'], f'GET /direct/deliveries/{message_id} {request_time}'.encode('ascii'), signature)
            if abs(parse_timestamp(request_time) - now) > REQUEST_SKEW:
                raise DirectProtocolError('stale', "The request time does not match this installation's clock.")
        except (DirectProtocolError, UnicodeEncodeError):
            return 403, {'detail': 'This request is not from an enrolled partner.'}
        row = self.store.answer_or_fence(message_id, peer, now=now)
        if row is None or row['peer_id'] != peer['id'] or row['state'] != 'accepted':
            return 200, signed(identity, {'type': 'status', 'message_id': message_id, 'status': 'not_received',
                                          'answered_at': timestamp(), 'capabilities': self.offered(peer)})
        return 200, {**signed(identity, {'type': 'status', 'message_id': message_id, 'status': 'accepted',
                                         'answered_at': timestamp(), 'capabilities': self.offered(peer)}),
                     'receipt': json.loads(row['receipt'])}

    def note(self, statement, signature, *, now=None):
        """A partner tells us, signed, what it accepts from us now (fax images, peer fax calls)."""
        now = now or utcnow()
        _, identity = self._enabled_identity()
        refused = (400, {'recorded': False, 'detail': 'This statement could not be checked.'})
        try:
            encoded = statement.encode('ascii')
            body = json.loads(encoded)
            if (not isinstance(body, dict) or set(body) != {'type', 'signer', 'recipient', 'capabilities'}
                    or body['type'] != 'capabilities' or body['recipient'] != identity.signing_key):
                return refused
            peer = self.store.peer_by_key(body['signer']) if isinstance(body['signer'], str) else None
            if peer is None or peer['state'] == 'revoked':
                return refused
            verify(peer['signing_key'], encoded, signature)
        except (ValueError, TypeError, UnicodeEncodeError, DirectProtocolError):
            return refused
        found = parse_capabilities(body['capabilities'])
        if found is None or abs(found[2] - now) > FRESHNESS:
            return refused
        self.heard(peer, body)
        return 200, {'recorded': True}

    async def tell_partner(self, peer):
        """Tell a partner, signed, what we accept from it now: ``told``, ``unreachable`` or ``unsupported``.

        A partner that could not be reached learns it from the next receipt,
        refusal or status answer we sign for it, which carry the same statement.
        ``unsupported`` is a partner whose Faxbot has no fax images yet (it
        answers 404 for the statement), so it keeps sending original documents.
        """
        identity = await run_lifecycle_step(lambda: self.identity(create=True))
        offered = self.offered(peer)
        envelope = signed(identity, {'type': 'capabilities', 'recipient': peer['signing_key'], 'capabilities': offered})
        try:
            status, body = await self.http.request('POST', peer['endpoint_url'] + '/direct/capabilities', json=envelope)
        except (PartnerUnreachable, httpx.HTTPError):
            return 'unreachable'
        if status == 200 and isinstance(body, dict) and body.get('recorded') is True:
            outcome = 'told'
        elif status in (404, 405):
            outcome = 'unsupported'
        else:
            return 'unreachable'
        # Told (or a Faxbot without fax images, which learns it from our answers once it has them): not asked again.
        await run_lifecycle_step(lambda: self.store.mark_told(peer['id'], fax_images=offered['fax_images'],
                                                              peer_calls=offered['peer_calls']))
        return outcome

    async def tell_partners(self):
        """Tell every enrolled partner not told yet what we accept from it: after an upgrade, an enrollment or a
        change, and again later for a partner that could not be reached."""
        if not getattr(await run_lifecycle_step(self.values), 'direct_delivery_enabled', False) or not self.ready():
            return False
        for peer in await run_lifecycle_step(self.store.untold):
            await self.tell_partner(peer)
        return False

    async def set_fax_images(self, peer_id, accept):
        """Accept fax images from a partner, or stop; returns (partner, ``told``, ``unreachable`` or ``unsupported``)."""
        peer = await run_lifecycle_step(lambda: self.store.set_receive_fax_images(peer_id, accept))
        return peer, await self.tell_partner(peer)

    def confirm(self, statement, signature, *, now=None):
        """A partner submits the code from our challenge fax, signed with its key."""
        now = now or utcnow()
        self._enabled_identity()
        try:
            encoded = statement.encode('ascii')
            body = json.loads(encoded)
            if not isinstance(body, dict) or set(body) != {'type', 'signing_key', 'code', 'created_at'} \
                    or body['type'] != 'verification':
                raise ValueError
            verify(body['signing_key'], encoded, signature)
            if abs(parse_timestamp(body['created_at']) - now) > FRESHNESS:
                raise ValueError
        except (ValueError, TypeError, UnicodeEncodeError, DirectProtocolError):
            return 400, {'verified': False, 'detail': 'This confirmation could not be checked.'}
        result, _ = self.store.confirm_code(body['signing_key'], body['code'], now=now)
        if result == 'verified':
            return 200, {'verified': True, 'detail': 'The code matched; direct delivery is verified.'}
        if result == 'mismatch':
            return 400, {'verified': False, 'detail': 'The code did not match.'}
        return 400, {'verified': False, 'detail': 'There is no open code for this partner; ask them to send a new one.'}

    # Sending ----------------------------------------------------------------
    async def send_confirmation(self, peer_id, code):
        """Our operator types the code from a partner's challenge fax; we prove it with our key."""
        peer = await run_lifecycle_step(lambda: self.store.get_peer(peer_id))
        if peer is None or peer['state'] == 'revoked':
            raise DirectConflict('This partner is not enrolled.')
        identity = await run_lifecycle_step(lambda: self.identity(create=True))
        statement = canonical({'type': 'verification', 'signing_key': identity.signing_key,
                               'code': ''.join(c for c in str(code) if c.isdigit()), 'created_at': timestamp()})
        try:
            status, body = await self.http.request('POST', peer['endpoint_url'] + '/direct/verifications', json={
                'statement': statement.decode('ascii'), 'signature': identity.sign(statement)})
        except PartnerAddressRefused as error:
            raise DirectConflict(str(error)) from None
        except PartnerUnreachable:
            raise DirectConflict('Faxbot could not reach the partner; check their address and try again.') from None
        except httpx.HTTPError:
            raise DirectConflict('The partner did not answer; try again.') from None
        detail = body.get('detail') if isinstance(body, dict) and isinstance(body.get('detail'), str) else None
        if status == 200 and isinstance(body, dict) and body.get('verified') is True:
            return 'The partner confirmed the code.'
        raise DirectConflict(detail or 'The partner did not accept the code.')

    def challenge_document(self, peer, code, organization):
        """The one-page challenge fax a partner reads the code from."""
        from io import BytesIO
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        output = BytesIO()
        pdf = canvas.Canvas(output, pagesize=letter)
        lines = [
            ('Helvetica-Bold', 16, 'Faxbot direct delivery code'),
            ('Helvetica', 12, f'{organization} would like to send documents directly to this fax number.'),
            ('Helvetica', 12, f'Partner on file: {peer["organization"]}, {peer["phone_number"]}.'),
            ('Helvetica', 12, 'If you expect this, enter this code in Faxbot under Delivery routes,'),
            ('Helvetica', 12, f'Direct partners, "Confirm a code", for {organization}:'),
            ('Helvetica-Bold', 28, f'{code[:4]} {code[4:]}'),
            ('Helvetica', 12, 'If you do not expect this, you can ignore this page. The code expires in 7 days.'),
        ]
        y = 700
        for font, size, text in lines:
            pdf.setFont(font, size)
            pdf.drawString(72, y, text)
            y -= size + 18
        pdf.showPage()
        pdf.save()
        return output.getvalue()

    def new_code(self):
        return f'{secrets.randbelow(10 ** 8):08d}'


class DirectRoute:
    """The delivery worker's direct route, used when a verified partner owns the number."""

    def __init__(self, service, *, preference=faximage.PEER_FIRST):
        """``preference``: ``peer_first`` or ``never_peer`` (``faximage.peer_route``); a rule sets it per fax later."""
        self.service = service
        self.preference = preference

    def ready(self):
        return self.service.ready()

    @asynccontextmanager
    async def prepare(self, claim, plan, job):
        service = self.service
        values = await run_lifecycle_step(service.values)
        pdf = Path(values.fax_data_dir) / (claim.job_id + '.pdf')
        if re.fullmatch('[a-f0-9]{32}', claim.job_id) is None or pdf.is_symlink() or not pdf.is_file():
            raise DirectRefused('The fax document is unavailable for direct delivery.')
        identity = await run_lifecycle_step(service.identity)
        # The partner's current record: what it last said it accepts decides the fax image.
        peer = await run_lifecycle_step(lambda: service.store.get_peer(plan.peer['id'])) or plan.peer
        route = faximage.peer_route(peer, preference=self.preference)
        if route is None and self.preference == faximage.NEVER_PEER:
            raise DirectRefused('A routing rule keeps this fax off direct delivery.')
        # An attempt prepared again (nothing was sent the first time) sends the same kind of document, and a fax
        # image keeps the time of its first preparation, so its bytes and digest stay those already recorded.
        earlier = await run_lifecycle_step(lambda: service.store.find('outbound', claim.attempt_id))
        wants_image = (earlier['kind'] == FAX_IMAGE if earlier is not None
                       else route is not None and route.kind == FAX_IMAGE)
        image, signed_at = None, None
        if wants_image:
            # The header's time is the manifest's signed time, so the image and its digest can be made again.
            signed_at = json.loads(earlier['manifest'])['created_at'] if earlier is not None else timestamp()
            try:
                image = await run_lifecycle_step(lambda: faximage.build(
                    values, claim.job_id, moment=parse_timestamp(signed_at)))
            except faximage.FaxImageUnavailable as error:
                # Nothing was sent; the original still goes directly, with no call. Said once per fax, so a partner
                # who opted in but keeps getting originals can be explained (no fax image tools in this image).
                logging.getLogger(__name__).warning('A fax went directly as the original document instead of a fax '
                                                    'image: %s', error)
                image, signed_at = None, None
        document = image.data if image is not None else await run_lifecycle_step(pdf.read_bytes)
        from ..routing.numbers import normalize_number
        sender_number = None
        try:
            sender_number = normalize_number(values.direct_fax_number, country=values.fax_default_country)
        except ValueError:
            sender_number = None
        message_id = claim.attempt_id
        manifest, signature, ciphertext = seal(
            identity, message_id=message_id, organization=values.direct_organization.strip() or 'Faxbot',
            fax_number=sender_number, recipient_number=peer['phone_number'],
            recipient_signing_key=peer['signing_key'], recipient_exchange_key=peer['exchange_key'],
            document=document, pages=image.pages if image is not None else job.get('pages'),
            fax=image.facts if image is not None else None, created_at=signed_at)
        await run_lifecycle_step(lambda: service.store.record_outbound(
            message_id=message_id, peer_id=peer['id'], job_id=claim.job_id, attempt_id=claim.attempt_id,
            recipient_number=peer['phone_number'], digest=hashlib.sha256(document).hexdigest(), size=len(document),
            manifest=manifest.decode('ascii'), kind=FAX_IMAGE if image is not None else None))
        yield _DirectSubmission(service, peer, message_id, manifest, signature, ciphertext,
                                hashlib.sha256(document).hexdigest())


async def _hear(service, peer, statement):
    """Keep what the partner's signed answer says it accepts from us; never changes the delivery's outcome."""
    if statement is None:
        return
    try:
        await run_lifecycle_step(lambda: service.heard(peer, statement))
    except Exception:
        logging.getLogger(__name__).warning("A partner's answer about fax images could not be kept.")


class _DirectSubmission:
    def __init__(self, service, peer, message_id, manifest, signature, ciphertext, digest):
        self.service, self.peer, self.message_id = service, peer, message_id
        self.manifest, self.signature, self.ciphertext, self.digest = manifest, signature, ciphertext, digest

    async def submit(self):
        service, peer = self.service, self.peer
        try:
            status, body = await service.http.request('POST', peer['endpoint_url'] + '/direct/deliveries', files={
                'manifest': (None, self.manifest, 'application/json'),
                'signature': (None, self.signature.encode('ascii'), 'text/plain'),
                'document': ('document.bin', self.ciphertext, 'application/octet-stream')})
        except PartnerUnreachable as error:
            await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'refused'))
            if isinstance(error, PartnerAddressRefused):
                raise DirectRefused(str(error)) from None
            raise DirectRefused('The partner could not be reached; nothing was sent.') from None
        except BaseException:
            await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'uncertain'))
            raise
        try:
            statement = check_signed(body, peer['signing_key'])
        except DirectProtocolError:
            statement = None
        await _hear(service, peer, statement)
        if statement is not None and statement.get('message_id') == self.message_id:
            if (status == 200 and statement.get('type') == 'receipt' and statement.get('status') == 'accepted'
                    and statement.get('document_sha256') == self.digest):
                await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'accepted', receipt=body))
                return SubmissionReceipt(None, 'success')
            if statement.get('type') == 'refusal' and 400 <= status < 500:
                await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'refused'))
                raise DirectRefused(statement.get('detail') or 'The partner refused the document.')
        # The partner may have accepted it; ask instead of sending again.
        await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'uncertain'))
        raise RuntimeError('The answer from the partner could not be confirmed.')


class DirectReconciler:
    """Ask partners about documents whose answer was lost; fall back to fax only on a signed no."""

    def __init__(self, service, delivery):
        self.service, self.delivery = service, delivery

    async def step(self):
        rows = await run_lifecycle_step(self.service.store.awaiting_partner)
        for row in rows:
            await self.reconcile(row)
        return False

    async def reconcile(self, row):
        service = self.service
        peer = await run_lifecycle_step(lambda: service.store.get_peer(row['peer_id'])) if row['peer_id'] else None
        if peer is None:
            return None
        identity = await run_lifecycle_step(service.identity)
        moment = timestamp()
        path = f"/direct/deliveries/{row['message_id']}"
        headers = {'X-Faxbot-Direct-Key': identity.signing_key, 'X-Faxbot-Direct-Time': moment,
                   'X-Faxbot-Direct-Signature': identity.sign(f'GET {path} {moment}'.encode('ascii'))}
        try:
            status, body = await service.http.request('GET', peer['endpoint_url'] + path, headers=headers)
            statement = check_signed(body, peer['signing_key'])
        except (PartnerUnreachable, httpx.HTTPError, DirectProtocolError):
            return None  # Ask again later; the fax stays waiting for confirmation.
        await _hear(service, peer, statement)
        if status != 200 or statement.get('type') != 'status' or statement.get('message_id') != row['message_id']:
            return None
        if statement.get('status') == 'accepted':
            receipt = body.get('receipt')
            try:
                accepted = check_signed(receipt, peer['signing_key'])
            except DirectProtocolError:
                return None
            if accepted.get('document_sha256') != row['digest'] or accepted.get('message_id') != row['message_id']:
                return None
            await run_lifecycle_step(lambda: service.store.mark_outbound(row['message_id'], 'accepted', receipt=receipt))
            _, profile = await run_lifecycle_step(lambda: self.delivery.attempt_context(row['job_id'], row['attempt_id']))
            await run_lifecycle_step(lambda: self.delivery.observe(
                row['job_id'], attempt_id=row['attempt_id'], profile_id=profile.id, provider_sid=None,
                status='success', event_key='direct:' + row['message_id']))
            return 'accepted'
        if statement.get('status') == 'not_received':
            await run_lifecycle_step(lambda: service.store.mark_outbound(row['message_id'], 'refused'))
            await run_lifecycle_step(lambda: self.delivery.requeue_after_failure(
                row['job_id'], attempt_id=row['attempt_id'], category='partner_not_received'))
            return 'not_received'
        return None
