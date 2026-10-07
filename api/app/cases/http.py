"""Case packets: send only what the recipient has not acknowledged, record its acknowledgements, repair.

Sending, adding originals and recording what a recipient said need fax:send
(like POST /fax); reading a case record needs settings:read; a recipient's
reuse period needs settings:write to change. Whether a recipient accepts
references is part of its destination profile (PATCH /routing/destinations/{number}).
"""
from datetime import datetime
import os
from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine
from ..routing.database import DeliveryStoreError
from ..routing.numbers import InvalidNumber, normalize_number
from ..routing.submit import accept_generated_fax
from .ledger import (
    LIMITS, MAX_NOTE, CaseConflict, CaseInputError, CaseLedger, PacketPlan, check_case_id, clean, compose, document,
)


cases_routes = APIRouter(prefix='/cases', tags=['Case ledger'])
recipients_router = APIRouter(prefix='/case-recipients', tags=['Case ledger'])
MAX_DOCUMENTS = 50
UNAVAILABLE = 'Case records are unavailable.'


def _values(request):
    return request.scope['faxbot.configuration'].active.values


def _ledger(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    try:
        return CaseLedger(engine, _values(request).fax_data_dir)
    except DeliveryStoreError:
        raise HTTPException(503, detail=UNAVAILABLE) from None


def _number(to, request):
    try:
        return normalize_number(to, country=_values(request).fax_default_country)
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


def _checked(case_id):
    try:
        return check_case_id(case_id)
    except CaseInputError as error:
        raise HTTPException(400, detail=str(error)) from None


def _inputs(case_id, to, request):
    return _checked(case_id), _number(to, request)


async def call(step):
    """Run a ledger step off the event loop with plain-sentence errors."""
    try:
        return await run_lifecycle_step(step)
    except CaseInputError as error:
        raise HTTPException(400, detail=str(error)) from None
    except CaseConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail=UNAVAILABLE) from None


def entry_view(view, kept=frozenset()):
    return {'id': view['id'], 'title': view['title'], 'pages': view['page_count'],
            'reference': view['digest'][:12], 'source': view['source'], 'version': view['version'],
            'purpose': view['purpose'], 'state': view['state'], 'accepted': view['state'] == 'accepted',
            'accepted_at': view['accepted_at'], 'accepted_how': view['accepted_how'],
            'accepted_by': view['accepted_by'], 'accepted_note': view['accepted_note'],
            'expires_at': view['expires_at'], 'invalidated_at': view['invalidated_at'],
            'invalidated_note': view['invalidated_note'], 'sent_at': view['sent_at'], 'fax_id': view['fax_id'],
            'kept': view['original_id'] is not None or view['digest'] in kept}


def original_view(row):
    return {'id': row['id'], 'title': row['title'], 'pages': row['page_count'], 'reference': row['digest'][:12],
            'document_type': row['document_type'], 'document_date': row['document_date'], 'source': row['source'],
            'version': row['version'], 'added_at': row['created_at'], 'added_by': row['principal_name']}


def packet_view(case_id, recipient, packet, *, purpose=''):
    included = [{'title': item.title, 'pages': item.pages, 'status': 'included', 'why': why,
                 'source': item.source, 'version': item.version}
                for item, why in zip(packet.included, packet.why or ('new',) * len(packet.included))]
    referenced = [{'title': entry['title'], 'pages': entry['pages'], 'status': 'referenced', 'why': 'accepted',
                   'source': entry['source'], 'version': entry['version'], 'accepted_at': entry['accepted_at']}
                  for entry in packet.referenced]
    return {'case_id': case_id, 'to': recipient, 'purpose': purpose, 'accepts_references': packet.references_allowed,
            'pages': packet.pages, 'pages_saved': packet.pages_saved, 'documents': included + referenced}


