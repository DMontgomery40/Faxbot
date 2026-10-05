"""The SSL Fax engine's result route: each finished job, posted by hylafax/bin/notify.

Authenticated with the same internal secret the Asterisk inbound hand-over
uses; the job's tag names the fax and attempt Faxbot created. The same result
posted twice changes nothing (the delivery store keys events by attempt and
outcome; the engine records key on the engine's own call reference).

Besides the delivery result, the call's record gets the confirmed pages and
the other machine's station ID, the engine record gets what SSL Fax did, and
the other number's "accepts SSL Fax" gets one more observation. A T.38 call on
which no fax message ever came back moves new calls to audio fax, as it does
for the built-in engine.
"""
from __future__ import annotations

import base64
import hmac
import re
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import hylafax_engine
from .access.route_policy import require_permission
from .config import settings
from .config_runtime import run_lifecycle_step

router = APIRouter()

# HylaFAX's words for a call on which the other side never sent one fax message.
_NO_MESSAGE = re.compile(r'T\.30 T1 timeout|No response to (?:DIS|DCS|DTC|EOP|MPS)|No sender protocol', re.I)


def _store(request: Request):
    from .outbound_store import OutboundStore
    runtime = getattr(request.app.state, 'configuration_runtime', None)
    if runtime is None or not runtime.serving:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return OutboundStore(runtime.manager.store)


def _engine_details(payload):
    """The engine record's fields from one notify body."""
    engine_id = payload.get('engine_id') if isinstance(payload.get('engine_id'), str) else ''
    commid = payload.get('commid') if isinstance(payload.get('commid'), str) else ''
    if not re.fullmatch(r'[a-f0-9]{8,32}', engine_id) or not re.fullmatch(r'[0-9]{1,12}', commid):
        return None
    return {'engine_ref': f'{engine_id}:{commid}', 'sslfax': payload.get('sslfax') is True,
            'sslfax_offered': payload.get('sslfax_offered') if isinstance(payload.get('sslfax_offered'), bool)
            else None,
            'transfer_seconds': payload.get('transfer_seconds'), 'session_seconds': payload.get('session_seconds'),
            'signal_rate': hylafax_engine._text64(payload, 'signal_rate_b64', 32),
            'data_format': hylafax_engine._text64(payload, 'data_format_b64', 32)}


def _record(request, job_id, attempt_id, payload, status, sentence):
    """Call record, engine record and SSL Fax observation; evidence only, never raises."""
    from . import hylafax_records, sip_calls
    from .routing.background import installation_engine
    try:
        engine, _ = installation_engine(request.app)
    except Exception:
        return None
    calls = sip_calls.SipCallRecords(engine)
    pages = payload.get('pages') if isinstance(payload.get('pages'), int) else None
    station = hylafax_engine._text64(payload, 'remote_station_b64', 40)
    status_text = hylafax_engine._text64(payload, 'status_b64', 120)
    if status in ('success', 'failed'):
        hylafax_records.safely(calls.record_engine_result, job_id, attempt_id, success=status == 'success',
                               pages=pages, station=station, reason=status_text or sentence)
    rows = hylafax_records.safely(calls.for_attempt, attempt_id) or []
    called = rows[-1].get('called') if rows else None
    details = _engine_details(payload)
    if details is not None:
        records = hylafax_records.records_for(engine)
        hylafax_records.safely(records.record_result, direction='outbound', call_key=attempt_id, details=details,
                               job_id=job_id, number=called)
    return rows[-1] if rows else None


def _audio_switch_check(row, payload, status):
    """A T.38 engine call that never got one fax message back: the same rule as the built-in engine."""
    if row is None or row.get('t38') != 'yes' or status != 'failed' or payload.get('pages'):
        return
    if not _NO_MESSAGE.search(hylafax_engine._text64(payload, 'status_b64', 200)):
        return
    from . import sip_fax_mode
    sip_fax_mode._on_fax_event({
        'Answered': '1', 'Status': 'FAILED', 'Pages': '0', 'Mode': 'T38',
        'Error64': base64.b64encode(b'timed out waiting for initial communication').decode()})


# Recipients, Details: one fax machine's own limits, and whether it takes SSL Fax -------------------

