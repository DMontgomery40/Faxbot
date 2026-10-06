"""The SSL Fax engine's result route: each finished job, posted by hylafax/bin/notify.

Authenticated with the engine's own secret (``report_secret`` in
``<data>/hylafax/secrets.json``), never Asterisk's; the job's tag names the
fax and attempt Faxbot created. The engine's scripts keep every report in
the engine's volume until Faxbot answers, so a result is never lost. The same result
posted twice changes nothing (the delivery store keys events by attempt and
outcome; the engine records key on the engine's own call reference).

Besides the delivery result, the call's record gets the confirmed pages and
the other machine's station ID, the engine record gets what SSL Fax did, and
the other number's "accepts SSL Fax" gets one more observation. A T.38 call on
which no fax message ever came back moves new calls to audio fax, as it does
for the built-in engine.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hmac
import re
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import hylafax_engine
from .access.route_policy import require_permission
from .config import settings
from .config_runtime import run_lifecycle_step

def _background(app):
    """Every few minutes: a send the engine took and never reported on waits for a person (see _sweep)."""
    from .routing.background import repeat
    return [('faxbot-engine-unreported', repeat(lambda: _sweep(app), interval=300.0, initial_delay=120.0,
                                                warning='Fax engine results could not be checked.'))]


def _lifespan(app):
    from .routing.background import lifespan_tasks
    return lifespan_tasks(_background)(app)


router = APIRouter(lifespan=_lifespan)

# How long a failed result waits for the trunk side of its call (Asterisk's FaxEngineCall, sent when
# the call hangs up and normally first), so the fax gets the built-in engine's sentence for it.
CALL_WAIT_SECONDS = 5.0


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
    # The communication ID starts again with each new engine container (its spool is not kept), so the
    # attempt keeps the reference unique; received faxes add their arrival time the same way.
    identity = hylafax_engine.parse_tag(payload.get('tag'))
    reference = f'{engine_id}:{commid}' + (f'.{identity[1][:12]}' if identity else '')
    # Speed and compression only from a session that agreed them: a call that never trained has none.
    agreed = payload.get('why') == 'done' or bool(payload.get('pages')) or bool(
        hylafax_engine._text64(payload, 'remote_station_b64', 40))
    return {'engine_ref': reference, 'sslfax': payload.get('sslfax') is True,
            'sslfax_offered': payload.get('sslfax_offered') if isinstance(payload.get('sslfax_offered'), bool)
            else None,
            'transfer_seconds': payload.get('transfer_seconds'), 'session_seconds': payload.get('session_seconds'),
            'signal_rate': hylafax_engine._text64(payload, 'signal_rate_b64', 32) if agreed else None,
            'data_format': hylafax_engine._text64(payload, 'data_format_b64', 32) if agreed else None}


def _record(request, job_id, attempt_id, payload, status, sentence):
    """The call record's half from the engine's result; the call record, or None. Evidence only, never raises."""
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
    return rows[-1] if rows else None


def _record_engine(request, job_id, attempt_id, payload, row):
    """The engine record and SSL Fax observation, once the delivery store has taken the result: an attempt
    whose result the store refused keeps no engine reference, so the restart rule still finds it."""
    from . import hylafax_records
    from .routing.background import installation_engine
    details = _engine_details(payload)
    if details is None:
        return
    try:
        engine, _ = installation_engine(request.app)
        records = hylafax_records.records_for(engine)
    except Exception:
        return
    hylafax_records.safely(records.record_result, direction='outbound', call_key=attempt_id, details=details,
                           job_id=job_id, number=(row or {}).get('called'))


async def _settled_call(request, attempt_id, row):
    """The call record once both halves are in (its verdict set), waiting briefly for the trunk side."""
    from . import sip_calls
    from .routing.background import installation_engine
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CALL_WAIT_SECONDS
    while row is not None and row.get('verdict') is None and row.get('ended_at') is None and loop.time() < deadline:
        await asyncio.sleep(0.5)
        try:
            engine, _ = installation_engine(request.app)
            rows = await run_lifecycle_step(lambda: sip_calls.SipCallRecords(engine).for_attempt(attempt_id))
        except Exception:
            return row
        row = rows[-1] if rows else row
    return row


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


def _require_engine(x_internal_secret):
    """The engine's own report secret; Asterisk's inbound secret is not accepted here."""
    expected = hylafax_engine.report_secret(settings.fax_data_dir)
    if not expected:
        raise HTTPException(401, detail='Internal secret not configured')
    if not hmac.compare_digest((x_internal_secret or '').encode(), expected.encode()):
        raise HTTPException(401, detail='Invalid internal secret')


def _require_receiving(path):
    """Receiving over the trunk is off, or another provider receives: for the engine's reports a state that
    may change, so 503 (the engine keeps the report and sends it again), never 404."""
    from .inbound.http import _require_route
    try:
        _require_route('sip', path)
    except HTTPException as error:
        if error.status_code != 404:
            raise
        raise HTTPException(503, detail='Receiving over the SIP trunk is off; the fax engine keeps its report.') from None


@router.post('/_internal/hylafax/inbound')
async def engine_inbound(request: Request, payload: dict = Body(...),
                         x_internal_secret: Optional[str] = Header(default=None)):
    """A fax the engine received: the same hand-over as Asterisk's, for images in the engine's out folder only."""
    from .inbound.http import receive_handover
    _require_engine(x_internal_secret)
    _require_receiving('/_internal/hylafax/inbound')
    answer = await run_lifecycle_step(
        lambda: receive_handover(request, payload, str(hylafax_engine.received_dir(settings))))
    # The engine's result decides this call's record, also when Asterisk's event for it came first.
    call = payload.get('call') if isinstance(payload.get('call'), dict) else {}
    pages = call.get('pages') if isinstance(call.get('pages'), int) else None
    row = await run_lifecycle_step(lambda: _engine_receive(
        request, str(payload.get('uniqueid') or ''), success=payload.get('faxstatus') == 'SUCCESS',
        pages=pages, station=hylafax_engine._text64(call, 'remote_station_id_b64', 40),
        reason=hylafax_engine._text64(payload, 'reason_b64', 64), did=payload.get('to_number'),
        caller=payload.get('from_number'), inbound_fax_id=answer.get('id')))
    from .sip_calls import engine_audio_check
    engine_audio_check(row)
    return answer


def _engine_receive(request, call_id, **fields):
    """Record the engine's result for one received call; the call record, or None. Never raises."""
    from . import hylafax_records, sip_calls
    from .inbound.http import received_number
    from .routing.background import installation_engine
    try:
        engine, _ = installation_engine(request.app)
        records = sip_calls.SipCallRecords(engine)
        for name in ('did', 'caller'):
            fields[name] = received_number(fields.get(name))
        row_id = records.record_engine_receive(call_id, preset=settings.sip_trunk_preset, **fields)
        row = records.call(row_id) if row_id else None
    except Exception:
        import logging
        logging.getLogger(__name__).warning('A SIP call record could not be saved.')
        return None
    return row


