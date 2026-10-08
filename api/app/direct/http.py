"""Direct delivery: partner enrollment for operators, and the signed partner protocol.

Operator routes use settings:read and settings:write (plus fax:send to send a
challenge fax). Partner routes carry no API key; each request is authenticated
by an Ed25519 signature from an enrolled partner and they answer 404 while
direct delivery is switched off. A partner's fax images are accepted only after
the operator turns them on for that partner; the partner is told with a signed
statement (``POST /direct/capabilities``) and in every signed answer after.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat_async
from ..routing.database import DeliveryStoreError
from ..routing.submit import accept_generated_fax
from .crypto import DirectProtocolError
from .identity import IdentityUnavailable
from .service import CERTIFICATE_CHANGED, DirectReconciler, DirectService, DirectUnavailable, MAX_DOCUMENT_BYTES
from .store import DirectConflict, accepts_fax_images


def service_for(app):
    engine, runtime = installation_engine(app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    http = getattr(app.state, 'direct_http', None)  # Replaced only by tests.

    def resources():
        access = getattr(app.state, 'access_runtime', None)
        return access.inbound if access is not None else None
    try:
        return DirectService(engine, values=lambda: runtime.manager.store.read().active.values,
                             environment=runtime.environment, http=http, resources=resources,
                             access=lambda: getattr(app.state, 'access_runtime', None))
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct delivery storage is unavailable.') from None


def _background(app):
    engine, runtime = installation_engine(app)
    if engine is None:
        return []
    from ..outbound_store import OutboundStore
    service = service_for(app)
    reconciler = DirectReconciler(service, OutboundStore(runtime.manager.store))

    async def file_arrivals():
        return await run_lifecycle_step(service.filing.step)
    return [('faxbot-direct-reconcile', repeat_async(reconciler.step, interval=60.0, initial_delay=20.0,
                                                     warning='Direct delivery confirmations are temporarily unavailable.')),
            # A document accepted just before a restart is filed in Received here.
            ('faxbot-direct-filing', repeat_async(file_arrivals, interval=60.0, initial_delay=15.0,
                                                  warning='Documents partners delivered directly are waiting to be '
                                                          'filed in Received.')),
            # Partners learn what we accept from them after an upgrade, an enrollment or a change (signed).
            ('faxbot-direct-tell', repeat_async(service.tell_partners, interval=600.0, initial_delay=30.0,
                                                warning='Partners could not be told about fax images yet; Faxbot '
                                                        'tries again.'))]


router = APIRouter(prefix='/direct', tags=['Direct delivery'], lifespan=lifespan_tasks(_background))


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except DirectProtocolError as error:
        raise HTTPException(400, detail=str(error)) from None
    except DirectConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except IdentityUnavailable:
        raise HTTPException(503, detail='Direct delivery keys are unavailable on this installation.') from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct delivery storage is unavailable.') from None


STATE_TEXT = {
    'pending': 'Waiting for the partner to confirm the code from our challenge fax.',
    'verified': 'Verified; faxes to this number are delivered directly.',
    'revoked': 'Removed; faxes to this number go by fax.',
}


def _flag(value):
    return value is not None and int(value) == 1


def fax_images_text(peer):
    """One sentence on fax images with this partner, or None when there is nothing to say."""
    if peer['state'] == 'revoked':
        return None
    receive, send = accepts_fax_images(peer), _flag(peer.get('partner_receives_fax_images'))
    if send and peer['state'] == 'verified':
        if receive:
            return 'Faxes go both ways as the exact fax image, with no telephone call.'
        return 'Your faxes to them go as the exact fax image, with no telephone call.'
    if receive:
        return 'Their faxes to you arrive as the exact fax image and are filed like any received fax.'
    return None


def _peer_view(peer, now=None):
    now = now or datetime.utcnow()
    open_code = peer['challenge_expires_at'] is not None and peer['challenge_expires_at'] > now
    status = STATE_TEXT[peer['state']]
    if peer['state'] == 'pending' and not open_code:
        status = 'Send a challenge fax so the partner can confirm this number.'
    return {'id': peer['id'], 'organization': peer['organization'], 'fax_number': peer['phone_number'],
            'endpoint': peer['endpoint_url'], 'state': peer['state'], 'status': status,
            'code_sent': open_code, 'code_expires_at': peer['challenge_expires_at'] if open_code else None,
            'verified_at': peer['verified_at'], 'expires_at': peer['expires_at'], 'version': peer['version'],
            'receive_fax_images': accepts_fax_images(peer),
            'partner_receives_fax_images': _flag(peer.get('partner_receives_fax_images')),
            'fax_images_text': fax_images_text(peer),
            'notice_fax': _flag(peer.get('notice_fax')),
            'notice_fax_text': ('Each document goes directly, with a one-page notice by fax for their fax intake.'
                                if _flag(peer.get('notice_fax')) and peer['state'] != 'revoked' else None),
            'certificate_changed': peer.get('certificate_changed_at') is not None,
            'certificate_text': CERTIFICATE_CHANGED if peer.get('certificate_changed_at') is not None else None}


# Operator routes ----------------------------------------------------------------
@router.get('/card', dependencies=[Depends(require_permission('settings:read'))])
async def own_card(request: Request):
    service = service_for(request.app)
    return {'card': await _call(service.own_card)}


@router.get('/peers', dependencies=[Depends(require_permission('settings:read'))])
async def list_peers(request: Request):
    service = service_for(request.app)
    peers = await _call(service.store.list_peers)
    return {'peers': [_peer_view(peer) for peer in peers]}


class EnrollIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    card: dict | str


@router.post('/peers', dependencies=[Depends(require_permission('settings:write'))], status_code=201)
async def enroll(payload: EnrollIn, request: Request):
    service = service_for(request.app)
    peer = await _call(lambda: service.enroll(payload.card))
    return _peer_view(peer)


@router.post('/peers/{peer_id}/challenge')
async def send_challenge(peer_id: str, request: Request, identity=Depends(require_permission('settings:write'))):
    """Send the partner's number a one-page fax with a code they confirm from their Faxbot."""
    from ..access.http import runtime as access_runtime
    service = service_for(request.app)
    _, runtime = installation_engine(request.app)
    snapshot = request.scope['faxbot.configuration']
    revision = snapshot.active
    values = revision.values
    peer = await _call(lambda: service.store.get_peer(peer_id))
    if peer is None or peer['state'] == 'revoked':
        raise HTTPException(409, detail='This partner is not enrolled.')
    if revision.profile_id('outbound') is None:
        raise HTTPException(409, detail='Outbound fax delivery is disabled in this configuration.')
    code = service.new_code()
    organization = values.direct_organization.strip() or 'A Faxbot installation'
    document = service.challenge_document(peer, code, organization)
    access = access_runtime(request)

    def accept():
        job_id = accept_generated_fax(runtime, access, identity.actor, revision, to_number=peer['phone_number'],
                                      document=document, file_name='direct-delivery-code.pdf', pages=1)
        return job_id, service.store.start_challenge(peer_id, code=code, job_id=job_id)
    job_id, updated = await _call(accept)
    return {**_peer_view(updated), 'fax_id': job_id}


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(min_length=8, max_length=20)


