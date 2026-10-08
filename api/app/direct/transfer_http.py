"""Direct delivery in pieces, and repaired calls: the signed partner protocol and the operator's lists.

Partner routes carry no API key; each request is authenticated by an Ed25519
signature from an enrolled partner, and they answer 404 while direct delivery
is switched off (``transfer.py``, ``repair.py``). Operator routes use
settings:read.
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat_async
from ..routing.database import DeliveryStoreError
from .http import service_for
from .service import DirectUnavailable
from .transfer import MAX_PIECE_SIZE, MAX_PIECES, TransferReceiver, TransferStore, background_step, transfer_view


def _background(app):
    engine, _ = installation_engine(app)
    if engine is None:
        return []
    service = service_for(app)
    step = background_step(service)

    async def expire():
        return await run_lifecycle_step(step)
    return [('faxbot-direct-transfers', repeat_async(expire, interval=900.0, initial_delay=45.0,
                                                     warning='Unfinished direct transfers are cleaned up later.'))]


router = APIRouter(prefix='/direct', tags=['Direct delivery'], lifespan=lifespan_tasks(_background))


def _partner_call(operation):
    async def run():
        try:
            status, body = await run_lifecycle_step(operation)
        except DirectUnavailable:
            raise HTTPException(404, detail='Not found.') from None
        return JSONResponse(body, status_code=status)
    return run()


async def _enabled(request):
    service = service_for(request.app)
    values = await run_lifecycle_step(service.values)
    if not values.direct_delivery_enabled:
        raise HTTPException(404, detail='Not found.')
    return service


async def _bounded_body(request, limit):
    """The request body, read only up to ``limit`` bytes (the caller is not authenticated yet)."""
    length = request.headers.get('content-length', '')
    if not length.isdigit():
        raise HTTPException(411, detail='Send the piece with a Content-Length header.')
    if int(length) > limit:
        raise HTTPException(413, detail='This piece is too large.')
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, detail='This piece is too large.')
    return bytes(data)


# Partner protocol (signature-authenticated) ----------------------------------------------------

@router.post('/transfers')
async def open_transfer(request: Request):
    """Preflight: the signed manifest and the signed list of pieces, before any document byte."""
    service = await _enabled(request)
    import json
    # Every piece's SHA-256 (64 characters) plus the manifest: bounded before it is parsed.
    raw = await _bounded_body(request, 32768 + 70 * MAX_PIECES)
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, detail='The message is not in the direct delivery format.') from None
    return await _partner_call(lambda: TransferReceiver(service).open(body))


@router.put('/transfers/{message_id}/pieces/{sequence}')
async def put_piece(message_id: str, sequence: int, request: Request,
                    upload_offset: str | None = Header(default=None),
                    x_faxbot_piece_sha256: str | None = Header(default=None),
                    x_faxbot_direct_key: str | None = Header(default=None),
                    x_faxbot_direct_time: str | None = Header(default=None),
                    x_faxbot_direct_signature: str | None = Header(default=None)):
    """One piece at its offset, signed by the partner that opened the transfer."""
    service = await _enabled(request)
    data = await _bounded_body(request, MAX_PIECE_SIZE)
    if upload_offset is None or not upload_offset.isdigit():
        raise HTTPException(400, detail='Send the piece with its Upload-Offset.')
    return await _partner_call(lambda: TransferReceiver(service).piece(
        message_id, sequence, data, signer=x_faxbot_direct_key, request_time=x_faxbot_direct_time,
        signature=x_faxbot_direct_signature, offset=int(upload_offset), digest=x_faxbot_piece_sha256))


@router.get('/transfers/{message_id}')
async def transfer_state(message_id: str, request: Request,
                         x_faxbot_direct_key: str | None = Header(default=None),
                         x_faxbot_direct_time: str | None = Header(default=None),
                         x_faxbot_direct_signature: str | None = Header(default=None)):
    """What this installation holds of a transfer: the missing pieces by sequence and SHA-256. Never a fence."""
    service = service_for(request.app)
    return await _partner_call(lambda: TransferReceiver(service).state(
        message_id, signer=x_faxbot_direct_key, request_time=x_faxbot_direct_time,
        signature=x_faxbot_direct_signature))


class SignedIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    statement: str = Field(max_length=4096)
    signature: str = Field(max_length=128)


@router.post('/transfers/{message_id}/commit')
async def commit_transfer(message_id: str, payload: SignedIn, request: Request):
    """Assemble the document from its pieces and accept it once; the same receipt as a single delivery."""
    service = service_for(request.app)
    return await _partner_call(lambda: TransferReceiver(service).commit(message_id, payload.statement,
                                                                        payload.signature))


# Operator routes --------------------------------------------------------------------------------

@router.get('/transfers', dependencies=[Depends(require_permission('settings:read'))])
async def list_transfers(request: Request):
    """Documents sent to and received from partners in pieces, with how many pieces each side holds."""
    service = service_for(request.app)

    def read():
        peers = {peer['id']: peer['organization'] for peer in service.store.list_peers()}
        return [transfer_view(row, peers.get(row['peer_id'])) for row in TransferStore(service.store.engine).recent()]
    try:
        return {'transfers': await run_lifecycle_step(read)}
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct delivery storage is unavailable.') from None
