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


class CertificateChanged(PartnerUnreachable):
    """Nothing was sent: the partner's certificate is not the one pinned for it (checked at the handshake)."""

    def __init__(self, seen):
        super().__init__(CERTIFICATE_CHANGED)
        self.seen = seen


CERTIFICATE_CHANGED = ("This partner's certificate is not the one it had when you enrolled it, so Faxbot sent it "
                       'nothing. Check with the partner that they replaced it.')


class HttpClient:
    """Production transport to partner installations.

    Unless private partners are allowed, each request first checks that the
    partner's host resolves only to public addresses, then connects to the
    address it checked (see addresses.py).

    A partner on your own network may use its own certificate instead of one
    from a trusted authority; Builder AT's discovery keeps its SHA-256 as a pin
    (``direct_certificate_pins``). ``pins(host)`` returns that pin, or None.
    A pinned host is accepted with exactly that certificate, compared right
    after the TLS handshake and before a single byte of the request is
    written, so a changed certificate means nothing was sent. Every other host
    is verified normally against trusted authorities; there is never a
    request without verification. ``changed(host, seen)`` hears about a
    pinned host whose certificate changed (seen is None when it matched again).
    """

    def __init__(self, *, timeout=60.0, allow_private=lambda: False, resolver=resolve, pins=lambda host: None,
                 changed=lambda host, seen: None):
        self.timeout = timeout
        self.allow_private = allow_private
        self.resolver = resolver
        self.pins = pins
        self.changed = changed

    async def request(self, method, url, **kwargs):
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        pin = await asyncio.to_thread(self.pins, parts.hostname) if parts.hostname else None
        if pin is not None and parts.scheme != 'https':
            raise PartnerAddressRefused("This partner's certificate is pinned, but its address does not use HTTPS.")
        if not self.allow_private():
            try:
                address = await asyncio.to_thread(checked_address, url, resolver=self.resolver)
            except PartnerAddressError as error:
                raise PartnerAddressRefused(str(error)) from None
            url, kwargs = pinned_request(url, address, kwargs)
        verify = True
        if pin is not None:
            verify, kwargs = _pinned_certificate(pin, kwargs)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, verify=verify,
                                         trust_env=False) as client:
                response = await client.request(method, url, **kwargs)
        except CertificateChanged as error:
            await asyncio.to_thread(self.changed, parts.hostname, error.seen)
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol, httpx.InvalidURL):
            raise PartnerUnreachable() from None
        if pin is not None:
            await asyncio.to_thread(self.changed, parts.hostname, None)
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body


