"""Recipients, Details → "Collect faxes from this number" (M21, ``routing/polling.py``).

``GET /routing/destinations/{number}/polling`` shows your setting for the
number, what collecting would cost your phone line for the faxes it sent you,
and the newest collections; ``PUT`` turns collecting on or off (a new row each
time); ``POST .../polling/collect`` asks the SSL Fax engine to call once and
collect the fax the other server holds. Nothing here polls by itself.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
import sqlalchemy as sa

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from . import polling
from .schedule_http import _engine, _number, _values


router = APIRouter(prefix='/routing', tags=['Delivery routes'])


class PollingIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool
    # A name for the other site, such as "Denver office"; None keeps none.
    label: str | None = Field(default=None, max_length=100)
    # The T.30 selective polling address the other fax server asks for (digits); None or empty: none.
    selective: str | None = Field(default=None, max_length=20)


def _actor(identity):
    actor = getattr(identity, 'actor', None)
    return getattr(actor, 'principal_id', None), getattr(actor, 'display_name', None)


def _view(engine, number):
    try:
        return polling.view(engine, number, now=datetime.utcnow())
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

    def save():
        engine = _engine(request)
        polling.save_source(engine, target, enabled=payload.enabled, label=payload.label,
                            selective=payload.selective, actor=actor, actor_name=name)
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
                                           actor_name=name)
    except polling.PollRefused as error:
        raise HTTPException(409, detail=str(error)) from None
    except (polling.PollStoreError, sa.exc.SQLAlchemyError):
        raise HTTPException(503, detail='Collecting faxes is unavailable. Try again.') from None
    from ..audit import audit_event
    audit_event('recipient_polling_collect', number=target)
    return {'id': request_id, 'sentence': polling.WAITING,
            **(await run_lifecycle_step(lambda: _view(engine, target)))}
