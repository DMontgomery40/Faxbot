"""Send once to a partner's intake, and reuse what a partner holds: the signed partner protocol and the operator's
agreements (``distribute.py``, ``reuse.py``).

Partner routes carry no API key; each is authenticated by an Ed25519 signature from an enrolled partner and
answers 404 while direct delivery is switched off. Operator routes use settings:read and settings:write.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat_async
from ..routing.database import DeliveryStoreError, utcnow
from .distribute import DistributionConflict, SendOnce
from .http import service_for
from .identity import IdentityUnavailable
from .service import MAX_DOCUMENT_BYTES, DirectUnavailable
from .store import DirectConflict


def _background(app):
    engine, _ = installation_engine(app)
    if engine is None:
        return []
    service = service_for(app)

    async def tell():
        return await SendOnce(service).tell_all()
    return [('faxbot-direct-send-once-tell', repeat_async(tell, interval=600.0, initial_delay=55.0,
                                                          warning='Partners are told about send-once agreements '
                                                                  'later.'))]


router = APIRouter(prefix='/direct', tags=['Direct delivery'], lifespan=lifespan_tasks(_background))


class SignedIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    statement: str = Field(max_length=8192)
    signature: str = Field(max_length=128)


class ReferenceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    manifest: str = Field(max_length=16384)
    signature: str = Field(max_length=128)


def _partner_call(operation):
    async def run():
        try:
            status, body = await run_lifecycle_step(operation)
        except DirectUnavailable:
            raise HTTPException(404, detail='Not found.') from None
        return JSONResponse(body, status_code=status)
    return run()


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except (DistributionConflict, DirectConflict) as error:
        raise HTTPException(409, detail=str(error)) from None
    except IdentityUnavailable:
        raise HTTPException(503, detail='Direct delivery keys are unavailable on this installation.') from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct delivery storage is unavailable.') from None


async def _enabled(service):
    values = await run_lifecycle_step(service.values)
    if not values.direct_delivery_enabled:
        raise HTTPException(404, detail='Not found.')


async def _form(request, *, fields, files=1):
    """A bounded multipart body: checked for size before it is parsed, because the caller is not yet known."""
    length = request.headers.get('content-length', '')
    if not length.isdigit():
        raise HTTPException(411, detail='Send the document with a Content-Length header.')
    if int(length) > MAX_DOCUMENT_BYTES + 65536:
        raise HTTPException(413, detail='This document is too large.')
    try:
        form = await request.form(max_files=files, max_fields=fields, max_part_size=16384)
    except Exception:
        raise HTTPException(400, detail='The message is not in the direct delivery format.') from None
    return form


def _ascii(value):
    try:
        return value.encode('ascii')
    except UnicodeEncodeError:
        return b''


# Partner protocol (signature-authenticated) ----------------------------------------------------

@router.post('/distribution/statements')
async def receive_distribution_statement(payload: SignedIn, request: Request):
    """A partner's signed "send once" offer, acceptance or withdrawal."""
    service = service_for(request.app)
    return await _partner_call(lambda: SendOnce(service).hear(payload.statement, payload.signature))


@router.post('/distributions')
async def receive_distribution(request: Request):
    """A send's first document with the signed list of every recipient: multipart ``manifest``, ``signature``,
    ``routing``, ``routing_signature`` and file ``document``."""
    service = service_for(request.app)
    await _enabled(service)
    form = await _form(request, fields=4)
    manifest, signature, document = form.get('manifest'), form.get('signature'), form.get('document')
    routing, routing_signature = form.get('routing'), form.get('routing_signature')
    if (not all(isinstance(value, str) for value in (manifest, signature, routing, routing_signature))
            or document is None or isinstance(document, str)):
        raise HTTPException(400, detail='The message is not in the direct delivery format.')
    ciphertext = await document.read(MAX_DOCUMENT_BYTES + 1)
    return await _partner_call(lambda: service.receive(_ascii(manifest), signature, ciphertext,
                                                       routing=(routing, routing_signature)))


@router.post('/holdings')
async def receive_holdings_question(payload: SignedIn, request: Request):
    """A partner asks, signed, which documents it delivered here are still held."""
    from .reuse import answer_holdings
    service = service_for(request.app)
    return await _partner_call(lambda: answer_holdings(service, payload.statement, payload.signature))


@router.post('/references')
async def receive_reference(payload: ReferenceIn, request: Request):
    """A document's signed manifest alone: file the copy of it this partner delivered before."""
    from .reuse import Reference
    service = service_for(request.app)
    return await _partner_call(lambda: service.receive(_ascii(payload.manifest), payload.signature, b'',
                                                       carriage=Reference()))