@router.post('/_internal/hylafax/received-failed')
async def engine_receive_failed(request: Request, payload: dict = Body(...),
                                x_internal_secret: Optional[str] = Header(default=None)):
    """A call the engine answered that left no fax (hylafax/bin/sessions): its record and sentence."""
    _require_engine(x_internal_secret)
    _require_receiving('/_internal/hylafax/received-failed')
    engine_id = payload.get('engine_id') if isinstance(payload.get('engine_id'), str) else ''
    key = payload.get('key') if isinstance(payload.get('key'), str) else ''
    token = payload.get('token') if isinstance(payload.get('token'), str) else ''
    if not re.fullmatch(r'[a-f0-9]{16}', engine_id) or not re.fullmatch(r'[0-9]{1,12}-[0-9]{1,12}', key):
        raise HTTPException(400, detail='Unknown fax engine call')
    if token and not re.fullmatch(r'[0-9]{1,40}', token):
        raise HTTPException(400, detail='Unknown fax engine call')
    call_id = f'engine.{token}' if token else f'hylafax.{engine_id}.{key}'
    row = await run_lifecycle_step(lambda: _engine_receive(
        request, call_id, success=False, pages=0, station=None,
        reason=hylafax_engine._text64(payload, 'reason_b64', 64) or 'fax failed',
        did=payload.get('called'), caller=payload.get('caller'), inbound_fax_id=None))
    from . import hylafax_records
    from .routing.background import installation_engine
    # The key is <communication id>-<time bin/sessions reported the call>: a new engine container starts its
    # communication IDs again, so the time keeps the reference unique, and the same report posted again
    # carries the same key.
    try:
        engine, _ = installation_engine(request.app)
        hylafax_records.safely(hylafax_records.records_for(engine).record_result, direction='inbound',
                               call_key=call_id, details={'engine_ref': f'{engine_id}:{key}', 'sslfax': False},
                               number=(row or {}).get('caller'))
    except Exception:
        pass
    from .sip_calls import engine_audio_check
    engine_audio_check(row)
    return {'status': 'ok', 'summary': (row or {}).get('summary')}


