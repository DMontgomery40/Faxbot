"""Direct delivery: partner enrollment for operators, and the signed partner protocol.

Operator routes use settings:read and settings:write (plus fax:send to send a
challenge fax). Partner routes carry no API key; each request is authenticated
by an Ed25519 signature from an enrolled partner and they answer 404 while
direct delivery is switched off.
"""
from datetime import datetime
import os
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat_async
from ..routing.database import DeliveryStoreError
from .crypto import DirectProtocolError
from .identity import IdentityUnavailable
from .service import DirectReconciler, DirectService, DirectUnavailable, MAX_DOCUMENT_BYTES
from .store import DirectConflict


def service_for(app):
    engine, runtime = installation_engine(app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    http = getattr(app.state, 'direct_http', None)  # Replaced only by tests.
    try:
        return DirectService(engine, values=lambda: runtime.manager.store.read().active.values,
                             environment=runtime.environment, http=http)
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct delivery storage is unavailable.') from None


def _background(app):
    engine, runtime = installation_engine(app)
    if engine is None:
        return []
    from ..outbound_store import OutboundStore
    service = service_for(app)
    reconciler = DirectReconciler(service, OutboundStore(runtime.manager.store))
    return [('faxbot-direct-reconcile', repeat_async(reconciler.step, interval=60.0, initial_delay=20.0,
                                                     warning='Direct delivery confirmations are temporarily unavailable.'))]


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


def _peer_view(peer, now=None):
    now = now or datetime.utcnow()
    open_code = peer['challenge_expires_at'] is not None and peer['challenge_expires_at'] > now
    status = STATE_TEXT[peer['state']]
    if peer['state'] == 'pending' and not open_code:
        status = 'Send a challenge fax so the partner can confirm this number.'
    return {'id': peer['id'], 'organization': peer['organization'], 'fax_number': peer['phone_number'],
            'endpoint': peer['endpoint_url'], 'state': peer['state'], 'status': status,
            'code_sent': open_code, 'code_expires_at': peer['challenge_expires_at'] if open_code else None,
            'verified_at': peer['verified_at'], 'expires_at': peer['expires_at'], 'version': peer['version']}


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
    job_id = uuid4().hex

    def accept():
        profile = runtime.manager.store.read_profile(revision.profile_id('outbound'))
        configuration = profile.configuration
        root = values.fax_data_dir
        pdf = os.path.join(root, job_id + '.pdf')
        with open(pdf, 'xb') as handle:
            handle.write(document)
        tiff = ''
        if ((configuration.manifest is None and configuration.provider_id in {'sip', 'freeswitch'})
                or configuration.traits.get('requires_tiff') is True):
            from ..conversion import pdf_to_tiff
            tiff = os.path.join(root, job_id + '.tiff')
            pdf_to_tiff(pdf, tiff)
        now = datetime.utcnow()
        access_runtime(request).outbound.accept(identity.actor, revision, {
            'id': job_id, 'to_number': peer['phone_number'], 'file_name': 'direct-delivery-code.pdf',
            'tiff_path': tiff, 'status': 'queued', 'pages': 1, 'created_at': now, 'updated_at': now})
        return service.store.start_challenge(peer_id, code=code, job_id=job_id)
    try:
        updated = await _call(accept)
    except HTTPException:
        raise
    except Exception:
        for suffix in ('.pdf', '.tiff'):
            try:
                os.unlink(os.path.join(values.fax_data_dir, job_id + suffix))
            except OSError:
                pass
        raise
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


@router.post('/peers/{peer_id}/revoke', dependencies=[Depends(require_permission('settings:write'))])
async def revoke_peer(peer_id: str, request: Request):
    service = service_for(request.app)
    return _peer_view(await _call(lambda: service.store.revoke(peer_id)))


@router.get('/deliveries', dependencies=[Depends(require_permission('settings:read'))])
async def list_deliveries(request: Request):
    service = service_for(request.app)
    rows = await _call(service.store.recent)
    text = {'sending': 'Sending.', 'accepted': 'Accepted by the recipient.', 'refused': 'Not delivered directly.',
            'uncertain': 'Waiting for the partner to confirm.'}
    return {'deliveries': [{'message_id': row['message_id'], 'direction': row['direction'],
                            'partner': row['organization'], 'fax_number': row['recipient_number'],
                            'state': row['state'], 'status': text[row['state']], 'size_bytes': row['size_bytes'],
                            'created_at': row['created_at'], 'accepted_at': row['accepted_at']} for row in rows]}


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
async def receive_delivery(request: Request, manifest: str = Form(..., max_length=16384),
                           signature: str = Form(..., max_length=128), document: UploadFile = File(...)):
    service = service_for(request.app)
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