@router.post('/peers/{peer_id}/confirm', dependencies=[Depends(require_permission('settings:write'))])
async def confirm_partner_code(peer_id: str, payload: ConfirmIn, request: Request):
    """Enter the code from a partner's challenge fax; Faxbot proves it to the partner with our key."""
    service = service_for(request.app)
    try:
        detail = await service.send_confirmation(peer_id, payload.code)
    except DirectConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    return {'confirmed': True, 'detail': detail}


class FaxImagesIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    accept: bool


@router.post('/peers/{peer_id}/fax-images', dependencies=[Depends(require_permission('settings:write'))])
async def set_fax_images(peer_id: str, payload: FaxImagesIn, request: Request):
    """Accept fax images from this partner, or stop; the partner is told with a signed statement."""
    service = service_for(request.app)
    try:
        peer, told = await service.set_fax_images(peer_id, payload.accept)
    except DirectConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except IdentityUnavailable:
        raise HTTPException(503, detail='Direct delivery keys are unavailable on this installation.') from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct delivery storage is unavailable.') from None
    name = peer['organization']
    if told == 'told':
        detail = (f'{name} now sends you faxes as the exact fax image.' if payload.accept
                  else f'{name} now sends you the original documents.')
    elif told == 'unsupported':
        detail = f"Saved. {name}'s Faxbot cannot send fax images yet, so their documents keep arriving as originals."
    else:
        detail = (f'Saved. Faxbot could not reach {name} just now; it tells them as soon as it can, and with its '
                  'answer to their next delivery.')
    return {**_peer_view(peer), 'detail': detail, 'partner_told': told == 'told'}


@router.post('/peers/{peer_id}/revoke', dependencies=[Depends(require_permission('settings:write'))])
async def revoke_peer(peer_id: str, request: Request):
    service = service_for(request.app)
    return _peer_view(await _call(lambda: service.store.revoke(peer_id)))


@router.get('/deliveries', dependencies=[Depends(require_permission('settings:read'))])
async def list_deliveries(request: Request):
    service = service_for(request.app)
    rows = await _call(service.store.recent)

    def noticed():
        # Originals that went with a one-page notice fax (notice.py): they are still never called faxed.
        from .notice import NoticeStore
        try:
            return {row['message_id'] for row in NoticeStore(service.store.engine).recent(limit=500)
                    if row['state'] != 'cancelled'}
        except Exception:
            return set()
    notices = await run_lifecycle_step(noticed)

    def sent_once():
        # A fax that went to a partner's intake, or as a reference or changes (distribute.py, reuse.py).
        from .distribute import sent_texts
        try:
            return sent_texts(service, rows)
        except Exception:
            return {}
    texts = await run_lifecycle_step(sent_once)
    return {'deliveries': [{'message_id': row['message_id'], 'direction': row['direction'],
                            'partner': row['organization'], 'fax_number': row['recipient_number'],
                            'kind': row.get('kind') or 'original', 'job_id': row.get('job_id'),
                            'notice': row['message_id'] in notices,
                            'send_once': texts.get(row['message_id']),
                            'state': row['state'], 'status': delivery_text(row, notice=row['message_id'] in notices),
                            'size_bytes': row['size_bytes'],
                            'created_at': row['created_at'], 'accepted_at': row['accepted_at']} for row in rows]}