# Engine restarts: a fax the engine took before it started again has no result coming.
RESTART_WINDOW = timedelta(days=7)


@router.post('/_internal/hylafax/started')
async def engine_started(request: Request, payload: dict = Body(...),
                         x_internal_secret: Optional[str] = Header(default=None)):
    """The engine started (again). Every fax it took earlier and never reported on waits for a person
    (uncertain): its call may have reached the other machine, and it is never sent again by itself.
    A result that arrives later still replaces that with the real outcome."""
    _require_engine(x_internal_secret)
    started = payload.get('started')
    if not isinstance(started, int) or isinstance(started, bool) or not 946684800 <= started <= 4102444800:
        raise HTTPException(400, detail='Unknown engine start time')
    before = datetime.fromtimestamp(started, timezone.utc).replace(tzinfo=None)
    store = _store(request)
    marked = await run_lifecycle_step(lambda: _settle_interrupted(request, store, before))
    if marked:
        from .audit import audit_event
        audit_event('native_result_requires_reconciliation', provider='sip', engine='hylafax',
                    reason='engine_restarted', count=marked)
    return {'status': 'ok', 'uncertain': marked}


def _settle_interrupted(request, store, before):
    from .routing.background import installation_engine
    engine, _ = installation_engine(request.app)
    return _settle_unreported(engine, store, before, 'restarted')


def _settle_unreported(engine, store, before, why):
    """Each send the engine took before ``before`` and never reported on, still in progress, waits for a
    person (uncertain); the number marked."""
    from . import hylafax_records
    from .config_store import ConfigurationStoreError
    from .outbound_store import DeliveryConflict
    pending = hylafax_records.records_for(engine).unfinished_sends(before=before, since=before - RESTART_WINDOW)
    marked = 0
    for job_id, attempt_id in pending:
        try:
            row = store.get(job_id)
            if row['state'] != 'in_progress' or row['attempt_id'] != attempt_id:
                continue
            _, profile = store.attempt_context(job_id, attempt_id)
            if store.record_unconfirmed(job_id, attempt_id=attempt_id, profile_id=profile.id,
                                        event_key=f'{attempt_id}:hylafax:{why}'):
                marked += 1
        except (DeliveryConflict, ConfigurationStoreError, LookupError, ValueError):
            continue
    return marked


# A send the engine took and never reported on (its report refused for good, or lost) waits for a person after
# this long; no fax call lasts it.
NO_RESULT_AFTER = timedelta(hours=3)


def _sweep(app):
    from .outbound_store import OutboundStore
    from .routing.background import installation_engine
    engine, runtime = installation_engine(app)
    if engine is None or runtime is None or not getattr(runtime, 'serving', False):
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    marked = _settle_unreported(engine, OutboundStore(runtime.manager.store), now - NO_RESULT_AFTER, 'no-result')
    if marked:
        from .audit import audit_event
        audit_event('native_result_requires_reconciliation', provider='sip', engine='hylafax',
                    reason='no_engine_result', count=marked)
    return False


