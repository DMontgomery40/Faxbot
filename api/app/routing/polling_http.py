"""Recipients, Details → "Collect faxes from this number" and "Faxes this number collects from Faxbot" (M21,
``routing/polling.py``).

``GET /routing/destinations/{number}/polling`` shows your setting for the
number, what collecting would cost your phone line for the faxes it sent you,
its timetable and the newest collections; ``PUT`` turns collecting on or off,
sets the polling password (sealed, never shown) and the timetable (a new row
each time); ``POST .../polling/collect`` asks the SSL Fax engine to call once
and collect the fax the other server holds. The timetable runs here, once a
minute, and collects only from numbers you turned on.

``GET /routing/destinations/{number}/polling/hold`` shows whether the number
may collect faxes from Faxbot and the faxes held for it; ``PUT`` turns that
on or off with the selective polling address and password it must give;
``POST .../polling/hold/faxes`` holds a document (PDF or fax TIFF) for it in
the SSL Fax engine; ``DELETE .../polling/hold/faxes/{id}`` withdraws one.
"""
from __future__ import annotations

from datetime import datetime
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StrictBool
import sqlalchemy as sa

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from . import polling
from .schedule_http import _engine, _number, _values


def _background(app):
    """Once a minute: the collections the timetables name for this minute (``polling.due``)."""
    from .background import repeat_async
    return [('faxbot-polling-timetable', repeat_async(lambda: _collect_due(app), interval=60.0, initial_delay=30.0,
                                                      warning='Timed fax collection could not run.'))]


async def _collect_due(app):
    from .background import installation_engine
    engine, runtime = installation_engine(app)
    if engine is None or runtime is None or not getattr(runtime, 'serving', False):
        return
    from ..ami import ami_client
    # The installation's active settings, as the runtime holds them between requests.
    values = getattr(getattr(getattr(runtime, 'snapshot', None), 'active', None), 'values', None)
    if values is None:
        return
    try:
        started = await polling.collect_due(engine, values, ami_client, seal=polling.PollSeal(runtime.manager.store))
    except (polling.PollStoreError, sa.exc.SQLAlchemyError):
        logging.getLogger(__name__).warning('Timed fax collection could not read its settings.')
        return
    if started:
        from ..audit import audit_event
        audit_event('recipient_polling_collect', number='timetable', count=len(started))


def _lifespan(app):
    from .background import lifespan_tasks
    return lifespan_tasks(_background)(app)


router = APIRouter(prefix='/routing', tags=['Delivery routes'], lifespan=_lifespan)

MAX_HELD_BYTES = 25 * 1024 * 1024


class PollingIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool
    # A name for the other site, such as "Denver office"; None keeps none.
    label: str | None = Field(default=None, max_length=100)
    # The T.30 selective polling address the other fax server asks for (digits); None or empty: none.
    selective: str | None = Field(default=None, max_length=20)
    # The T.30 polling password it asks for: None keeps the one set, "" clears it, digits set a new one.
    password: str | None = Field(default=None, max_length=20)
    # The timetable: times "08:00,16:00", days "mon,tue,wed,thu,fri", the time zone; None keeps, "" clears.
    collect_times: str | None = Field(default=None, max_length=60)
    collect_days: str | None = Field(default=None, max_length=40)
    time_zone: str | None = Field(default=None, max_length=64)


class HoldIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool
    label: str | None = Field(default=None, max_length=100)
    # What the other site must give to get its fax: a selective polling address and a password (None keeps).
    selective: str | None = Field(default=None, max_length=20)
    password: str | None = Field(default=None, max_length=20)


def _actor(identity):
    actor = getattr(identity, 'actor', None)
    return getattr(actor, 'principal_id', None), getattr(actor, 'display_name', None)


def _seal(request):
    from .background import installation_engine
    _, runtime = installation_engine(request.app)
    store = getattr(getattr(runtime, 'manager', None), 'store', None)
    return polling.PollSeal(store) if store is not None else None


def _view(engine, number):
    try:
        return polling.view(engine, number, now=datetime.utcnow())
    except polling.PollStoreError as error:
        raise HTTPException(503, detail=str(error)) from None


def _hold_view(engine, number):
    try:
        return polling.hold_view(engine, number)
    except polling.PollStoreError as error:
        raise HTTPException(503, detail=str(error)) from None


@router.get('/destinations/{number}/polling', dependencies=[Depends(require_permission('settings:read'))])
async def get_polling(number: str, request: Request):
    target = _number(number, request)
    return await run_lifecycle_step(lambda: _view(_engine(request), target))