class FaxLimits(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # None: the installation's own fax settings apply.
    max_rate: Optional[int] = Field(default=None)
    ecm: Optional[StrictBool] = None


def _recipient_number(value, request):
    from .routing.numbers import InvalidNumber, normalize_number
    country = request.scope['faxbot.configuration'].active.values.fax_default_country
    try:
        return normalize_number(value, country=country)
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


def recipient_view(engine, number):
    """The Recipients, Details panel: numbers, sentences and limits; dates in ISO for the console to format."""
    from . import hylafax_records
    detail = hylafax_records.records_for(engine).recipient_detail(number)
    accepts = detail['accepts_sslfax']
    sentence = (None if accepts is None else
                'This fax machine can take pages faster, so faxes to it are quicker.' if accepts else
                'This fax machine cannot take pages faster, so faxes to it go the usual way.')
    return {'number': number, **detail, 'sslfax_sentence': sentence}


def _engine_for(request):
    from .routing.background import installation_engine
    engine, _ = installation_engine(request.app)
    return engine


@router.get('/routing/destinations/{number}/fax-limits', dependencies=[Depends(require_permission('settings:read'))])
async def get_fax_limits(number: str, request: Request):
    from . import hylafax_records
    target = _recipient_number(number, request)
    try:
        return await run_lifecycle_step(lambda: recipient_view(_engine_for(request), target))
    except hylafax_records.EngineRecordError:
        raise HTTPException(503, detail='Fax limits are unavailable. Try again.') from None


@router.put('/routing/destinations/{number}/fax-limits')
async def put_fax_limits(number: str, payload: FaxLimits, request: Request,
                         identity=Depends(require_permission('settings:write'))):
    from . import hylafax_records
    target = _recipient_number(number, request)
    actor = getattr(getattr(identity, 'actor', None), 'principal_id', None) or 'settings'

    def save():
        engine = _engine_for(request)
        hylafax_records.records_for(engine).set_recipient_settings(
            target, max_rate=payload.max_rate, ecm=payload.ecm, actor=str(actor))
        return recipient_view(engine, target)
    try:
        result = await run_lifecycle_step(save)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from None
    except hylafax_records.EngineRecordError:
        raise HTTPException(503, detail='Fax limits could not be saved. Try again.') from None
    from .audit import audit_event
    audit_event('recipient_fax_limits', number=target, max_rate=payload.max_rate, ecm=payload.ecm)
    return result


@router.post('/_internal/hylafax/result')
async def engine_result(request: Request, payload: dict = Body(...),
                        x_internal_secret: Optional[str] = Header(default=None)):
    expected = settings.asterisk_inbound_secret
    if not expected:
        raise HTTPException(401, detail='Internal secret not configured')
    if not hmac.compare_digest((x_internal_secret or '').encode(), expected.encode()):
        raise HTTPException(401, detail='Invalid internal secret')
    identity = hylafax_engine.parse_tag(payload.get('tag'))
    if identity is None:
        raise HTTPException(400, detail='Unknown fax engine job')
    job_id, attempt_id = identity
    status, sentence, category = hylafax_engine.result_outcome(payload)
    from .config_store import ConfigurationStoreError
    from .outbound_store import DeliveryConflict
    store = _store(request)
    try:
        _, profile = await run_lifecycle_step(lambda: store.attempt_context(job_id, attempt_id))
    except (DeliveryConflict, ConfigurationStoreError, LookupError, ValueError):
        raise HTTPException(404, detail='Unknown fax engine job') from None
    if profile.configuration.provider_id != 'sip' or profile.configuration.manifest is not None:
        raise HTTPException(409, detail='The fax engine job does not match the fax.')
    why = payload.get('why') if isinstance(payload.get('why'), str) else ''
    row = await run_lifecycle_step(lambda: _record(request, job_id, attempt_id, payload, status, sentence))
    if status == hylafax_engine.UNCERTAIN:
        from .audit import audit_event
        audit_event('native_result_requires_reconciliation', provider='sip', engine='hylafax')
        return {'status': 'uncertain'}
    try:
        await run_lifecycle_step(lambda: store.observe(
            job_id, attempt_id=attempt_id, profile_id=profile.id, provider_sid=job_id, status=status,
            event_key=f'{attempt_id}:hylafax:{why[:40]}', error=sentence, error_category=category))
    except DeliveryConflict:
        raise HTTPException(409, detail='The fax engine result does not match the fax.') from None
    _audio_switch_check(row, payload, status)
    return {'status': 'ok'}
