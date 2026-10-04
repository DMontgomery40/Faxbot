"""The work queue over HTTP: owned items for received documents, settings and evidence export.

Item routes authenticate here and check ``work:*`` on each document inside the
service; settings routes also declare ``settings:read``/``settings:write``.
"""
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from ..access.http import require_identity
from ..access.route_policy import authorize, require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat
from ..routing.database import DeliveryStoreError
from ..inbound.acquisition import AcquisitionError, InvalidDocument
from .export import EvidenceExport
from .imports import (ImportConflict, ImportInputError, ImportUnavailable, discard, parse_manifest,
                      spool_upload, store_document, validate_document)
from .service import WorkError, WorkService
from .store import WorkStore
from .worker import WorkWorker


UNAVAILABLE = 'The work queue is temporarily unavailable.'


def _active_values(runtime):
    return lambda: runtime.manager.store.read().active.values


def _background(app):
    engine, runtime = installation_engine(app)
    if engine is None:
        return []
    store = WorkStore(engine)

    def control():
        access = getattr(app.state, 'access_runtime', None)
        if access is None:
            raise RuntimeError('Access is not ready.')
        return access.control
    worker = WorkWorker(store, control=control, values=_active_values(runtime))
    return [('faxbot-work-queue', repeat(worker.step, interval=5.0, initial_delay=2.0,
                                         warning='The work queue could not be updated.'))]


router = APIRouter(prefix='/work', tags=['Work'], lifespan=lifespan_tasks(_background))


def work_store(request):
    engine, runtime = installation_engine(request.app)
    if engine is None or getattr(request.app.state, 'access_runtime', None) is None:
        raise HTTPException(503, detail='The work queue is not ready.')
    store = getattr(request.app.state, 'work_store', None)
    if store is None or store.engine is not engine:
        try:
            store = WorkStore(engine)
        except DeliveryStoreError:
            raise HTTPException(503, detail=UNAVAILABLE) from None
        request.app.state.work_store = store
    return store, runtime


def _values(request, runtime):
    snapshot = request.scope.get('faxbot.configuration')
    return (lambda: snapshot.active.values) if snapshot is not None else _active_values(runtime)


def work_service(request):
    store, runtime = work_store(request)
    return WorkService(store, request.app.state.access_runtime, values=_values(request, runtime))


async def call(operation):
    try:
        return await run_lifecycle_step(operation)
    except WorkError as error:
        raise HTTPException(error.status, detail=error.message) from None
    except (DeliveryStoreError, sa.exc.SQLAlchemyError):
        raise HTTPException(503, detail=UNAVAILABLE) from None


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AssignIn(Strict):
    principal_id: str = Field(min_length=1, max_length=40)
    version: int = Field(ge=1)


class VersionIn(Strict):
    version: int = Field(ge=1)


class DoneIn(Strict):
    note: str = Field(max_length=400)
    version: int = Field(ge=1)


class MailboxSettingIn(Strict):
    mailbox_id: str = Field(min_length=1, max_length=40)
    acknowledge_hours: int | None = Field(default=None)
    backup_principal_id: str | None = Field(default=None, max_length=40)
    version: int = Field(default=0, ge=0)


class SettingsIn(Strict):
    mailboxes: list[MailboxSettingIn] = Field(min_length=1, max_length=200)


@router.get('', summary='Work items you can see, with their owner, target and state')
async def list_work(request: Request, view: Literal['all', 'mine', 'unassigned', 'overdue'] = 'all',
                    state: Literal['open', 'acknowledged', 'done'] | None = None,
                    mailbox: str | None = Query(default=None, max_length=100),
                    limit: int = Query(default=100, ge=1, le=200),
                    inbound_fax_id: str | None = Query(default=None, min_length=1, max_length=64),
                    identity=Depends(require_identity)):
    service = work_service(request)
    return {'items': await call(lambda: service.list(identity.actor, view=view, state=state, mailbox=mailbox,
                                                     limit=limit, inbound_fax_id=inbound_fax_id))}


@router.get('/counts', summary='How many items you can see in each state')
async def work_counts(request: Request, identity=Depends(require_identity)):
    service = work_service(request)
    return await call(lambda: service.counts(identity.actor))


@router.get('/settings', summary='Acknowledgement targets and backup people',
            dependencies=[Depends(require_permission('settings:read'))])
async def get_settings(request: Request, identity=Depends(require_identity)):
    service = work_service(request)
    return await call(lambda: service.settings(identity.actor))


@router.put('/settings', summary='Change mailbox acknowledgement targets and backup people',
            dependencies=[Depends(require_permission('settings:write'))])
async def put_settings(body: SettingsIn, request: Request, identity=Depends(require_identity)):
    service = work_service(request)
    entries = [entry.model_dump() for entry in body.mailboxes]
    return await call(lambda: service.update_settings(identity.actor, entries))


@router.get('/{item_id}', summary='One work item')
async def get_work(item_id: str, request: Request, identity=Depends(require_identity)):
    service = work_service(request)
    return await call(lambda: service.detail(identity.actor, item_id))


@router.get('/{item_id}/history', summary="An item's history, oldest first")
async def work_history(item_id: str, request: Request, identity=Depends(require_identity)):
    service = work_service(request)
    return {'events': await call(lambda: service.history(identity.actor, item_id))}


