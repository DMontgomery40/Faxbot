"""Send a case packet that leaves out documents the recipient already accepted.

Sending needs fax:send (like POST /fax); reading a case record needs
settings:read. Whether a recipient accepts references is part of its
destination profile (PATCH /routing/destinations/{number}).
"""
import os
from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine
from ..routing.database import DeliveryStoreError
from ..routing.numbers import InvalidNumber, normalize_number
from ..routing.submit import accept_generated_fax
from .ledger import CaseDocument, CaseInputError, CaseLedger, check_case_id, compose


router = APIRouter(prefix='/cases', tags=['Case ledger'])
MAX_DOCUMENTS = 50


def _ledger(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    try:
        return CaseLedger(engine)
    except DeliveryStoreError:
        raise HTTPException(503, detail='Case records are unavailable.') from None


def _inputs(case_id, to, request):
    country = request.scope['faxbot.configuration'].active.values.fax_default_country
    try:
        return check_case_id(case_id), normalize_number(to, country=country)
    except (CaseInputError, InvalidNumber) as error:
        raise HTTPException(400, detail=str(error)) from None


def _entry_view(entry):
    return {'title': entry['title'], 'pages': entry['page_count'], 'reference': entry['digest'][:12],
            'accepted': entry['accepted_at'] is not None, 'accepted_at': entry['accepted_at'],
            'fax_id': entry['source_job_id']}


@router.get('/{case_id}/documents', dependencies=[Depends(require_permission('settings:read'))])
async def case_documents(case_id: str, request: Request, to: str = Query(...)):
    case_id, recipient = _inputs(case_id, to, request)
    ledger = _ledger(request)
    try:
        entries = await run_lifecycle_step(lambda: ledger.entries(case_id, recipient))
        allowed = await run_lifecycle_step(lambda: ledger.references_allowed(recipient))
    except DeliveryStoreError:
        raise HTTPException(503, detail='Case records are unavailable.') from None
    return {'case_id': case_id, 'to': recipient, 'accepts_references': allowed,
            'documents': [_entry_view(entry) for entry in entries]}


def _read_documents(uploads, titles, data_dir, max_bytes):
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
            title = (titles[index] if index < len(titles) else '') or Path(name or '').stem or f'Document {index + 1}'
            documents.append(CaseDocument(title.strip()[:200], data, pages))
    return documents


@router.post('/{case_id}/faxes', status_code=202)
async def send_case_packet(case_id: str, request: Request, to: str = Form(...),
                           documents: list[UploadFile] = File(...), titles: list[str] = Form(default=[]),
                           preview: bool = Form(False), identity=Depends(require_permission('fax:send', resource='personal'))):
    from ..access.http import runtime as access_runtime
    case_id, recipient = _inputs(case_id, to, request)
    if not 0 < len(documents) <= MAX_DOCUMENTS:
        raise HTTPException(400, detail=f'Send between 1 and {MAX_DOCUMENTS} documents.')
    revision = request.scope['faxbot.configuration'].active
    values = revision.values
    max_bytes = values.max_file_size_mb * 1024 * 1024
    uploads = [(await upload.read(max_bytes + 1), upload.filename) for upload in documents]
    ledger = _ledger(request)
    _, runtime = installation_engine(request.app)

    def plan():
        os.makedirs(values.fax_data_dir, exist_ok=True)
        return ledger.plan(case_id, recipient, _read_documents(uploads, titles, values.fax_data_dir, max_bytes))
    try:
        packet = await run_lifecycle_step(plan)
    except CaseInputError as error:
        raise HTTPException(400, detail=str(error)) from None
    view = {'case_id': case_id, 'to': recipient, 'accepts_references': packet.references_allowed,
            'pages': packet.pages, 'pages_saved': packet.pages_saved,
            'documents': [{'title': document.title, 'pages': document.pages, 'status': 'included'}
                          for document in packet.included]
                         + [{'title': entry['title'], 'pages': entry['page_count'], 'status': 'referenced'}
                            for entry in packet.referenced]}
    if not packet.included:
        raise HTTPException(409, detail='Every document was already accepted for this case; there is nothing new to send.')
    if preview:
        return {**view, 'fax_id': None}
    organization = values.direct_organization.strip() or values.fax_header or 'Faxbot'
    access = access_runtime(request)

    def send():
        document = compose(case_id, recipient, packet, organization)
        job_id = accept_generated_fax(runtime, access, identity.actor, revision, to_number=recipient,
                                      document=document, file_name=f'case-{case_id}.pdf', pages=packet.pages)
        ledger.record(case_id, recipient, packet, job_id)
        return job_id
    try:
        job_id = await run_lifecycle_step(send)
    except RuntimeError as error:
        raise HTTPException(409, detail=str(error)) from None
    return {**view, 'fax_id': job_id}
