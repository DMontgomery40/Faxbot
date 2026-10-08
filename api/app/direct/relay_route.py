"""The sender's side of a partner relay: send a fax through the relay, then wait for its signed outcome.

``RelayRoute`` has the same contract as the direct route (``prepare(claim,
plan, job)``, plus the planner's choice): it seals the fax's original PDF as
the ``relay`` kind, records it, and its submission posts it to the partner.

- A signed receipt that the relay accepted it leaves the fax in progress
  (``SubmissionReceipt(None, 'in_progress')``): accepted for relaying is not
  delivered. The relay's signed outcome settles it later.
- A signed refusal proves nothing was accepted, so the fax may still go by
  its own route in the same attempt (``DirectRefused``), which the routed
  transport records as a fallback.
- Anything else may have reached the relay: the attempt is uncertain and the
  partner is asked (``RelayReconciler``), never sent again.

``RelayReconciler`` asks partners about uploads whose answer was lost, and
asks relays for outcomes that have not arrived.
"""
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re

import httpx

from ..config_runtime import run_lifecycle_step
from ..outbound_worker import SubmissionReceipt
from ..routing.database import utcnow
from ..routing.transport import DirectRefused
from .crypto import DirectProtocolError, check_signed, seal, timestamp
from .relay import KEY_PREFIX, RelayService, _price_of, check_terms, covers, own_prediction, predicted


ASK_AFTER = timedelta(minutes=5)


class RelayRoute:
    """The delivery worker's relay route, used when the planner chose ``relay:<partner>``."""

    def __init__(self, service, *, access=None, delivery=None):
        self.service = service
        self.relay = RelayService(service, access=access or getattr(service, 'access', None), delivery=delivery)

    def ready(self):
        return self.service.ready()

    def _agreement(self, peer_id, destination):
        for row in self.relay.store.agreements_for(peer_id=peer_id, role='sender', states=('active',)):
            terms = check_terms(json.loads(row['terms']))
            if terms is not None and covers(terms, destination):
                return row
        return None

    @asynccontextmanager
    async def prepare(self, claim, plan, job, choice=None):
        service, relay = self.service, self.relay
        values = await run_lifecycle_step(service.values)
        route = getattr(choice, 'route', None)
        peer_id = getattr(route, 'peer_id', None) or (route.key[len(KEY_PREFIX):] if route is not None else None)
        destination = getattr(plan, 'destination', None) or job['to_number']
        pdf = Path(values.fax_data_dir) / (claim.job_id + '.pdf')
        if re.fullmatch('[a-f0-9]{32}', claim.job_id) is None or pdf.is_symlink() or not pdf.is_file():
            raise DirectRefused('The fax document is unavailable for relaying.')
        peer = await run_lifecycle_step(lambda: service.store.get_peer(peer_id)) if peer_id else None
        if peer is None or peer['state'] != 'verified':
            raise DirectRefused('The relaying partner is not verified.')
        agreement = await run_lifecycle_step(lambda: self._agreement(peer_id, destination))
        if agreement is None:
            raise DirectRefused(f"There is no relay agreement in force with {peer['organization']} for this number.")
        identity = await run_lifecycle_step(service.identity)
        document = await run_lifecycle_step(pdf.read_bytes)
        from ..routing.numbers import InvalidNumber, normalize_number
        try:
            sender_number = normalize_number(values.direct_fax_number, country=values.fax_default_country)
        except (InvalidNumber, ValueError):
            sender_number = None
        message_id = claim.attempt_id
        facts = {'agreement': agreement['id'], 'destination': destination,
                 'together': bool(agreement['send_together'])}
        manifest, signature, ciphertext = seal(
            identity, message_id=message_id, organization=values.direct_organization.strip() or 'Faxbot',
            fax_number=sender_number, recipient_number=peer['phone_number'],
            recipient_signing_key=peer['signing_key'], recipient_exchange_key=peer['exchange_key'],
            document=document, pages=job.get('pages'), relay=facts)
        digest = hashlib.sha256(document).hexdigest()
        await run_lifecycle_step(lambda: service.store.record_outbound(
            message_id=message_id, peer_id=peer['id'], job_id=claim.job_id, attempt_id=claim.attempt_id,
            recipient_number=peer['phone_number'], digest=digest, size=len(document),
            manifest=manifest.decode('ascii'), kind='relay'))
        pages = job.get('pages')

        def costs():
            price = _price_of(relay.store, agreement)
            found = predicted(price, destination, pages, peer['organization'])
            bound = job.get('outbound_backend') or job.get('backend')
            own, _ = own_prediction(service.store.engine, values, bound, destination, pages)
            return (((found.cost.micros, found.cost.currency) if found is not None and found.cost is not None
                     else None),
                    ((own.cost.micros, own.cost.currency) if own is not None and own.cost is not None else None))
        cost, own = await run_lifecycle_step(costs)
        await run_lifecycle_step(lambda: relay.store.add_fax(
            role='sender', message_id=message_id, agreement_id=agreement['id'], peer_id=peer['id'],
            job_id=claim.job_id, attempt_id=claim.attempt_id, destination=destination, pages=pages,
            state='sending', cost=cost, own_route=own))
        yield _RelaySubmission(self, peer, message_id, manifest, signature, ciphertext, digest)