def _pinned_certificate(pin, kwargs):
    """The TLS settings and request options that accept exactly the certificate whose SHA-256 is ``pin``.

    The chain and name are not checked (a partner's own certificate has no trusted authority); instead the
    httpcore ``trace`` hook reads the certificate the handshake received and refuses any other one before the
    request is written.
    """
    import ssl
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    async def trace(event, info):
        if event != 'connection.start_tls.complete':
            return
        stream = info.get('return_value')
        ssl_object = stream.get_extra_info('ssl_object') if stream is not None else None
        der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
        seen = hashlib.sha256(der).hexdigest() if der else None
        if seen != pin:
            try:
                await stream.aclose()
            finally:
                raise CertificateChanged(seen)
    options = dict(kwargs)
    options['extensions'] = {**(kwargs.get('extensions') or {}), 'trace': trace}
    return context, options


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
        self.http = http or HttpClient(allow_private=self._allow_private, resolver=resolver,
                                       pins=self.store.certificate_pin, changed=self.store.note_certificate)
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

    def receive(self, manifest_bytes, signature, ciphertext, *, now=None, routing=None, carriage=None):
        """Verify, decrypt, store unchanged and queue one document; returns (status, body).

        A document for another of this installation's numbers is accepted only under an active "send once"
        agreement with that partner that covers it (``distribute.py``). ``routing`` is the signed list of every
        recipient of such a send, as (statement, signature), sent with its first document. ``carriage`` is how the
        bytes come when not as ``ciphertext`` (``reuse.Reference`` or ``reuse.Patch``); when the partner lacks what
        it needs, the signed refusal records nothing, so the whole document may follow under the same message ID.
        """
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
        intake = None
        if manifest['recipient']['signing_key'] == identity.signing_key and manifest['recipient']['fax_number'] != own_number \
                and kind_of(manifest) == 'original':
            from .distribute import SendOnce
            intake = SendOnce(self).covering_received(peer, manifest['recipient']['fax_number'])
        if manifest['recipient']['signing_key'] != identity.signing_key or (
                manifest['recipient']['fax_number'] != own_number and intake is None):
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
        routed = None
        if routing is not None:
            from .distribute import RoutingRefused, SendOnce
            try:
                routed = SendOnce(self).check_routing(identity, peer, manifest, routing)
            except RoutingRefused as error:
                return 409, self._refusal(identity, message_id, error.reason, str(error), peer)
        if carriage is not None:
            from .reuse import CarriageMiss
            if kind != 'original':
                return 400, self._refusal(identity, message_id, 'malformed', 'Only original documents can be sent '
                                                                             'this way.', peer)
            try:
                document = carriage.document(self, identity, peer, manifest)
            except CarriageMiss as error:
                # Nothing is recorded: the whole document may come next under the same message ID.
                return 409, self._refusal(identity, message_id, error.reason, str(error), peer)
        else:
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

        # A document whose sender linked a notice fax to it first is held out of Received until that fax
        # arrives (notice.py); its receipt says so. Stored and accepted is still accepted.
        from .notice import NoticeReceiver
        held = NoticeReceiver(self).held(peer, message_id, manifest['document']['sha256'])
        # Filed at the intake for one of its numbers: the receipt says where its rules file it, and for a send's
        # first document where they file every recipient (distribute.py).
        intake_facts = None
        if intake is not None or routed is not None:
            from .distribute import SendOnce
            send_once = SendOnce(self)
            if routed is not None:
                agreement, recipients = routed
                placements = {number: send_once.placement(number, from_number=manifest['sender']['fax_number'],
                                                          now=now) for number in recipients}
                try:
                    send_once.record_received(agreement=agreement, peer=peer, manifest=manifest, routing=routing,
                                              recipients=recipients, placements=placements, now=now)
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
                intake_facts = send_once.receipt_facts(peer, manifest, recipients=recipients, placements=placements,
                                                       now=now)
            else:
                intake_facts = send_once.receipt_facts(peer, manifest, now=now)

        def receipt_for(local_id):
            receipt = {'type': 'receipt', 'message_id': message_id, 'status': 'accepted',
                       'document_sha256': manifest['document']['sha256'], 'recipient': manifest['recipient'],
                       'accepted_at': timestamp(), 'capabilities': self.offered(peer)}
            if kind == FAX_IMAGE:
                receipt['kind'] = FAX_IMAGE
            if held is not None:
                receipt['held_for_notice'] = True
            if intake_facts:
                receipt.update(intake_facts)
            if carriage is not None:
                receipt['carriage'] = carriage.kind
                if getattr(carriage, 'base', None):
                    receipt.update(base_sha256=carriage.base, delta_size=carriage.delta_size)
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
        elif held is None:
            if kind == FAX_IMAGE:
                # The pages missing after a broken call: filed as one fax with the call's pages (repair.py).
                from .repair import CallRepair
                repairs = CallRepair(self)
                offer = repairs.offered(peer, message_id)
                if offer is not None:
                    try:
                        repairs.complete(offer, document, folder)
                    except Exception:
                        logging.getLogger(__name__).warning('The pages a partner sent to complete a broken call '
                                                            'are filed on their own.')
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
            # The sender never got these pages here: an offer for them (repair.py) will never be completed.
            from .repair import RepairStore
            RepairStore(self.store.engine).expire_offers(now=now, message_id=message_id, peer_id=peer['id'])
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
        except (PartnerAddressRefused, CertificateChanged) as error:
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
        from . import notice
        if await run_lifecycle_step(lambda: notice.is_notice_job(service.store.engine, claim.job_id)):
            # The notice page exists for its fax event: it always goes by telephone.
            raise DirectRefused('A notice fax always goes by telephone.')
        identity = await run_lifecycle_step(service.identity)
        # The partner's current record: what it last said it accepts decides the fax image.
        peer = await run_lifecycle_step(lambda: service.store.get_peer(plan.peer['id'])) or plan.peer
        route = faximage.peer_route(peer, preference=self.preference)
        if route is None and self.preference == faximage.NEVER_PEER:
            raise DirectRefused('A routing rule keeps this fax off direct delivery.')
        # One of the partner's other numbers, filed by its intake under a "send once" agreement (distribute.py):
        # sealed for that number, always as the original document.
        from .distribute import SendOnce
        send_once = SendOnce(service)
        to_number = job.get('to_number') or peer['phone_number']
        agreement = await run_lifecycle_step(lambda: send_once.store.covering(peer['id'], to_number, role='sender'))
        if agreement is None and to_number != peer['phone_number']:
            raise DirectRefused("The partner's intake no longer takes faxes for this number.")
        recipient_number = to_number if agreement is not None else peer['phone_number']
        # A partner whose intake needs a fax event gets the original directly and a one-page notice by fax.
        with_notice = notice.wants_notice(peer) and agreement is None
        # An attempt prepared again (nothing was sent the first time) sends the same kind of document, and a fax
        # image keeps the time of its first preparation, so its bytes and digest stay those already recorded.
        earlier = await run_lifecycle_step(lambda: service.store.find('outbound', claim.attempt_id))
        wants_image = (earlier['kind'] == FAX_IMAGE if earlier is not None
                       else route is not None and route.kind == FAX_IMAGE and not with_notice and agreement is None)
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
        from .transfer import TransferSender
        staged = await run_lifecycle_step(lambda: TransferSender(service).staged(message_id))
        if staged is not None:
            # A transfer of this attempt is still open: the partner holds pieces of exactly these bytes.
            manifest, signature, ciphertext = staged
        else:
            manifest, signature, ciphertext = seal(
                identity, message_id=message_id, organization=values.direct_organization.strip() or 'Faxbot',
                fax_number=sender_number, recipient_number=recipient_number,
                recipient_signing_key=peer['signing_key'], recipient_exchange_key=peer['exchange_key'],
                document=document, pages=image.pages if image is not None else job.get('pages'),
                fax=image.facts if image is not None else None, created_at=signed_at)
        digest = hashlib.sha256(document).hexdigest()
        await run_lifecycle_step(lambda: service.store.record_outbound(
            message_id=message_id, peer_id=peer['id'], job_id=claim.job_id, attempt_id=claim.attempt_id,
            recipient_number=recipient_number, digest=digest, size=len(document),
            manifest=manifest.decode('ascii'), kind=FAX_IMAGE if image is not None else None))
        routing = None
        if agreement is not None and image is None and staged is None:
            # The first fax of a send carries the document and the signed list of every recipient; the others
            # then send only a reference to it (reuse.py).
            routing = await run_lifecycle_step(lambda: _first_of_send(
                send_once, identity, peer, agreement, claim, recipient_number, digest, len(document), values))
        yield _DirectSubmission(service, peer, message_id, manifest, signature, ciphertext, digest,
                                notice_job=claim.job_id if with_notice and image is None else None,
                                routing=routing, job_id=claim.job_id,
                                document=document if image is None and staged is None and not with_notice else None)