@router.post('/_internal/hylafax/result')
async def engine_result(request: Request, payload: dict = Body(...),
                        x_internal_secret: Optional[str] = Header(default=None)):
    _require_engine(x_internal_secret)
    identity = hylafax_engine.parse_tag(payload.get('tag'))
    if identity is None:
        raise HTTPException(400, detail='Unknown fax engine job')
    job_id, attempt_id = identity
    status, sentence, category = hylafax_engine.result_outcome(payload)
    import sqlalchemy as sa
    from .config_store import ConfigurationStoreError, UnboundProviderProfile
    from .outbound_store import DeliveryConflict
    store = _store(request)
    try:
        _, profile = await run_lifecycle_step(lambda: store.attempt_context(job_id, attempt_id))
    except UnboundProviderProfile:
        raise HTTPException(404, detail='Unknown fax engine job') from None
    except (ConfigurationStoreError, sa.exc.SQLAlchemyError):
        # Storage that is not available now: the engine keeps the result and sends it again.
        raise HTTPException(503, detail='Fax engine results cannot be saved now; try again.') from None
    except DeliveryConflict:
        raise HTTPException(409, detail='The fax engine result does not match the fax.') from None
    except (LookupError, ValueError):
        raise HTTPException(404, detail='Unknown fax engine job') from None
    if profile.configuration.provider_id != 'sip' or profile.configuration.manifest is not None:
        raise HTTPException(409, detail='The fax engine job does not match the fax.')
    why = payload.get('why') if isinstance(payload.get('why'), str) else ''
    row = await run_lifecycle_step(lambda: _record(request, job_id, attempt_id, payload, status, sentence))
    if status == hylafax_engine.UNCERTAIN and category == 'pages_unconfirmed' and not hylafax_engine.exchanged(payload):
        # The engine's words leave it open, but the trunk may know no fax machine was ever heard (no fax
        # signal, no sound back, not a fax machine): then nothing was delivered and it failed for certain.
        from . import sip_calls
        row = await _settled_call(request, attempt_id, row)
        if (row or {}).get('verdict') in (sip_calls.NO_FAX_SIGNAL, 'no_media_back', 'no_fax_answer'):
            status, category = 'failed', None
    if status == 'failed' and category is None:
        # Nothing confirmed: the same sentence the built-in engine gives for this call, when the trunk
        # side says why (no fax data came back, not a fax machine, no sound).
        from . import sip_calls
        row = await _settled_call(request, attempt_id, row)
        # The engine's own sentence, unless the call says more: the engine heard no fax machine, no
        # sound came back, sound came back but no fax machine answered, or the other machine answered
        # (sent its ID) and the fax did not finish.
        found = (row or {}).get('verdict')
        if found in (sip_calls.NO_FAX_SIGNAL, 'no_media_back', 'no_fax_answer', 'remote_fax_failed'):
            sentence = sip_calls.verdict_sentence(found)
    if status == hylafax_engine.UNCERTAIN:
        # Pages that may have arrived unconfirmed, or a job removed or rejected after it dialed: the fax may
        # have arrived. It waits for a person and is never sent again by itself (no other route takes it).
        try:
            await run_lifecycle_step(lambda: store.record_unconfirmed(
                job_id, attempt_id=attempt_id, profile_id=profile.id, event_key=f'{attempt_id}:hylafax:{why[:40]}'))
        except DeliveryConflict:
            raise HTTPException(409, detail='The fax engine result does not match the fax.') from None
        except UnboundProviderProfile:
            raise HTTPException(404, detail='Unknown fax engine job') from None
        except (ConfigurationStoreError, sa.exc.SQLAlchemyError):
            raise HTTPException(503, detail='Fax engine results cannot be saved now; try again.') from None
        await run_lifecycle_step(lambda: _record_engine(request, job_id, attempt_id, payload, row))
        from .audit import audit_event
        audit_event('native_result_requires_reconciliation', provider='sip', engine='hylafax')
        return {'status': 'uncertain'}
    try:
        await run_lifecycle_step(lambda: store.observe(
            job_id, attempt_id=attempt_id, profile_id=profile.id, provider_sid=job_id, status=status,
            event_key=f'{attempt_id}:hylafax:{why[:40]}', error=sentence, error_category=category))
    except DeliveryConflict:
        raise HTTPException(409, detail='The fax engine result does not match the fax.') from None
    except UnboundProviderProfile:
        raise HTTPException(404, detail='Unknown fax engine job') from None
    except (ConfigurationStoreError, sa.exc.SQLAlchemyError):
        raise HTTPException(503, detail='Fax engine results cannot be saved now; try again.') from None
    await run_lifecycle_step(lambda: _record_engine(request, job_id, attempt_id, payload, row))
    from .sip_calls import engine_audio_check
    engine_audio_check(row)
    return {'status': 'ok'}
