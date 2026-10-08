"""Expected faxes over HTTP: the expectations, their proposals, imports, outage mode and evidence.

Every route authenticates here and checks ``work:*`` for each mailbox inside the
service; imports and outage mode check ``work:import`` (or ``settings:write``
for outages) at the installation. The background task examines each received
fax once and looks back for each new expectation; overdue escalation runs with
the work queue's own escalation (``WorkStore.escalate``). Nothing here sends,
resends or submits anything.
"""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from ..access.http import require_identity
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat
from ..routing.database import DeliveryStoreError
from .expectation_imports import MAX_FILE_BYTES
from .expectation_service import ExpectationService, ExpectedError
from .expectations import ExpectationStore, ExpectationWorker


UNAVAILABLE = 'Expected faxes are temporarily unavailable.'


def _background(app):
    engine, _ = installation_engine(app)
    if engine is None:
        return []
    try:
        store = ExpectationStore(engine)
    except DeliveryStoreError:
        return []
    worker = ExpectationWorker(store)
    return [('faxbot-expected-faxes', repeat(worker.step, interval=10.0, initial_delay=4.0,
                                             warning='Expected faxes could not be matched yet.'))]


router = APIRouter(prefix='/expected-faxes', tags=['Expected faxes'], lifespan=lifespan_tasks(_background))


def expectation_service(request):
    engine, runtime = installation_engine(request.app)
    if engine is None or getattr(request.app.state, 'access_runtime', None) is None:
        raise HTTPException(503, detail='Expected faxes are not ready.')
    store = getattr(request.app.state, 'expectation_store', None)
    if store is None or store.engine is not engine:
        try:
            store = ExpectationStore(engine)
        except DeliveryStoreError:
            raise HTTPException(503, detail=UNAVAILABLE) from None
        request.app.state.expectation_store = store
    snapshot = request.scope.get('faxbot.configuration')
    values = ((lambda: snapshot.active.values) if snapshot is not None
              else (lambda: runtime.manager.store.read().active.values))
    return ExpectationService(store, request.app.state.access_runtime, values=values)


async def call(operation):
    try:
        return await run_lifecycle_step(operation)
    except ExpectedError as error:
        raise HTTPException(error.status, detail=error.message) from None
    except (DeliveryStoreError, sa.exc.SQLAlchemyError):
        raise HTTPException(503, detail=UNAVAILABLE) from None


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ExpectIn(Strict):
    reference: str = Field(max_length=200)
    kind: str = Field(max_length=100)
    mailbox_id: str = Field(max_length=40)
    description: str | None = Field(default=None, max_length=500)
    counterparty: str | None = Field(default=None, max_length=200)
    fax_numbers: list[str] = Field(default_factory=list, max_length=20)
    direct_address: str | None = Field(default=None, max_length=320)
    partner_id: str | None = Field(default=None, max_length=40)
    required_parts: list[str] = Field(default_factory=list, max_length=10)
    required_revision: str | None = Field(default=None, max_length=40)
    due_at: datetime | None = None
    due_hours: int | None = None
    subaddress: str | None = Field(default=None, max_length=40)
    email_subject: str | None = Field(default=None, max_length=200)
    message_id: str | None = Field(default=None, max_length=512)
    form_field: str | None = Field(default=None, max_length=100)
    revision_field: str | None = Field(default=None, max_length=100)
    owner_principal_id: str | None = Field(default=None, max_length=40)


class VersionIn(Strict):
    version: int = Field(ge=1)


class DecideIn(Strict):
    proposal_id: str = Field(min_length=1, max_length=40)
    version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=400)


class MatchIn(Strict):
    inbound_fax_id: str = Field(min_length=1, max_length=40)
    version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=400)


class CloseIn(Strict):
    outcome: Literal['cancelled', 'completed_elsewhere']
    note: str = Field(max_length=400)
    version: int = Field(ge=1)


class ConflictIn(Strict):
    choice: Literal['keep', 'apply']
    version: int = Field(ge=1)


class SourceIn(Strict):
    id: str | None = Field(default=None, max_length=40)
    name: str = Field(max_length=100)
    format: Literal['csv', 'json']
    mapping: dict[str, str]
    mailbox_id: str | None = Field(default=None, max_length=40)
    due_hours: int | None = None
    subject_template: str | None = Field(default=None, max_length=200)
    subaddress_template: str | None = Field(default=None, max_length=40)
    form_field: str | None = Field(default=None, max_length=100)
    revision_field: str | None = Field(default=None, max_length=100)
    version: int | None = Field(default=None, ge=1)


class OutageIn(Strict):
    source: str = Field(min_length=1, max_length=100)
    started_at: datetime | None = None
    note: str | None = Field(default=None, max_length=300)


class OutageEndIn(Strict):
    version: int = Field(ge=1)
    ended_at: datetime | None = None


class ActionIn(Strict):
    operation_id: str = Field(max_length=100)
    revision: str | None = Field(default=None, max_length=40)
    reference: str | None = Field(default=None, max_length=200)
    action: str = Field(max_length=300)
    channel: Literal['fax', 'email', 'phone', 'other']
    outcome: Literal['done', 'uncertain'] = 'done'
    fax_job_id: str | None = Field(default=None, max_length=40)
    evidence_note: str | None = Field(default=None, max_length=300)
    occurred_at: datetime | None = None