@router.post('/patches')
async def receive_patch(request: Request):
    """A document as the changes to an earlier version this partner delivered: multipart ``manifest``,
    ``signature``, ``patch``, ``patch_signature`` and file ``delta``."""
    from .reuse import Patch
    service = service_for(request.app)
    await _enabled(service)
    form = await _form(request, fields=4)
    manifest, signature, delta = form.get('manifest'), form.get('signature'), form.get('delta')
    patch, patch_signature = form.get('patch'), form.get('patch_signature')
    if (not all(isinstance(value, str) for value in (manifest, signature, patch, patch_signature))
            or delta is None or isinstance(delta, str)):
        raise HTTPException(400, detail='The message is not in the direct delivery format.')
    ciphertext = await delta.read(MAX_DOCUMENT_BYTES + 1)
    if len(ciphertext) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, detail='This document is too large.')
    return await _partner_call(lambda: service.receive(_ascii(manifest), signature, b'',
                                                       carriage=Patch(patch, patch_signature, ciphertext)))


@router.post('/regions')
async def receive_regions(request: Request):
    """A fax image as its sender's new header regions around a page body this partner delivered before: multipart
    ``manifest``, ``signature``, ``regions``, ``regions_signature`` and file ``image``."""
    from .reuse import Regions
    service = service_for(request.app)
    await _enabled(service)
    form = await _form(request, fields=4)
    manifest, signature, image = form.get('manifest'), form.get('signature'), form.get('image')
    regions, regions_signature = form.get('regions'), form.get('regions_signature')
    if (not all(isinstance(value, str) for value in (manifest, signature, regions, regions_signature))
            or image is None or isinstance(image, str)):
        raise HTTPException(400, detail='The message is not in the direct delivery format.')
    ciphertext = await image.read(MAX_DOCUMENT_BYTES + 1)
    if len(ciphertext) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, detail='This document is too large.')
    return await _partner_call(lambda: service.receive(_ascii(manifest), signature, b'',
                                                       carriage=Regions(regions, regions_signature, ciphertext)))


# Operator routes --------------------------------------------------------------------------------

def _views(service, rows):
    send_once = SendOnce(service)
    names = {peer['id']: peer['organization'] for peer in service.store.list_peers()}
    return [send_once.view(row, names.get(row['peer_id'])) for row in rows]


@router.get('/send-once', dependencies=[Depends(require_permission('settings:read'))])
async def list_agreements(request: Request):
    """Every "send once" agreement, both ways: partners whose intake files your faxes, and yours for theirs."""
    service = service_for(request.app)

    def read():
        from .reuse import savings_view
        days = 30
        return {'agreements': _views(service, SendOnce(service).store.agreements_for()),
                'bytes': savings_view(service.store.engine, since=utcnow() - timedelta(days=days), days=days)}
    return await _call(read)


class GrantIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    numbers: list[str] = Field(min_length=1, max_length=100)
    intake: str = Field(min_length=1, max_length=200)


async def _actor_name(request, identity):
    from .relay_http import _actor_name as name
    return await name(request, identity)


@router.post('/peers/{peer_id}/send-once')
async def grant(peer_id: str, payload: GrantIn, request: Request,
                identity=Depends(require_permission('settings:write'))):
    """Let a partner send one copy to your intake, which files it for each of these numbers (signed)."""
    service = service_for(request.app)
    name = await _actor_name(request, identity)
    send_once = SendOnce(service)
    row = await _call(lambda: send_once.grant(peer_id, payload.numbers, payload.intake, actor_name=name))
    told = await send_once.tell(row)
    row = await _call(lambda: send_once.store.agreement(row['id']))
    (view,) = await _call(lambda: _views(service, [row]))
    detail = ('Offered. The partner accepts it in their Faxbot.' if told == 'told'
              else 'Offered. Faxbot tells the partner as soon as it can reach them.')
    return {**view, 'detail': detail}


@router.post('/send-once/{agreement_id}/accept')
async def accept(agreement_id: str, request: Request, identity=Depends(require_permission('settings:write'))):
    """Accept a partner's offer: your faxes to its numbers then go once to its intake."""
    service = service_for(request.app)
    name = await _actor_name(request, identity)
    send_once = SendOnce(service)
    row = await _call(lambda: send_once.accept(agreement_id, actor_name=name))
    told = await send_once.tell(row)
    row = await _call(lambda: send_once.store.agreement(agreement_id))
    (view,) = await _call(lambda: _views(service, [row]))
    detail = {'told': 'Accepted. Faxes to these numbers now go to the partner\'s intake.',
              'refused': 'The partner did not take the acceptance; their offer may have changed.',
              'unreachable': 'Accepted. It starts once Faxbot reaches the partner to tell them.'}[told]
    return {**view, 'detail': detail}


@router.post('/send-once/{agreement_id}/withdraw')
async def withdraw(agreement_id: str, request: Request, identity=Depends(require_permission('settings:write'))):
    """End an agreement at once; the partner is told, signed. Faxes already accepted stay accepted."""
    service = service_for(request.app)
    name = await _actor_name(request, identity)
    send_once = SendOnce(service)
    row = await _call(lambda: send_once.withdraw(agreement_id, actor_name=name))
    await send_once.tell(row)
    row = await _call(lambda: send_once.store.agreement(agreement_id))
    (view,) = await _call(lambda: _views(service, [row]))
    return {**view, 'detail': 'Ended. Faxes to these numbers go by your usual routes again.'}