@router.get('/{item_id}/assignees', summary='People who can see this document and so can own it')
async def work_assignees(item_id: str, request: Request, identity=Depends(require_identity)):
    service = work_service(request)
    return {'people': await call(lambda: service.assignees(identity.actor, item_id))}


@router.post('/{item_id}/assign', summary='Give an item to an owner')
async def assign_work(item_id: str, body: AssignIn, request: Request, identity=Depends(require_identity)):
    service = work_service(request)
    return await call(lambda: service.assign(identity.actor, item_id, body.principal_id, version=body.version))


@router.post('/{item_id}/acknowledge', summary='The owner acknowledges the item')
async def acknowledge_work(item_id: str, body: VersionIn, request: Request, identity=Depends(require_identity)):
    service = work_service(request)
    return await call(lambda: service.acknowledge(identity.actor, item_id, version=body.version))


@router.post('/{item_id}/done', summary='Mark an item done, with a short note')
async def complete_work(item_id: str, body: DoneIn, request: Request, identity=Depends(require_identity)):
    service = work_service(request)
    return await call(lambda: service.done(identity.actor, item_id, note=body.note, version=body.version))


@router.post('/{item_id}/reopen', summary='Reopen a done item')
async def reopen_work(item_id: str, body: VersionIn, request: Request, identity=Depends(require_identity)):
    service = work_service(request)
    return await call(lambda: service.reopen(identity.actor, item_id, version=body.version))


@router.get('/{item_id}/export', summary='Download evidence for an item as a zip',
            response_class=Response, responses={200: {'content': {'application/zip': {}}}})
async def export_work(item_id: str, request: Request, identity=Depends(require_identity)):
    service = work_service(request)
    exporter = EvidenceExport(service, values=service.values)
    data, name = await call(lambda: exporter.create(identity.actor, item_id))
    return Response(data, media_type='application/zip', headers={
        'Content-Disposition': f'attachment; filename="{name}"', 'Cache-Control': 'no-store'})


# -- generic import ---------------------------------------------------------------------

imports_router = APIRouter(prefix='/imports', tags=['Work'])


def record_import(request, actor, manifest, path, digest, size):
    """Acquire one validated document through the inbound acquisition store.

    Returns (status, import id, inbound fax id): ``received`` for a new document,
    ``duplicate`` when this identity already holds these bytes. Different bytes
    under the same identity raise ImportConflict; the first document is kept.
    """
    from ..inbound.acquisition import ImportStore, account_identity, discard as discard_artifact, store_document
    values = request.scope['faxbot.configuration'].active.values
    store = ImportStore(request.app.state.access_runtime.inbound)
    begun = store.begin(source='import', account=account_identity('import', actor.principal_id),
                        operation_id=manifest.operation_id, revision=manifest.revision, backend='import',
                        to_number=manifest.to_number, from_number=manifest.from_number,
                        reported_pages=manifest.pages, report=manifest.report(),
                        source_received_at=manifest.source_received_at, artifact_digest=digest, schedule=False,
                        country=values.fax_default_country)
    if begun.conflict:
        raise ImportConflict(CONFLICT)
    if begun.state in ('received', 'conflict'):
        return 'duplicate', begun.import_id, begun.inbound_fax_id
    with open(path, 'rb') as handle:
        data = handle.read()
    artifact = store_document(data, begun.inbound_fax_id, provider='The import')
    completion = store.complete(begun.import_id, artifact_path=artifact.path, digest=artifact.digest,
                                size=artifact.size, pages=artifact.pages, media_type=artifact.media_type)
    discard_artifact(artifact, completion)
    if completion.state == 'conflict':
        raise ImportConflict(CONFLICT)
    return ('received' if completion.stored else 'duplicate'), begun.import_id, begun.inbound_fax_id


CONFLICT = 'A different document was already imported with this operation id and revision; the first one is kept.'


@imports_router.post('', summary='Import a document from another system into the work queue',
                     dependencies=[Depends(require_permission('work:import'))])
async def create_import(request: Request, file: UploadFile | None = File(default=None),
                        manifest: str | None = Form(default=None), identity=Depends(require_identity)):
    values = request.scope['faxbot.configuration'].active.values
    try:
        parsed = parse_manifest(manifest, country=values.fax_default_country)
        path, digest, size = await spool_upload(file, max_bytes=values.max_file_size_mb * 1024 * 1024,
                                                directory=values.fax_data_dir)
    except ImportInputError as error:
        raise HTTPException(400, detail=str(error)) from None
    access = request.app.state.access_runtime

    def run():
        try:
            authorize(access, identity.actor, 'work:import')
            validate_document(path)
            return record_import(request, identity.actor, parsed, path, digest, size)
        finally:
            discard(path)
    try:
        status, import_id, inbound_id = await call(run)
    except ImportInputError as error:
        raise HTTPException(400, detail=str(error)) from None
    except ImportConflict as error:
        raise HTTPException(409, detail=str(error) or CONFLICT) from None
    except ImportUnavailable:
        raise HTTPException(503, detail='Imports are not available on this installation yet.') from None
    except InvalidDocument as error:
        raise HTTPException(400, detail=str(error)) from None
    except AcquisitionError as error:
        raise HTTPException(503, detail=str(error)) from None
    return {'import_id': import_id, 'inbound_id': inbound_id, 'status': status}
