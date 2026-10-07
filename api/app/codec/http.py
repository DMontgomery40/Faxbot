"""HTTP surface of the experimental payload codec.

Per-number settings use the settings permissions. A sent fax's encoded-pages
line needs read access to that fax; a received fax's decode result and its
decoded original need access to that fax's document.
"""
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import private_operation, require_identity, runtime as access_runtime
from ..access.route_policy import require_permission
from ..audit import audit_event
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine
from ..routing.numbers import InvalidNumber, normalize_number
from . import receive, send
from .store import (CodecConflict, CodecInputError, CodecSettings, CodecStoreError, KeySeal, receipt_for, send_for)

router = APIRouter(prefix='/codec', tags=['Encoded pages (experimental)'])

AGREEMENT = ('The recipient agreed to receive documents as encoded pages that their Faxbot, or the decoder at '
             'faxbot.net/decode, turns back into the original.')
LIMITS = ('Experimental. Use it only for recipients outside HIPAA-style rules: an encoded page is not readable '
          'as a fax until it is decoded.')
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
        state = ('On: when encoded pages cost less on the fax’s route, faxes to this number go as encoded '
                 'pages (experimental).')
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


def _send_view(row):
    if row is None:
        return {'encoded': False, 'sentence': None}
    return {'encoded': True, 'sentence': send.sentence(row), 'pages_original': row['pages_original'],
            'pages_encoded': row['pages_encoded'], 'layout': row['layout'], 'provider': row['provider_id'],
            'encrypted': bool(row['encrypted']), 'seconds_original': row['seconds_original'],
            'seconds_encoded': row['seconds_encoded'], 'experimental': True}


@router.get('/faxes/{job_id}')
async def fax(job_id: str, request: Request, identity=Depends(require_identity)):
    service = access_runtime(request)
    await run_lifecycle_step(private_operation(lambda: service.queries.job(identity.actor, job_id)))
    engine, _ = _engine(request)
    return await _call(lambda: _send_view(send_for(engine, job_id)))


def _receipt_view(receipt):
    if receipt is None:
        return {'encoded': False, 'state': None, 'sentence': None}
    return {'encoded': True, 'state': receipt['state'], 'sentence': receive.sentence(receipt),
            'document_name': receipt.get('document_name'), 'content_type': receipt.get('content_type'),
            'size_bytes': receipt.get('size_bytes'), 'sha256': receipt.get('document_sha256'),
            'pages_encoded': receipt.get('pages_encoded'), 'experimental': True}


async def _received_document(request, identity, inbound_id):
    service = access_runtime(request)
    return await run_lifecycle_step(private_operation(lambda: service.inbound_queries.document(
        identity.actor, inbound_id)))


@router.get('/received/{inbound_id}')
async def received(inbound_id: str, request: Request, identity=Depends(require_identity)):
    """The decode result for a received fax; a fax not checked yet is checked now."""
    document = await _received_document(request, identity, inbound_id)
    engine, runtime = _engine(request)
    revision = request.scope['faxbot.configuration'].active

    def check():
        found = receipt_for(engine, inbound_id)
        if found is not None or document.get('status') != 'received' or not document.get('pdf_path'):
            return _receipt_view(found)
        from ..intake.worker import load_document
        try:
            data = load_document(document['pdf_path'], revision.values)
        except Exception:
            return _receipt_view(None)
        return _receipt_view(receive.check_document(
            engine, inbound_id, data, from_number=document.get('from_number'),
            folder=revision.values.fax_data_dir, seal=_seal(runtime)))
    return await _call(check)


@router.get('/received/{inbound_id}/document')
async def received_document(inbound_id: str, request: Request, identity=Depends(require_identity)):
    await _received_document(request, identity, inbound_id)
    engine, _ = _engine(request)
    receipt = await _call(lambda: receipt_for(engine, inbound_id))
    if receipt is None or receipt['state'] != 'decoded' or not receipt.get('document_path'):
        raise HTTPException(404, detail='This fax has no decoded document.')
    audit_event('codec_document_served', job_id=inbound_id)
    extension = receive.EXTENSIONS.get(receipt['content_type'], '.pdf')
    return FileResponse(receipt['document_path'], media_type=receipt['content_type'],
                        filename=f'decoded_{inbound_id}{extension}',
                        headers={'Cache-Control': 'no-cache, no-store, must-revalidate'})