@cases_routes.get('', dependencies=[Depends(require_permission('settings:read'))])
async def recent_cases(request: Request, limit: int = Query(default=50, ge=1, le=200)):
    """The newest cases this installation sent packets for, with recipient and document counts."""
    ledger = _ledger(request)
    cases = await call(lambda: ledger.recent(limit))
    return {'cases': [{'case_id': case['case_id'], 'to': case['recipient'], 'documents': case['documents'],
                       'sent': case['sent'], 'accepted': case['accepted'],
                       'needs_attention': case['needs_attention'], 'pages': case['pages'],
                       'last_sent_at': case['last_sent_at'], 'accepts_references': case['accepts_references']}
                      for case in cases]}


@cases_routes.get('/{case_id}/documents', dependencies=[Depends(require_permission('settings:read'))])
async def case_documents(case_id: str, request: Request, to: str = Query(...)):
    """What this recipient was sent for the case, and what it acknowledged."""
    case_id, recipient = _inputs(case_id, to, request)
    ledger = _ledger(request)

    def read():
        return (ledger.entries(case_id, recipient), ledger.references_allowed(recipient),
                ledger.recipient(recipient), {row['digest'] for row in ledger.originals(case_id)},
                ledger.in_flight(case_id, recipient))
    entries, allowed, settings, kept, waiting = await call(read)
    return {'case_id': case_id, 'to': recipient, 'accepts_references': allowed,
            'reuse_days': settings['reuse_days'], 'reuse_days_default': settings['reuse_days_default'],
            'reuse_days_set': settings['reuse_days_set'], 'recipient_version': settings['version'],
            'packets_in_flight': len(waiting), 'documents': [entry_view(entry, kept) for entry in entries]}


def _texts(values, index, limit):
    return clean(values[index] if index < len(values) else '', limit)


def _date(value):
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        raise CaseInputError('Write each document date as year-month-day, for example 2026-09-30.') from None


def _read_documents(uploads, fields, data_dir, max_bytes, *, purpose=''):
    from ..conversion import DocumentConversionError, validate_pdf
    documents = []
    with tempfile.TemporaryDirectory(dir=data_dir) as stage:
        for index, (data, name) in enumerate(uploads):
            if not data.startswith(b'%PDF'):
                raise CaseInputError('Each case document must be a PDF.')
            if len(data) > max_bytes:
                raise CaseInputError('A case document is larger than the upload limit.')
            path = Path(stage) / f'{index}.pdf'
            path.write_bytes(data)
            try:
                pages = validate_pdf(str(path))
            except DocumentConversionError as error:
                raise CaseInputError(str(error)) from None
            title = _texts(fields['titles'], index, LIMITS['title']) or clean(Path(name or '').stem, LIMITS['title'])
            documents.append(document(
                title or f'Document {index + 1}', data, pages, source=_texts(fields['sources'], index, 200),
                version=_texts(fields['versions'], index, 200), document_type=_texts(fields['types'], index, 200),
                document_date=_date(fields['dates'][index] if index < len(fields['dates']) else ''),
                purpose=purpose))
    return documents


async def _uploads(request, documents):
    if not 0 < len(documents) <= MAX_DOCUMENTS:
        raise HTTPException(400, detail=f'Send between 1 and {MAX_DOCUMENTS} documents.')
    max_bytes = _values(request).max_file_size_mb * 1024 * 1024
    return [(await upload.read(max_bytes + 1), upload.filename) for upload in documents], max_bytes


def _staged(request, uploads, fields, max_bytes, purpose=''):
    values = _values(request)
    os.makedirs(values.fax_data_dir, exist_ok=True)
    return _read_documents(uploads, fields, values.fax_data_dir, max_bytes, purpose=purpose)


async def send_packet(request, identity, ledger, case_id, recipient, packet, *, kind, purpose='', reason=None,
                      checklist_id=None):
    """Fax the packet as a new fax and record what it carries; never retried here."""
    from ..access.http import runtime as access_runtime
    revision = request.scope['faxbot.configuration'].active
    values = revision.values
    organization = values.direct_organization.strip() or values.fax_header or 'Faxbot'
    access = access_runtime(request)
    _, runtime = installation_engine(request.app)
    principal_id = getattr(identity.actor, 'principal_id', None)

    def send():
        kept = ledger.keep(case_id, packet.included, principal_id=principal_id)
        planned = PacketPlan(tuple(kept), packet.referenced, packet.references_allowed, packet.why)
        pdf = compose(case_id, recipient, planned, organization, purpose)
        job_id = accept_generated_fax(runtime, access, identity.actor, revision, to_number=recipient,
                                      document=pdf, file_name=f'case-{case_id}.pdf', pages=planned.pages)
        ledger.record(case_id, recipient, planned, job_id, kind=kind, purpose=purpose, reason=reason,
                      checklist_id=checklist_id, principal_id=principal_id)
        return job_id
    try:
        return await call(send)
    except RuntimeError as error:  # outbound delivery refused the fax, in a plain sentence
        raise HTTPException(409, detail=str(error)) from None