def _first_of_send(send_once, identity, peer, agreement, claim, number, digest, size, values):
    """The signed routing statement when this fax leads a send of several recipients; None otherwise."""
    existing = send_once.store.send('sender', claim.attempt_id)
    if existing is not None:
        return {'statement': existing['routing_statement'], 'signature': existing['routing_signature']}
    if send_once.store.live_send_for_job(claim.job_id) is not None:
        return None  # named by a send that already went: this fax sends a reference to that copy
    others = send_once.gather(job_id=claim.job_id, to_number=number, agreement=agreement, digest=digest,
                              values=values)
    if not others:
        return None
    _, envelope = send_once.start(identity=identity, peer=peer, agreement=agreement, message_id=claim.attempt_id,
                                  job_id=claim.job_id, to_number=number, digest=digest, size=size, others=others)
    return envelope


async def _hear(service, peer, statement):
    """Keep what the partner's signed answer says it accepts from us; never changes the delivery's outcome."""
    if statement is None:
        return
    try:
        await run_lifecycle_step(lambda: service.heard(peer, statement))
    except Exception:
        logging.getLogger(__name__).warning("A partner's answer about fax images could not be kept.")


class _DirectSubmission:
    def __init__(self, service, peer, message_id, manifest, signature, ciphertext, digest, *, notice_job=None,
                 routing=None, job_id=None, document=None):
        """``notice_job``: the original fax's ID when a notice fax goes with this document (notice.py).
        ``routing``: the signed list of a send's recipients, sent with its first document (distribute.py).
        ``document``: the original's bytes when it may go as a reference or as changes (reuse.py)."""
        self.service, self.peer, self.message_id = service, peer, message_id
        self.manifest, self.signature, self.ciphertext, self.digest = manifest, signature, ciphertext, digest
        self.notice_job, self.routing, self.job_id, self.document = notice_job, routing, job_id, document

    async def submit(self):
        if self.notice_job is None:
            return await self._submit()
        from .notice import NoticeSender
        notices = NoticeSender(self.service)
        identity = await run_lifecycle_step(self.service.identity)
        # The signed link goes first, so the partner holds the document until the notice fax arrives.
        reason = await notices.announce(identity, self.peer, message_id=self.message_id, digest=self.digest,
                                        job_id=self.notice_job)
        if reason is not None:
            await run_lifecycle_step(lambda: self.service.store.mark_outbound(self.message_id, 'refused'))
            await run_lifecycle_step(lambda: notices.cancel(self.message_id))
            raise DirectRefused(reason)
        try:
            return await self._submit()
        except DirectRefused:
            await run_lifecycle_step(lambda: notices.cancel(self.message_id))
            raise

    async def _submit(self):
        service, peer = self.service, self.peer
        if self.routing is not None:
            # A send's first document, with every recipient: the partner's intake files each when its fax arrives.
            outcome = await self.deliver('/direct/distributions', fallback=(), files={
                'manifest': (None, self.manifest, 'application/json'),
                'signature': (None, self.signature.encode('ascii'), 'text/plain'),
                'routing': (None, self.routing['statement'].encode('ascii'), 'application/json'),
                'routing_signature': (None, self.routing['signature'].encode('ascii'), 'text/plain'),
                'document': ('document.bin', self.ciphertext, 'application/octet-stream')})
            if outcome is None:
                await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'refused'))
                raise DirectRefused('The partner is not taking documents at its intake now; nothing was sent.')
            return outcome
        if self.document is not None:
            # The partner may already hold this document, or an earlier version of it (reuse.py).
            from .reuse import offer
            outcome = await offer(self)
            if outcome is not None:
                return outcome
        from .transfer import TransferSender, TransferUnsupported
        if TransferSender.wanted(service, self.ciphertext):
            # A large document goes in pieces: preflight, only the pieces missing after a drop, one commit.
            try:
                identity = await run_lifecycle_step(service.identity)
                outcome, detail = await TransferSender(service).deliver(
                    identity, peer, self.message_id, self.manifest, self.signature, self.ciphertext, self.digest)
            except TransferUnsupported:
                outcome = None  # Nothing was sent; the whole document goes in one request below.
            except PartnerUnreachable as error:
                await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'refused'))
                if isinstance(error, (PartnerAddressRefused, CertificateChanged)):
                    raise DirectRefused(str(error)) from None
                raise DirectRefused('The partner could not be reached; nothing was sent.') from None
            except BaseException:
                # Pieces may have reached the partner: the reconciler resumes the transfer instead of resending.
                await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'uncertain'))
                raise
            if outcome == 'accepted':
                await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'accepted',
                                                                              receipt=detail))
                await self.accepted()
                return SubmissionReceipt(None, 'success')
            if outcome == 'refused':
                await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'refused'))
                raise DirectRefused(detail)
        return await self.deliver('/direct/deliveries', optional=False, files={
            'manifest': (None, self.manifest, 'application/json'),
            'signature': (None, self.signature.encode('ascii'), 'text/plain'),
            'document': ('document.bin', self.ciphertext, 'application/octet-stream')})

    async def deliver(self, path, *, fallback=(), optional=True, **request):
        """POST this document to ``path`` and settle it from the partner's signed answer.

        Returns the SubmissionReceipt once the partner accepted it, or None when it signed one of ``fallback``'s
        reasons or (``optional``) has no such path: nothing was accepted, so the whole document may go now. Like
        every direct send: nothing sent is a refusal, anything else unconfirmed is asked about later, never sent
        again.
        """
        service, peer = self.service, self.peer
        try:
            status, body = await service.http.request('POST', peer['endpoint_url'] + path, **request)
        except PartnerUnreachable as error:
            await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'refused'))
            if isinstance(error, (PartnerAddressRefused, CertificateChanged)):
                raise DirectRefused(str(error)) from None
            raise DirectRefused('The partner could not be reached; nothing was sent.') from None
        except BaseException:
            await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'uncertain'))
            raise
        try:
            statement = check_signed(body, peer['signing_key'])
        except DirectProtocolError:
            statement = None
        if optional and statement is None and status in (404, 405):
            return None  # This path is not there (direct delivery off, or an older Faxbot): nothing was accepted.
        await _hear(service, peer, statement)
        if statement is not None and statement.get('message_id') == self.message_id:
            if (status == 200 and statement.get('type') == 'receipt' and statement.get('status') == 'accepted'
                    and statement.get('document_sha256') == self.digest):
                await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'accepted', receipt=body))
                await self.accepted()
                return SubmissionReceipt(None, 'success')
            if statement.get('type') == 'refusal' and 400 <= status < 500:
                if statement.get('reason') in fallback:
                    return None
                await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'refused'))
                raise DirectRefused(statement.get('detail') or 'The partner refused the document.')
        # The partner may have accepted it; ask instead of sending again.
        await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'uncertain'))
        raise RuntimeError('The answer from the partner could not be confirmed.')

    async def accepted(self):
        """After the partner's signed receipt: what follows an accepted document (a notice fax's queueing)."""
        await accepted_followups(self.service, self.message_id)


