"""Providers → In use → "Where Faxbot may dial": the dialing guard's classes, and changing one (``guard.py``).

``GET /routing/dialing`` lists every class of numbers and every country Faxbot
knows about here, with whether it may dial it and why. ``PUT
/routing/dialing/{class}`` allows or blocks a class, puts it back to Faxbot's
default, or sets a per-minute price ceiling for it. Every change is a new row;
the newest counts, and the older ones stay as history.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from .background import installation_engine
from . import guard


router = APIRouter(prefix='/routing', tags=['Delivery routes'])
UNAVAILABLE = 'Where Faxbot may dial is unavailable right now. Try again in a moment.'


class DialingChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # 'allowed', 'blocked', or 'default' (back to Faxbot's own default for the class).
    state: str = Field(min_length=1, max_length=16)
    # The highest price a minute a call to this class may cost, as decimal text ("0.25"); empty or None: none.
    # Left out: the class keeps the ceiling it has.
    ceiling: str | None = Field(default=None, max_length=16)


def _runtime(request):
    engine, runtime = installation_engine(request.app)
    if engine is None or runtime is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    return engine, runtime.manager.store


def _home(request):
    return getattr(request.scope['faxbot.configuration'].active.values, 'fax_default_country', 'US') or 'US'


def _currency(engine):
    """The currency of the installation's rate cards (the first saved), else US dollars."""
    cards = sa.table('provider_rate_cards', sa.column('currency'))
    with engine.connect() as connection:
        found = connection.execute(sa.select(cards.c.currency).limit(1)).scalar_one_or_none()
    return (found or 'USD').upper()


def _actor_name(engine, identity):
    principal = getattr(getattr(identity, 'actor', None), 'principal_id', None)
    if not principal:
        return None
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    with engine.connect() as connection:
        return connection.execute(sa.select(principals.c.display_name).where(
            principals.c.id == principal)).scalar_one_or_none()


@router.get('/dialing', dependencies=[Depends(require_permission('settings:read'))])
async def get_dialing(request: Request):
    engine, configuration = _runtime(request)
    home = _home(request)

    def read():
        # The first read records the countries already delivered to, so it runs under the configuration lock.
        with configuration._locked() as connection:
            return guard.view_on(connection, home, datetime.utcnow())
    return await run_lifecycle_step(read)


@router.put('/dialing/{class_key}')
async def put_dialing(class_key: str, payload: DialingChange, request: Request,
                      identity=Depends(require_permission('settings:write'))):
    engine, configuration = _runtime(request)
    home = _home(request)
    try:
        key = guard.parse_class(class_key, home)
    except guard.GuardInputError as error:
        raise HTTPException(400, detail=str(error)) from None
    principal = getattr(getattr(identity, 'actor', None), 'principal_id', None)

    def change():
        name = _actor_name(engine, identity)
        currency = _currency(engine)
        with configuration._locked() as connection:
            now = datetime.utcnow()
            ceiling = payload.ceiling if 'ceiling' in payload.model_fields_set else guard.KEEP
            setting = guard.change_on(connection, key, state=payload.state, ceiling=ceiling,
                                      currency=currency, actor_principal_id=principal, actor_name=name,
                                      home_country=home, now=now)
            return setting, guard.view_on(connection, home, now)
    try:
        setting, view = await run_lifecycle_step(change)
    except guard.GuardInputError as error:
        raise HTTPException(400, detail=str(error)) from None
    from ..audit import audit_event
    audit_event('dialing_class_changed', class_key=key, state=setting.state,
                ceiling_micros=setting.ceiling_micros, currency=setting.currency)
    return {**view, 'sentence': guard.change_sentence(key, setting), 'changed': key}