@cases_routes.post('/{case_id}/faxes', status_code=202)
async def send_case_packet(case_id: str, request: Request, to: str = Form(...),
                           documents: list[UploadFile] = File(...), titles: list[str] = Form(default=[]),
                           sources: list[str] = Form(default=[]), versions: list[str] = Form(default=[]),
                           types: list[str] = Form(default=[]), dates: list[str] = Form(default=[]),
                           purpose: str = Form(default=''), preview: bool = Form(False),
                           identity=Depends(require_permission('fax:send', resource='personal'))):
    """Send a packet: documents the recipient acknowledged are listed on an index page when it accepts that."""
    case_id, recipient = _inputs(case_id, to, request)
    purpose = clean(purpose, LIMITS['purpose'])
    uploads, max_bytes = await _uploads(request, documents)
    ledger = _ledger(request)
    fields = {'titles': titles, 'sources': sources, 'versions': versions, 'types': types, 'dates': dates}
    packet = await call(lambda: ledger.plan(case_id, recipient, _staged(request, uploads, fields, max_bytes, purpose)))
    view = packet_view(case_id, recipient, packet, purpose=purpose)
    if not packet.included:
        raise HTTPException(409, detail='The recipient already acknowledged every document for this case; '
                                        'there is nothing new to send.')
    if preview:
        return {**view, 'fax_id': None}
    job_id = await send_packet(request, identity, ledger, case_id, recipient, packet, kind='update', purpose=purpose)
    return {**view, 'fax_id': job_id}


@cases_routes.get('/{case_id}/originals', dependencies=[Depends(require_permission('settings:read'))])
async def case_originals(case_id: str, request: Request):
    """The case's original documents, kept unchanged, with their type, date, source and version."""
    case_id = _checked(case_id)
    ledger = _ledger(request)
    rows = await call(lambda: ledger.originals(case_id))
    return {'case_id': case_id, 'originals': [original_view(row) for row in rows]}


@cases_routes.post('/{case_id}/originals', status_code=201)
async def add_case_originals(case_id: str, request: Request, documents: list[UploadFile] = File(...),
                             titles: list[str] = Form(default=[]), sources: list[str] = Form(default=[]),
                             versions: list[str] = Form(default=[]), types: list[str] = Form(default=[]),
                             dates: list[str] = Form(default=[]),
                             identity=Depends(require_permission('fax:send', resource='personal'))):
    """Keep documents in the case without sending them, for checklist packets and repairs."""
    case_id = _checked(case_id)
    uploads, max_bytes = await _uploads(request, documents)
    ledger = _ledger(request)
    fields = {'titles': titles, 'sources': sources, 'versions': versions, 'types': types, 'dates': dates}
    principal_id = getattr(identity.actor, 'principal_id', None)

    def add():
        ledger.keep(case_id, _staged(request, uploads, fields, max_bytes), principal_id=principal_id)
        return ledger.originals(case_id)
    rows = await call(add)
    return {'case_id': case_id, 'originals': [original_view(row) for row in rows]}


class Choice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    to: str = Field(min_length=1, max_length=40)
    documents: list[str] = Field(min_length=1, max_length=500)
    note: str | None = Field(default=None, max_length=MAX_NOTE)


class Confirmation(Choice):
    # The recipient's acknowledgement fax, when it arrived as a received fax.
    received_fax_id: str | None = Field(default=None, min_length=1, max_length=40)