async def accepted_followups(service, message_id):
    """Work that follows a partner's receipt for ``message_id``; it never changes the delivery's outcome."""
    try:
        from .notice import NoticeSender
        await run_lifecycle_step(lambda: NoticeSender(service).original_accepted(message_id))
    except Exception:
        logging.getLogger(__name__).warning('The notice fax for a document delivered directly is queued shortly.')
    try:
        # The bytes a reference or changes saved, from the partner's signed receipt (reuse.py).
        from .reuse import record_from_receipt
        await run_lifecycle_step(lambda: record_from_receipt(service, message_id))
    except Exception:
        logging.getLogger(__name__).warning('The bytes saved by a direct delivery could not be counted.')


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
        # A document sent in pieces is finished first: only what the partner says it lacks, then one commit.
        from .transfer import TransferSender
        resumed = await TransferSender(service).resume(row)
        if resumed == 'waiting':
            return None  # The partner could not be reached; asked again later.
        if resumed is not None:
            receipt = resumed[1]
            await run_lifecycle_step(lambda: service.store.mark_outbound(row['message_id'], 'accepted', receipt=receipt))
            await accepted_followups(service, row['message_id'])
            _, profile = await run_lifecycle_step(lambda: self.delivery.attempt_context(row['job_id'], row['attempt_id']))
            await run_lifecycle_step(lambda: self.delivery.observe(
                row['job_id'], attempt_id=row['attempt_id'], profile_id=profile.id, provider_sid=None,
                status='success', event_key='direct:' + row['message_id']))
            return 'accepted'
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
            await accepted_followups(service, row['message_id'])
            _, profile = await run_lifecycle_step(lambda: self.delivery.attempt_context(row['job_id'], row['attempt_id']))
            await run_lifecycle_step(lambda: self.delivery.observe(
                row['job_id'], attempt_id=row['attempt_id'], profile_id=profile.id, provider_sid=None,
                status='success', event_key='direct:' + row['message_id']))
            return 'accepted'
        if statement.get('status') == 'not_received':
            await run_lifecycle_step(lambda: service.store.mark_outbound(row['message_id'], 'refused'))
            from .notice import NoticeSender
            await run_lifecycle_step(lambda: NoticeSender(service).cancel(row['message_id']))
            await run_lifecycle_step(lambda: self.delivery.requeue_after_failure(
                row['job_id'], attempt_id=row['attempt_id'], category='partner_not_received'))
            return 'not_received'
        return None
