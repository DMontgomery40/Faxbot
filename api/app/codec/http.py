"""HTTP surface of the experimental payload codec.

Per-number settings use the settings permissions. A received fax's decode
result and its decoded original need access to that fax's document. A sent
fax's encoded pages are said in its page line (``pages.views.sent_view``, the
Sent detail), because each attempt chooses its pages' layout.
"""
from datetime import timezone
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import private_operation, require_identity, runtime as access_runtime
from ..access.route_policy import require_permission
from ..audit import audit_event
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine
from ..routing.numbers import InvalidNumber, normalize_number
from . import receive
from .store import CodecConflict, CodecInputError, CodecSettings, CodecStoreError, KeySeal, receipt_for

router = APIRouter(prefix='/codec', tags=['Encoded pages (experimental)'])

AGREEMENT = ('The recipient agreed to receive documents as encoded pages that their Faxbot, or the decoder at '
             'faxbot.net/decode, turns back into the original.')
LIMITS = ('Experimental. The recipient must decode the pages to read the document and agree that encoded pages '
          'meet their document-handling requirements.')
STYLE_TEXT = {'dense': 'Dense pages', 'picture': 'A picture of the first page'}
LEVEL_TEXT = {'low': 'Low', 'medium': 'Medium', 'high': 'High'}


def _engine(request):
    engine, runtime = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return engine, runtime


def _seal(runtime):
    store = getattr(getattr(runtime, 'manager', None), 'store', None)
    return KeySeal(store) if store is not None else None


def _number(value, request):
    country = request.scope['faxbot.configuration'].active.values.fax_default_country
    try:
        return normalize_number(value, country=country)
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except CodecInputError as error:
        raise HTTPException(400, detail=str(error)) from None
    except CodecConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except CodecStoreError:
        raise HTTPException(503, detail='Encoded-page storage is unavailable; upgrade the database.') from None


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value is not None else None


def _change_view(row):
    return {'action': row['action'], 'by': row['actor_name'] or 'Someone with settings access',
            'at': _utc(row['created_at']), 'recipient_agreed': bool(row['recipient_agreed']),
            'style': STYLE_TEXT.get(row['style'], row['style']), 'fec': LEVEL_TEXT.get(row['fec'], row['fec']),
            'key_fingerprint': row['key_fingerprint']}


def _number_view(settings, number):
    setting = settings.get(number)
    history = settings.history(number)
    agreement = next((row for row in history if row['action'] == 'on'), None) if setting['enabled'] else None
    if setting['enabled']:
        state = ('On: Faxbot compares encoded pages with ordinary pages for each attempt. It can choose a '
                 'lower estimated bill or fewer pages on a plan or an unpriced route (experimental).')
    else:
        state = 'Off: faxes to this number go as normal pages.'
    return {'number': number, 'enabled': setting['enabled'], 'style': setting['style'], 'fec': setting['fec'],
            'has_key': setting['has_key'], 'key_fingerprint': setting['key_fingerprint'],
            'version': setting['version'], 'state_sentence': state,
            'agreement': None if agreement is None else _change_view(agreement),
            'history': [_change_view(row) for row in history], 'agreement_text': AGREEMENT, 'limits_text': LIMITS}


@router.get('/numbers/{number}', dependencies=[Depends(require_permission('settings:read'))])
async def get_number(number: str, request: Request):
    number = _number(number, request)
    engine, runtime = _engine(request)
    return await _call(lambda: _number_view(CodecSettings(engine, _seal(runtime)), number))


