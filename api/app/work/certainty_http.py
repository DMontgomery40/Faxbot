"""Uncertain sent faxes over HTTP: the owned items, their checks, settling, and the two sends a person may ask for.

Item routes authenticate here and check ``fax:read`` / ``fax:reconcile`` on
each fax inside the service; settings routes also declare
``settings:read``/``settings:write``. The background tasks make items and
record what the automatic checks find; they never send anything.
"""
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from ..access.http import require_identity
from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat, repeat_async
from ..routing.database import DeliveryStoreError
from .certainty import CertaintyError, CertaintyStore, CertaintyWorker, PartnerQuestion
from .certainty_service import CertaintyService


UNAVAILABLE = 'Uncertain sent faxes are temporarily unavailable.'


def _background(app):
    engine, _ = installation_engine(app)
    if engine is None:
        return []
    try:
        store = CertaintyStore(engine)
    except DeliveryStoreError:
        return []

    def control():
        access = getattr(app.state, 'access_runtime', None)
        if access is None:
            raise RuntimeError('Access is not ready.')
        return access.control
    worker = CertaintyWorker(store, control=control)
    tasks = [('faxbot-certainty', repeat(worker.step, interval=15.0, initial_delay=5.0,
                                         warning='Uncertain sent faxes could not be updated.'))]

    async def ask_partners():
        from ..direct.http import service_for
        try:
            service = service_for(app)
        except Exception:
            return False
        if not getattr(await run_lifecycle_step(service.values), 'direct_delivery_enabled', False):
            return False
        return await PartnerQuestion(store, service).step()
    tasks.append(('faxbot-certainty-partners', repeat_async(
        ask_partners, interval=60.0, initial_delay=30.0, warning='A partner could not be asked about a fax yet.')))
    return tasks


router = APIRouter(prefix='/certainty', tags=['Uncertain sent faxes'], lifespan=lifespan_tasks(_background))


def certainty_service(request):
    engine, runtime = installation_engine(request.app)
    if engine is None or getattr(request.app.state, 'access_runtime', None) is None:
        raise HTTPException(503, detail='Uncertain sent faxes are not ready.')
    store = getattr(request.app.state, 'certainty_store', None)
    if store is None or store.engine is not engine:
        try:
            store = CertaintyStore(engine)
        except DeliveryStoreError:
            raise HTTPException(503, detail=UNAVAILABLE) from None
        request.app.state.certainty_store = store
    snapshot = request.scope.get('faxbot.configuration')
    values = ((lambda: snapshot.active.values) if snapshot is not None
              else (lambda: runtime.manager.store.read().active.values))
    return CertaintyService(store, request.app.state.access_runtime, values=values)


async def call(operation):
    try:
        return await run_lifecycle_step(operation)
    except CertaintyError as error:
        raise HTTPException(error.status, detail=error.message) from None
    except (DeliveryStoreError, sa.exc.SQLAlchemyError):
        raise HTTPException(503, detail=UNAVAILABLE) from None


def accept_once(request, actor):
    """Queue one Faxbot-made fax under a fixed fax ID for this person; an earlier request's fax is kept as is.

    The same as ``routing/submit.accept_generated_fax`` (the person must be allowed to send, the fax is bound to
    the active provider and appears in Sent), except that a fax ID already taken means an earlier click queued
    it: nothing is written and nothing is removed.
    """
    from ..access.http import runtime as access_runtime
    _, runtime = installation_engine(request.app)
    revision = request.scope['faxbot.configuration'].active
    access = access_runtime(request)

    def accept(job_id, to_number, document, file_name, pages):
        from datetime import datetime
        profile_id = revision.profile_id('outbound')
        if profile_id is None:
            raise RuntimeError('Outbound fax delivery is turned off, so nothing can be sent.')
        configuration = runtime.manager.store.read_profile(profile_id).configuration
        root = revision.values.fax_data_dir
        pdf, tiff = os.path.join(root, job_id + '.pdf'), ''
        try:
            handle = open(pdf, 'xb')
        except FileExistsError:
            return False  # an earlier request queued this fax
        try:
            with handle:
                handle.write(document)
            if ((configuration.manifest is None and configuration.provider_id in {'sip', 'freeswitch'})
                    or configuration.traits.get('requires_tiff') is True):
                from ..conversion import pdf_to_tiff
                tiff = os.path.join(root, job_id + '.tiff')
                pdf_to_tiff(pdf, tiff)
            now = datetime.utcnow()
            access.outbound.accept(actor, revision, {
                'id': job_id, 'to_number': to_number, 'file_name': file_name[:255], 'tiff_path': tiff,
                'status': 'queued', 'pages': pages, 'created_at': now, 'updated_at': now})
        except BaseException:
            for path in (pdf, tiff):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            raise
        return True
    return accept


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AssignIn(Strict):
    principal_id: str = Field(min_length=1, max_length=40)
    version: int = Field(ge=1)


class VersionIn(Strict):
    version: int = Field(ge=1)