@router.put('/destinations/{number}/polling')
async def put_polling(number: str, payload: PollingIn, request: Request,
                      identity=Depends(require_permission('settings:write'))):
    target = _number(number, request)
    actor, name = _actor(identity)
    seal = _seal(request)

    def save():
        engine = _engine(request)
        polling.save_source(engine, target, enabled=payload.enabled, label=payload.label,
                            selective=payload.selective, actor=actor, actor_name=name, password=payload.password,
                            seal=seal, collect_times=payload.collect_times, collect_days=payload.collect_days,
                            time_zone=payload.time_zone)
        return _view(engine, target)
    try:
        result = await run_lifecycle_step(save)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from None
    except polling.PollStoreError as error:
        raise HTTPException(503, detail=str(error)) from None
    from ..audit import audit_event
    audit_event('recipient_polling', number=target, enabled=payload.enabled)
    return result


@router.post('/destinations/{number}/polling/collect', status_code=202)
async def collect_now(number: str, request: Request, identity=Depends(require_permission('settings:write'))):
    from ..ami import ami_client
    target = _number(number, request)
    actor, name = _actor(identity)
    engine = _engine(request)
    try:
        request_id = await polling.collect(engine, _values(request), ami_client, target, actor=actor,
                                           actor_name=name, seal=_seal(request))
    except polling.PollRefused as error:
        raise HTTPException(409, detail=str(error)) from None
    except (polling.PollStoreError, sa.exc.SQLAlchemyError):
        raise HTTPException(503, detail='Collecting faxes is unavailable. Try again.') from None
    from ..audit import audit_event
    audit_event('recipient_polling_collect', number=target)
    return {'id': request_id, 'sentence': polling.WAITING,
            **(await run_lifecycle_step(lambda: _view(engine, target)))}


# Faxes held for the number to collect -----------------------------------------------------------------------

@router.get('/destinations/{number}/polling/hold', dependencies=[Depends(require_permission('settings:read'))])
async def get_hold(number: str, request: Request):
    target = _number(number, request)
    return await run_lifecycle_step(lambda: _hold_view(_engine(request), target))


@router.put('/destinations/{number}/polling/hold')
async def put_hold(number: str, payload: HoldIn, request: Request,
                   identity=Depends(require_permission('settings:write'))):
    target = _number(number, request)
    actor, name = _actor(identity)
    seal = _seal(request)

    def save():
        engine = _engine(request)
        polling.save_source(engine, target, enabled=payload.enabled, label=payload.label,
                            selective=payload.selective, actor=actor, actor_name=name, direction='hold',
                            password=payload.password, seal=seal)
        return _hold_view(engine, target)
    try:
        result = await run_lifecycle_step(save)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from None
    except polling.PollStoreError as error:
        raise HTTPException(503, detail=str(error)) from None
    from ..audit import audit_event
    audit_event('recipient_polling_hold', number=target, enabled=payload.enabled)
    return result


@router.post('/destinations/{number}/polling/hold/faxes', status_code=202)
async def hold_fax(number: str, request: Request, file: UploadFile = File(...),
                   identity=Depends(require_permission('settings:write'))):
    """Hold one document (PDF or fax TIFF) for the number to collect."""
    from ..ami import ami_client
    target = _number(number, request)
    actor, name = _actor(identity)
    engine = _engine(request)
    data = await file.read(MAX_HELD_BYTES + 1)
    if not data:
        raise HTTPException(400, detail='The document is empty.')
    if len(data) > MAX_HELD_BYTES:
        raise HTTPException(413, detail='The document is larger than 25 MB.')
    values = _values(request)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'held-document'
        path.write_bytes(data)
        try:
            hold_id = await polling.hold(engine, values, target, path=path, source_name=file.filename, actor=actor,
                                         actor_name=name, seal=_seal(request), ami=ami_client,
                                         header=getattr(values, 'fax_header', '') or '')
        except polling.PollRefused as error:
            raise HTTPException(409, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        except (polling.PollStoreError, sa.exc.SQLAlchemyError):
            raise HTTPException(503, detail='Holding faxes is unavailable. Try again.') from None
    from ..audit import audit_event
    audit_event('recipient_polling_held', number=target)
    return {'id': hold_id, 'sentence': polling.HELD_WAITING,
            **(await run_lifecycle_step(lambda: _hold_view(engine, target)))}


@router.delete('/destinations/{number}/polling/hold/faxes/{hold_id}')
async def withdraw_fax(number: str, hold_id: str, request: Request,
                       identity=Depends(require_permission('settings:write'))):
    target = _number(number, request)
    _, name = _actor(identity)
    engine = _engine(request)
    try:
        kept = await polling.withdraw(engine, _values(request), hold_id, actor_name=name)
    except LookupError:
        raise HTTPException(404, detail='No such held fax.') from None
    except polling.PollRefused as error:
        raise HTTPException(409, detail=str(error)) from None
    except (polling.PollStoreError, sa.exc.SQLAlchemyError):
        raise HTTPException(503, detail='Holding faxes is unavailable. Try again.') from None
    from ..audit import audit_event
    audit_event('recipient_polling_withdrawn', number=target, outcome=kept)
    return {'id': hold_id, 'outcome': kept, **(await run_lifecycle_step(lambda: _hold_view(engine, target)))}