class _RelaySubmission:
    def __init__(self, route, peer, message_id, manifest, signature, ciphertext, digest):
        self.route, self.peer, self.message_id = route, peer, message_id
        self.manifest, self.signature, self.ciphertext, self.digest = manifest, signature, ciphertext, digest

    async def _move(self, state, *, receipt=None, detail=None):
        service, store = self.route.service, self.route.relay.store
        await run_lifecycle_step(lambda: service.store.mark_outbound(
            self.message_id, 'accepted' if state == 'accepted' else 'refused' if state == 'refused' else 'uncertain',
            receipt=receipt))
        row = await run_lifecycle_step(lambda: store.fax(role='sender', message_id=self.message_id))
        if row is not None:
            await run_lifecycle_step(lambda: store.move_fax(
                row['id'], state, expected=('sending',), **({'detail': detail[:300]} if detail else {})))

    async def submit(self):
        from .service import PartnerAddressRefused, PartnerUnreachable, _hear
        service, peer = self.route.service, self.peer
        try:
            status, body = await service.http.request('POST', peer['endpoint_url'] + '/direct/deliveries', files={
                'manifest': (None, self.manifest, 'application/json'),
                'signature': (None, self.signature.encode('ascii'), 'text/plain'),
                'document': ('document.bin', self.ciphertext, 'application/octet-stream')})
        except PartnerUnreachable as error:
            await self._move('refused', detail='The partner could not be reached; nothing was sent.')
            if isinstance(error, PartnerAddressRefused):
                raise DirectRefused(str(error)) from None
            raise DirectRefused('The relaying partner could not be reached; nothing was sent.') from None
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
                    and statement.get('kind') == 'relay' and statement.get('document_sha256') == self.digest):
                await self._move('accepted', receipt=body)
                # Accepted for relaying, not delivered: the relay's signed outcome settles the fax.
                return SubmissionReceipt(None, 'in_progress')
            if statement.get('type') == 'refusal' and 400 <= status < 500:
                detail = statement.get('detail') if isinstance(statement.get('detail'), str) else None
                await self._move('refused', detail=detail)
                raise DirectRefused(detail or 'The relaying partner did not accept the fax.')
        # The partner may have accepted it; ask instead of sending again.
        await run_lifecycle_step(lambda: service.store.mark_outbound(self.message_id, 'uncertain'))
        raise RuntimeError('The answer from the relaying partner could not be confirmed.')


class RelayReconciler:
    """Ask partners about relayed uploads whose answer was lost, and about outcomes that have not arrived."""

    def __init__(self, service, delivery, *, access=None):
        self.service, self.delivery = service, delivery
        self.relay = RelayService(service, access=access or getattr(service, 'access', None),
                                  delivery=lambda: delivery)

    async def step(self):
        values = await run_lifecycle_step(self.service.values)
        if not getattr(values, 'direct_delivery_enabled', False) or not self.service.ready():
            return False
        for row in await run_lifecycle_step(self._lost):
            await self.reconcile(row)
        cutoff = utcnow() - ASK_AFTER
        waiting = await run_lifecycle_step(lambda: self.relay.store.faxes_in(role='sender',
                                                                             states=('accepted', 'uncertain')))
        for fax in waiting:
            if fax['updated_at'] <= cutoff:
                await self.relay.ask_outcome(fax)
        return False

    def _lost(self):
        import sqlalchemy as sa
        from ..routing.database import read_connection
        d, o = self.service.store.deliveries, self.service.store.outbound
        query = (sa.select(d).join(o, o.c.id == d.c.job_id).where(
            d.c.direction == 'outbound', d.c.kind == 'relay', d.c.state.in_(('sending', 'uncertain')),
            o.c.state == 'reconciliation_required', o.c.attempt_id == d.c.attempt_id)
            .order_by(d.c.updated_at, d.c.id).limit(20))
        with read_connection(self.service.store.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    async def reconcile(self, row):
        """Ask the relay whether a relayed upload arrived: accepted means in progress, never delivered."""
        from .service import PartnerUnreachable
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
        except (PartnerUnreachable, httpx.HTTPError, DirectProtocolError, OSError):
            return None
        if status != 200 or statement.get('type') != 'status' or statement.get('message_id') != row['message_id']:
            return None
        store = self.relay.store
        fax = await run_lifecycle_step(lambda: store.fax(role='sender', message_id=row['message_id']))
        if statement.get('status') == 'accepted':
            try:
                accepted = check_signed(body.get('receipt'), peer['signing_key'])
            except DirectProtocolError:
                return None
            if (accepted.get('document_sha256') != row['digest'] or accepted.get('message_id') != row['message_id']
                    or accepted.get('kind') != 'relay'):
                return None
            await run_lifecycle_step(lambda: service.store.mark_outbound(row['message_id'], 'accepted',
                                                                         receipt=body['receipt']))
            if fax is not None:
                await run_lifecycle_step(lambda: store.move_fax(fax['id'], 'accepted', expected=('sending',)))
            _, profile = await run_lifecycle_step(lambda: self.delivery.attempt_context(row['job_id'],
                                                                                        row['attempt_id']))
            # In progress at the relay; its signed outcome settles the fax.
            await run_lifecycle_step(lambda: self.delivery.observe(
                row['job_id'], attempt_id=row['attempt_id'], profile_id=profile.id, provider_sid=None,
                status='in_progress', event_key='relay:' + row['message_id'] + ':accepted'))
            return 'accepted'
        if statement.get('status') == 'not_received':
            await run_lifecycle_step(lambda: service.store.mark_outbound(row['message_id'], 'refused'))
            if fax is not None:
                await run_lifecycle_step(lambda: store.move_fax(
                    fax['id'], 'refused', expected=('sending',),
                    detail='The relaying partner confirmed it never arrived; nothing was sent.'))
            await run_lifecycle_step(lambda: self.delivery.requeue_after_failure(
                row['job_id'], attempt_id=row['attempt_id'], category='partner_not_received'))
            return 'not_received'
        return None