class NumberSetting(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool
    recipient_agreed: bool = False
    style: str | None = Field(default=None, pattern='^(dense|picture)$')
    fec: str | None = Field(default=None, pattern='^(low|medium|high)$')
    shared_key: str | None = Field(default=None, min_length=8, max_length=200)
    clear_key: bool = False
    version: int | None = Field(default=None, ge=0)


def _actor_name(engine, actor):
    import sqlalchemy as sa
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    try:
        with engine.connect() as connection:
            return connection.execute(sa.select(principals.c.display_name).where(
                principals.c.id == actor.principal_id)).scalar()
    except Exception:
        return None


async def _save(request, identity, number, payload):
    engine, runtime = _engine(request)
    settings = CodecSettings(engine, _seal(runtime))

    def save():
        _, action = settings.save(
            number, enabled=payload.enabled, recipient_agreed=payload.recipient_agreed,
            actor=identity.actor.replay_scope, actor_name=_actor_name(engine, identity.actor),
            style=payload.style, fec=payload.fec, secret=payload.shared_key, clear_key=payload.clear_key,
            expected_version=payload.version)
        if action is not None:
            # Never the key: only whether one is set and its fingerprint.
            view = settings.get(number)
            audit_event('codec_' + action, to_number=number, recipient_agreed=payload.recipient_agreed,
                        style=view['style'], fec=view['fec'], key_fingerprint=view['key_fingerprint'])
        return _number_view(settings, number)
    return await _call(save)


@router.put('/numbers/{number}')
async def put_number(number: str, payload: NumberSetting, request: Request,
                     identity=Depends(require_permission('settings:write'))):
    return await _save(request, identity, _number(number, request), payload)


@router.delete('/numbers/{number}')
async def delete_number(number: str, request: Request, identity=Depends(require_permission('settings:write'))):
    return await _save(request, identity, _number(number, request), NumberSetting(enabled=False))


def _receipt_view(receipt, document_available=False):
    if receipt is None:
        return {'encoded': False, 'state': None, 'sentence': None, 'document_available': False}
    sentence = receive.sentence(receipt)
    if receipt['state'] == 'decoded' and not document_available:
        sentence += ' The decoded original is no longer available.'
    return {'encoded': True, 'state': receipt['state'], 'sentence': sentence,
            'document_available': document_available,
            'document_name': receipt.get('document_name'), 'content_type': receipt.get('content_type'),
            'size_bytes': receipt.get('size_bytes'), 'sha256': receipt.get('document_sha256'),
            'pages_encoded': receipt.get('pages_encoded'), 'experimental': True}


async def _received_document(request, identity, inbound_id):
    service = access_runtime(request)
    return await run_lifecycle_step(private_operation(lambda: service.inbound_queries.document(
        identity.actor, inbound_id)))


def _available(receipt, document):
    return bool(receipt and receipt['state'] == 'decoded' and receipt.get('document_path')
                and document and document['status'] == 'received' and document['pdf_path']
                and Path(receipt['document_path']).is_file())


async def _check_received(request, identity, inbound_id):
    """Both detail and direct download perform the same authorized, key-change-bounded check."""
    await _received_document(request, identity, inbound_id)
    engine, runtime = _engine(request)
    revision = request.scope['faxbot.configuration'].active

    def check():
        from ..inbound.retention import locked_document
        with locked_document(engine, inbound_id) as (connection, document):
            found = receipt_for(engine, inbound_id, connection=connection)
            already_decoded = found is not None and found['state'] == 'decoded'
            unavailable = not document or document['status'] != 'received' or not document['pdf_path']
            if already_decoded or unavailable:
                return found, _available(found, document)
        if found is not None and not CodecSettings(engine).keys_changed_since(found['created_at']):
            return found, False
        from ..intake.worker import load_document
        try:
            data = load_document(document['pdf_path'], revision.values)
        except Exception:
            return found, False
        receive.check_document(
            engine, inbound_id, data, from_number=document.get('from_number'),
            folder=revision.values.fax_data_dir, seal=_seal(runtime))
        with locked_document(engine, inbound_id) as (connection, document):
            found = receipt_for(engine, inbound_id, connection=connection)
            return found, _available(found, document)
    return engine, await _call(check)


@router.get('/received/{inbound_id}')
async def received(inbound_id: str, request: Request, identity=Depends(require_identity)):
    """Check a received document once, or retry a failure after shared-key settings change."""
    _, (receipt, available) = await _check_received(request, identity, inbound_id)
    return _receipt_view(receipt, available)


@router.get('/received/{inbound_id}/document')
async def received_document(inbound_id: str, request: Request, identity=Depends(require_identity)):
    engine, (_, available) = await _check_received(request, identity, inbound_id)
    if not available:
        raise HTTPException(404, detail='This fax has no decoded document.')
    opened = []

    def acquire():
        from ..inbound.retention import locked_document
        with locked_document(engine, inbound_id) as (connection, document):
            receipt = receipt_for(engine, inbound_id, connection=connection)
            if not _available(receipt, document):
                raise HTTPException(404, detail='This fax has no decoded document.')
            try:
                handle = Path(receipt['document_path']).open('rb')
            except OSError:
                raise HTTPException(404, detail='This fax has no decoded document.') from None
            opened.append(handle)
            return receipt, handle

    try:
        receipt, handle = await _call(acquire)
        audit_event('codec_document_served', job_id=inbound_id)
        extension = receive.EXTENSIONS.get(receipt['content_type'], '.pdf')
        filename = quote(f'decoded_{inbound_id}{extension}')
        return _DecodedResponse(handle, media_type=receipt['content_type'], headers={
            'Content-Disposition': f"attachment; filename*=utf-8''{filename}",
            'Content-Length': str(os.fstat(handle.fileno()).st_size),
            'Cache-Control': 'no-cache, no-store, must-revalidate'})
    except BaseException:
        # run_lifecycle_step joins acquisition even when the request is cancelled.
        for handle in opened:
            handle.close()
        raise


class _DecodedResponse(StreamingResponse):
    """A retained original opened under its parent lock; cleanup may unlink it during transfer."""

    def __init__(self, handle, **kwargs):
        self.handle = handle
        super().__init__(self.chunks(), **kwargs)

    def chunks(self):
        while chunk := self.handle.read(64 * 1024):
            yield chunk

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.handle.close()
