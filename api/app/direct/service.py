"""Direct delivery protocol: receive, answer status, confirm codes, send and reconcile.

Receiving never needs an API key: every request is authenticated by an Ed25519
signature from an enrolled partner. Sending keeps the delivery worker's rule:
only a refusal that proves nothing was accepted allows the conventional fax
route in the same attempt; any other failure after upload is reconciled by
asking the partner, never by sending again.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

import httpx

from ..config_runtime import run_lifecycle_step
from ..outbound_worker import SubmissionReceipt
from ..routing.database import utcnow
from ..routing.transport import DirectRefused
from .crypto import (DirectProtocolError, card, canonical, check_card, check_signed, open_document, parse_manifest,
                     parse_timestamp, seal, signed, timestamp, verify)
from .identity import IdentityUnavailable, identity_path, load_identity
from .store import DirectConflict, DirectStore


FRESHNESS = timedelta(hours=24)
REQUEST_SKEW = timedelta(minutes=5)
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


class DirectUnavailable(RuntimeError):
    """Direct delivery is switched off or not set up on this installation."""


class PartnerUnreachable(RuntimeError):
    """Nothing reached the partner: the connection could not be opened."""


def _naive(moment):
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


class HttpClient:
    """Production transport to partner installations."""

    def __init__(self, *, timeout=60.0):
        self.timeout = timeout

    async def request(self, method, url, **kwargs):
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


class DirectService:
    def __init__(self, engine, *, values, environment=None, http=None):
        """``values`` returns the active configuration values."""
        self.store = DirectStore(engine)
        self.values = values
        self.environment = environment or {}
        self.http = http or HttpClient()

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
            number = normalize_number(values.direct_fax_number)
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

    def _refusal(self, identity, message_id, reason, text):
        return signed(identity, {'type': 'refusal', 'message_id': message_id, 'reason': reason, 'detail': text})

    def _withdrawn(self, identity, message_id):
        return self._refusal(identity, message_id, 'withdrawn',
                             'The sender asked about this document before it arrived, so it was not accepted.')

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
            own_number = normalize_number(values.direct_fax_number)
        except InvalidNumber:
            own_number = None
        if manifest['recipient']['signing_key'] != identity.signing_key or manifest['recipient']['fax_number'] != own_number:
            return 403, self._refusal(identity, message_id, 'wrong_recipient', 'This document is addressed to another recipient.')
        existing = self.store.find('inbound', message_id)
        if existing is not None and existing['state'] == 'refused':
            return 409, self._withdrawn(identity, message_id)
        if existing is not None:
            if existing['manifest'].encode('ascii') != manifest_bytes:
                return 409, self._refusal(identity, message_id, 'replay', 'This message id was already used for a different document.')
            return 200, json.loads(existing['receipt'])
        created = parse_timestamp(manifest['created_at'])
        if abs(created - now) > FRESHNESS:
            return 403, self._refusal(identity, message_id, 'stale', 'This document was signed too long ago; send it again.')
        if len(ciphertext) > MAX_DOCUMENT_BYTES:
            return 413, self._refusal(identity, message_id, 'too_large', 'This document is too large.')
        try:
            document = open_document(identity, manifest, ciphertext)
        except DirectProtocolError as error:
            return 400, self._refusal(identity, message_id, error.reason, str(error))
        if not document.startswith(b'%PDF'):
            return 400, self._refusal(identity, message_id, 'not_pdf', 'Only PDF documents can be delivered directly.')
        folder = Path(values.fax_data_dir) / 'direct'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / f'{message_id}-{secrets.token_hex(8)}.pdf'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())

        def receipt_for(local_id):
            return signed(identity, {'type': 'receipt', 'message_id': message_id, 'status': 'accepted',
                                     'document_sha256': manifest['document']['sha256'],
                                     'recipient': manifest['recipient'], 'accepted_at': timestamp()})
        try:
            row, created_now = self.store.accept_inbound(message_id=message_id, peer=peer, manifest=manifest_bytes,
                                                         receipt_for=receipt_for, document_path=str(path), now=now)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if not created_now:
            path.unlink(missing_ok=True)
            if row['state'] == 'refused':
                return 409, self._withdrawn(identity, message_id)
            if row['manifest'].encode('ascii') != manifest_bytes:
                return 409, self._refusal(identity, message_id, 'replay', 'This message id was already used for a different document.')
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
                                          'answered_at': timestamp()})
        return 200, {**signed(identity, {'type': 'status', 'message_id': message_id, 'status': 'accepted',
                                         'answered_at': timestamp()}), 'receipt': json.loads(row['receipt'])}

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

    def __init__(self, service):
        self.service = service

    def ready(self):
        return self.service.ready()

    @asynccontextmanager
    async def prepare(self, claim, plan, job):
        service = self.service
        values = await run_lifecycle_step(service.values)
        pdf = Path(values.fax_data_dir) / (claim.job_id + '.pdf')
        if re.fullmatch('[a-f0-9]{32}', claim.job_id) is None or pdf.is_symlink() or not pdf.is_file():
            raise DirectRefused('The fax document is unavailable for direct delivery.')
        document = await run_lifecycle_step(pdf.read_bytes)
        identity = await run_lifecycle_step(service.identity)
        peer = plan.peer
        from ..routing.numbers import normalize_number
        sender_number = None
        try:
            sender_number = normalize_number(values.direct_fax_number)
        except ValueError:
            sender_number = None
        message_id = claim.attempt_id
        manifest, signature, ciphertext = seal(
            identity, message_id=message_id, organization=values.direct_organization.strip() or 'Faxbot',
            fax_number=sender_number, recipient_number=peer['phone_number'],
            recipient_signing_key=peer['signing_key'], recipient_exchange_key=peer['exchange_key'],
            document=document, pages=job.get('pages'))
        await run_lifecycle_step(lambda: service.store.record_outbound(
            message_id=message_id, peer_id=peer['id'], job_id=claim.job_id, attempt_id=claim.attempt_id,
            recipient_number=peer['phone_number'], digest=hashlib.sha256(document).hexdigest(), size=len(document),
            manifest=manifest.decode('ascii')))
        yield _DirectSubmission(service, peer, message_id, manifest, signature, ciphertext,
                                hashlib.sha256(document).hexdigest())


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
        except PartnerUnreachable:
            await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'refused'))
            raise DirectRefused('The partner could not be reached; nothing was sent.') from None
        except BaseException:
            await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'uncertain'))
            raise
        try:
            statement = check_signed(body, peer['signing_key'])
        except DirectProtocolError:
            statement = None
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