@router.get('', summary='Expected faxes you can see, with their state and due time')
async def list_expected(request: Request,
                        view: Literal['waiting', 'overdue', 'proposed', 'missing', 'conflicts', 'closed',
                                      'all'] = 'waiting',
                        mailbox: str | None = Query(default=None, max_length=100),
                        search: str | None = Query(default=None, max_length=200),
                        limit: int = Query(default=100, ge=1, le=500), identity=Depends(require_identity)):
    service = expectation_service(request)
    return {'expected': await call(lambda: service.list(identity.actor, view=view, mailbox=mailbox, search=search,
                                                        limit=limit))}


@router.get('/counts', summary='How many expected faxes you can see in each state')
async def expected_counts(request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.counts(identity.actor))


@router.get('/report', summary='What is still missing and which received faxes answered nothing')
async def expected_report(request: Request, days: int = Query(default=30, ge=1, le=365),
                          identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.report(identity.actor, days=days))


@router.get('/mailboxes', summary='Mailboxes where you may add expected faxes')
async def expected_mailboxes(request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return {'mailboxes': await call(lambda: service.mailboxes(identity.actor))}


@router.post('', summary='Expect a fax: record what should arrive, from whom and by when', status_code=201)
async def add_expected(body: ExpectIn, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.add(identity.actor, body.model_dump()))


@router.get('/sources', summary='Saved import sources and their column mappings')
async def list_sources(request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return {'sources': await call(lambda: service.sources(identity.actor))}


@router.post('/sources', summary='Save an import source and its column mapping')
async def save_source(body: SourceIn, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.save_source(identity.actor, body.model_dump()))


@router.get('/imports', summary='Recent imports of expected faxes')
async def list_imports(request: Request, limit: int = Query(default=20, ge=1, le=100),
                       identity=Depends(require_identity)):
    service = expectation_service(request)
    return {'imports': await call(lambda: service.imports(identity.actor, limit=limit))}


@router.post('/imports', summary='Import an open-work export (CSV or JSON) through a saved source')
async def create_import(request: Request, source: str = Form(min_length=1, max_length=100),
                        full_export: bool = Form(default=False), file: UploadFile | None = File(default=None),
                        identity=Depends(require_identity)):
    if file is None:
        raise HTTPException(400, detail='Attach the export file.')
    data = await file.read(MAX_FILE_BYTES + 1)
    service = expectation_service(request)
    return await call(lambda: service.import_file(identity.actor, source, data, file_name=file.filename,
                                                  full=full_export))


@router.get('/outages', summary='Declared outages of the systems expected faxes come from')
async def list_outages(request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return {'outages': await call(lambda: service.outages(identity.actor))}


@router.post('/outages', summary='Mark a source system down', status_code=201)
async def start_outage(body: OutageIn, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.start_outage(identity.actor, body.source, started_at=body.started_at,
                                                   note=body.note))


@router.get('/outages/{outage_id}', summary='One outage: what was done during it and the reconciliation lists')
async def get_outage(outage_id: str, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.outage(identity.actor, outage_id))


@router.post('/outages/{outage_id}/end', summary='Mark a source system back')
async def end_outage(outage_id: str, body: OutageEndIn, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.end_outage(identity.actor, outage_id, version=body.version,
                                                 ended_at=body.ended_at))


@router.post('/outages/{outage_id}/actions', summary='Record what was done by fax, email or phone during an outage')
async def record_action(outage_id: str, body: ActionIn, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.record_action(identity.actor, outage_id, body.model_dump()))


@router.post('/outages/{outage_id}/reconcile', summary='Sort the export imported after an outage into three lists')
async def reconcile_outage(outage_id: str, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.reconcile(identity.actor, outage_id))


@router.get('/{expected_id}', summary='One expected fax')
async def get_expected(expected_id: str, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.detail(identity.actor, expected_id))


@router.get('/{expected_id}/history', summary="An expected fax's history, oldest first")
async def expected_history(expected_id: str, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return {'events': await call(lambda: service.history(identity.actor, expected_id))}


@router.post('/{expected_id}/confirm', summary='Confirm that a proposed received fax answers it')
async def confirm_expected(expected_id: str, body: DecideIn, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.confirm(identity.actor, expected_id, body.proposal_id, version=body.version))


@router.post('/{expected_id}/reject', summary='Say a proposed received fax does not answer it')
async def reject_expected(expected_id: str, body: DecideIn, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.reject(identity.actor, expected_id, body.proposal_id, version=body.version,
                                             note=body.note))


@router.post('/{expected_id}/match', summary='Link a received fax to it by hand')
async def match_expected(expected_id: str, body: MatchIn, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.match(identity.actor, expected_id, body.inbound_fax_id, version=body.version,
                                            note=body.note))


@router.post('/{expected_id}/close', summary='Cancel it, or record that it was completed another way')
async def close_expected(expected_id: str, body: CloseIn, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.close(identity.actor, expected_id, outcome=body.outcome, note=body.note,
                                            version=body.version))


@router.post('/{expected_id}/conflict', summary="Keep the first version of an imported row, or use the import's")
async def resolve_conflict(expected_id: str, body: ConflictIn, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    return await call(lambda: service.resolve_conflict(identity.actor, expected_id, choice=body.choice,
                                                       version=body.version))


@router.get('/{expected_id}/export', summary='Download evidence for an expected fax as a zip',
            response_class=Response, responses={200: {'content': {'application/zip': {}}}})
async def export_expected(expected_id: str, request: Request, identity=Depends(require_identity)):
    service = expectation_service(request)
    data, name = await call(lambda: service.export(identity.actor, expected_id))
    return Response(data, media_type='application/zip', headers={
        'Content-Disposition': f'attachment; filename="{name}"', 'Cache-Control': 'no-store'})