class SettleIn(Strict):
    outcome: Literal['delivered', 'not_delivered', 'unknown']
    reason: str = Field(max_length=400)
    version: int = Field(ge=1)
    send_again: bool = False


class SettingsIn(Strict):
    fallback_principal_id: str | None = Field(default=None, max_length=40)
    settle_hours: int = Field(ge=0, le=720)
    version: int = Field(ge=0)


@router.get('/items', summary='Uncertain sent faxes you can see, with their owner and deadline')
async def list_items(request: Request, view: Literal['all', 'mine', 'unassigned', 'overdue'] = 'all',
                     state: Literal['open', 'settled', 'any'] = 'open',
                     fax_id: str | None = Query(default=None, min_length=1, max_length=40),
                     limit: int = Query(default=100, ge=1, le=200), identity=Depends(require_identity)):
    service = certainty_service(request)
    return {'items': await call(lambda: service.list(identity.actor, view=view, limit=limit, fax_id=fax_id,
                                                     state=None if state == 'any' else state))}


@router.get('/counts', summary='How many uncertain sent faxes are open, yours, unassigned and overdue')
async def counts(request: Request, identity=Depends(require_identity)):
    service = certainty_service(request)
    return await call(lambda: service.counts(identity.actor))


@router.get('/settings', summary='Who settles uncertain sent faxes when the sender cannot, and how soon',
            dependencies=[Depends(require_permission('settings:read'))])
async def get_settings(request: Request, identity=Depends(require_identity)):
    service = certainty_service(request)
    return await call(lambda: service.settings(identity.actor))


@router.put('/settings', summary='Change the fallback person and the hours to settle an uncertain fax',
            dependencies=[Depends(require_permission('settings:write'))])
async def put_settings(body: SettingsIn, request: Request, identity=Depends(require_identity)):
    service = certainty_service(request)
    return await call(lambda: service.update_settings(identity.actor, fallback_principal_id=body.fallback_principal_id,
                                                      settle_hours=body.settle_hours, version=body.version))


@router.get('/faxes/{fax_id}', summary="One sent fax's uncertain outcomes, with the checks")
async def for_fax(fax_id: str, request: Request, identity=Depends(require_identity)):
    service = certainty_service(request)
    return await call(lambda: service.for_fax(identity.actor, fax_id))


@router.get('/items/{item_id}', summary='One uncertain sent fax, with the checks ranked by cost')
async def get_item(item_id: str, request: Request, identity=Depends(require_identity)):
    service = certainty_service(request)
    return await call(lambda: service.detail(identity.actor, item_id))


@router.get('/items/{item_id}/history', summary="An item's history, oldest first")
async def history(item_id: str, request: Request, identity=Depends(require_identity)):
    service = certainty_service(request)
    return {'events': await call(lambda: service.history(identity.actor, item_id))}


@router.get('/items/{item_id}/assignees', summary='People who can see this fax and so can own it')
async def assignees(item_id: str, request: Request, identity=Depends(require_identity)):
    service = certainty_service(request)
    return {'people': await call(lambda: service.assignees(identity.actor, item_id))}


@router.post('/items/{item_id}/assign', summary='Give an uncertain sent fax to an owner')
async def assign(item_id: str, body: AssignIn, request: Request, identity=Depends(require_identity)):
    service = certainty_service(request)
    return await call(lambda: service.assign(identity.actor, item_id, body.principal_id, version=body.version))


@router.get('/items/{item_id}/receipt-query', summary='The one-page receipt query, to check before sending',
            response_class=Response, responses={200: {'content': {'application/pdf': {}}}})
async def draft_query(item_id: str, request: Request, identity=Depends(require_identity)):
    service = certainty_service(request)
    document, name, _ = await call(lambda: service.draft(identity.actor, item_id))
    return Response(document, media_type='application/pdf', headers={
        'Content-Disposition': f'inline; filename="{name}"', 'Cache-Control': 'no-store'})


@router.post('/items/{item_id}/receipt-query', summary='Send the one-page receipt query to the recipient')
async def send_query(item_id: str, body: VersionIn, request: Request, identity=Depends(require_identity)):
    service = certainty_service(request)
    accept = accept_once(request, identity.actor)
    try:
        return await call(lambda: service.send_query(identity.actor, item_id, version=body.version, accept=accept))
    except RuntimeError as error:  # outbound delivery refused the fax, in a plain sentence
        raise HTTPException(409, detail=str(error)) from None


@router.post('/items/{item_id}/settle', summary='Settle what happened: delivered, not delivered, or can\'t tell')
async def settle(item_id: str, body: SettleIn, request: Request, identity=Depends(require_identity)):
    service = certainty_service(request)
    accept = accept_once(request, identity.actor) if body.send_again else None
    try:
        return await call(lambda: service.settle(identity.actor, item_id, outcome=body.outcome, reason=body.reason,
                                                 version=body.version, send_again=body.send_again, accept=accept))
    except RuntimeError as error:
        raise HTTPException(409, detail=str(error)) from None