@cases_routes.post('/{case_id}/accept')
async def accept_documents(case_id: str, payload: Confirmation, request: Request,
                           identity=Depends(require_permission('fax:send', resource='personal'))):
    """Record that the recipient confirmed it has these documents, with a note or its acknowledgement fax."""
    from ..access.http import runtime as access_runtime
    case_id, recipient = _inputs(case_id, payload.to, request)
    ledger = _ledger(request)
    if payload.received_fax_id is not None:
        # The person must be able to read the received fax they cite.
        await run_lifecycle_step(lambda: access_runtime(request).inbound_queries.item(
            identity.actor, payload.received_fax_id))
    views = await call(lambda: ledger.confirm(
        case_id, recipient, payload.documents, note=payload.note, received_fax_id=payload.received_fax_id,
        principal_id=getattr(identity.actor, 'principal_id', None)))
    return {'case_id': case_id, 'to': recipient, 'documents': [entry_view(view) for view in views]}


@cases_routes.post('/{case_id}/invalidate')
async def invalidate_documents(case_id: str, payload: Choice, request: Request,
                               identity=Depends(require_permission('fax:send', resource='personal'))):
    """The recipient could not find these documents: stop referring to them; the next packet sends them in full."""
    case_id, recipient = _inputs(case_id, payload.to, request)
    ledger = _ledger(request)
    views = await call(lambda: ledger.invalidate(case_id, recipient, payload.documents, note=payload.note,
                                                 principal_id=getattr(identity.actor, 'principal_id', None)))
    return {'case_id': case_id, 'to': recipient, 'documents': [entry_view(view) for view in views]}


class Repair(BaseModel):
    model_config = ConfigDict(extra='forbid')
    to: str = Field(min_length=1, max_length=40)
    reason: str = Field(default='', max_length=MAX_NOTE)
    preview: bool = False


@cases_routes.post('/{case_id}/repair', status_code=202)
async def repair_packet(case_id: str, payload: Repair, request: Request,
                        identity=Depends(require_permission('fax:send', resource='personal'))):
    """Send every document of the case to this recipient again, as a new fax, for the reason a person gives."""
    case_id, recipient = _inputs(case_id, payload.to, request)
    ledger = _ledger(request)
    reason = payload.reason.strip()
    if not payload.preview and len(reason) < 3:
        raise HTTPException(400, detail='Say why the full packet is being sent again.')
    documents, missing = await call(lambda: ledger.full_packet(case_id, recipient))
    waiting = await call(lambda: ledger.in_flight(case_id, recipient))
    if not documents:
        raise HTTPException(409, detail='Faxbot has no kept copy of any document sent for this case to this '
                                        'recipient. Add the documents to the case, then send the full packet.')
    packet = PacketPlan(tuple(documents), (), False, ('repair',) * len(documents))
    view = {**packet_view(case_id, recipient, packet), 'missing': missing, 'packets_in_flight': len(waiting),
            'reason': reason}
    if payload.preview:
        return {**view, 'fax_id': None}
    job_id = await send_packet(request, identity, ledger, case_id, recipient, packet, kind='repair', reason=reason)
    return {**view, 'fax_id': job_id}


class RecipientPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reuse_days: int | None = Field(default=None, ge=0, le=3650)
    version: int | None = Field(default=None, ge=0)


@recipients_router.get('/{number}', dependencies=[Depends(require_permission('settings:read'))])
async def case_recipient(number: str, request: Request):
    """How long references to documents this recipient acknowledged are trusted."""
    recipient = _number(number, request)
    ledger = _ledger(request)
    return await call(lambda: ledger.recipient(recipient))


@recipients_router.patch('/{number}', dependencies=[Depends(require_permission('settings:write'))])
async def update_case_recipient(number: str, payload: RecipientPatch, request: Request):
    """Set the reuse period: a number of days, 0 for no limit, or null for Faxbot's default."""
    recipient = _number(number, request)
    ledger = _ledger(request)
    return await call(lambda: ledger.set_reuse_days(recipient, payload.reuse_days, expected_version=payload.version))


# One router for main.py: the case routes, recipient reuse periods and checklists.
router = APIRouter()
router.include_router(cases_routes)
router.include_router(recipients_router)