DELIVERY_TEXT = {'sending': 'Sending.', 'accepted': 'Accepted by the recipient.', 'refused': 'Not delivered directly.',
                 'uncertain': 'Waiting for the partner to confirm.'}


def delivery_text(row, *, notice=False):
    """One sentence for a direct delivery record. A fax image is never called "faxed": no telephone call was made."""
    if row.get('kind') == 'relay' and row['state'] == 'accepted':
        # Accepted for relaying as a local call (relay.py), which is not yet delivered.
        return 'Accepted for relaying as a local call.'
    if row.get('kind') == 'repair' and row['state'] == 'accepted':
        name = row.get('organization') or 'the partner'
        return f'The pages missing after a broken call went directly to {name}, who now holds the whole fax.'
    if notice and row['state'] == 'accepted' and row.get('kind') != 'fax_image':
        name = row.get('organization') or 'the partner'
        verb = 'to' if row['direction'] == 'outbound' else 'by'
        return f'Delivered directly {verb} {name}; only a one-page notice went by fax.'
    if row.get('kind') != 'fax_image' or row['state'] != 'accepted':
        return DELIVERY_TEXT[row['state']]
    name = row.get('organization')
    if not name:
        return 'Delivered directly as a fax image; no telephone call.'
    if row['direction'] == 'outbound':
        return f'Delivered directly as a fax image to {name}; no telephone call.'
    return f'Delivered directly as a fax image by {name}; no telephone call.'


# Partner protocol (signature-authenticated) ------------------------------------
def _partner_call(operation):
    async def run():
        try:
            status, body = await run_lifecycle_step(operation)
        except DirectUnavailable:
            raise HTTPException(404, detail='Not found.') from None
        return JSONResponse(body, status_code=status)
    return run()


@router.post('/deliveries')
async def receive_delivery(request: Request):
    """Multipart fields ``manifest`` and ``signature`` and file ``document``.

    The body is bounded before it is parsed, because the caller is not yet
    authenticated: the signature is checked against the enrolled partner after.
    """
    service = service_for(request.app)
    values = await run_lifecycle_step(service.values)
    if not values.direct_delivery_enabled:
        raise HTTPException(404, detail='Not found.')
    length = request.headers.get('content-length', '')
    if not length.isdigit():
        raise HTTPException(411, detail='Send the document with a Content-Length header.')
    if int(length) > MAX_DOCUMENT_BYTES + 65536:
        raise HTTPException(413, detail='This document is too large.')
    try:
        form = await request.form(max_files=1, max_fields=2, max_part_size=16384)
    except Exception:
        raise HTTPException(400, detail='The message is not in the direct delivery format.') from None
    manifest, signature, document = form.get('manifest'), form.get('signature'), form.get('document')
    if not isinstance(manifest, str) or not isinstance(signature, str) or document is None or isinstance(document, str):
        raise HTTPException(400, detail='The message is not in the direct delivery format.')
    try:
        manifest_bytes = manifest.encode('ascii')
    except UnicodeEncodeError:
        manifest_bytes = b''
    ciphertext = await document.read(MAX_DOCUMENT_BYTES + 1)
    return await _partner_call(lambda: service.receive(manifest_bytes, signature, ciphertext))


@router.get('/deliveries/{message_id}')
async def delivery_status(message_id: str, request: Request,
                          x_faxbot_direct_key: str | None = Header(default=None),
                          x_faxbot_direct_time: str | None = Header(default=None),
                          x_faxbot_direct_signature: str | None = Header(default=None)):
    service = service_for(request.app)
    return await _partner_call(lambda: service.status(message_id, signer=x_faxbot_direct_key,
                                                      request_time=x_faxbot_direct_time,
                                                      signature=x_faxbot_direct_signature))


class VerificationIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    statement: str = Field(max_length=2048)
    signature: str = Field(max_length=128)


@router.post('/verifications')
async def receive_verification(payload: VerificationIn, request: Request):
    service = service_for(request.app)
    return await _partner_call(lambda: service.confirm(payload.statement, payload.signature))


@router.post('/capabilities')
async def receive_capabilities(payload: VerificationIn, request: Request):
    """A partner's signed statement of what it accepts from this installation now (fax images)."""
    service = service_for(request.app)
    return await _partner_call(lambda: service.note(payload.statement, payload.signature))
