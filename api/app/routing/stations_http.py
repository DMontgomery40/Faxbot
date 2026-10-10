"""The station check over HTTP (``stations.py``): Recipients details, Numbers → Sender identity and Sent details.

- ``GET``/``PUT /routing/stations/{number}``: what Faxbot does when the number answers as another fax machine
  (``warn`` or ``refuse``), the stations it expects there, and a station a person confirms.
- ``PUT /routing/stations/mailboxes/{mailbox}``: the same choice for one mailbox's faxes.
- ``GET /routing/stations/faxes/{fax}``: what Sent details say about the stations a fax's calls answered as.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from ..access.http import private_operation, require_identity
from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from . import stations


router = APIRouter(prefix='/routing/stations', tags=['Delivery routes'])
UNAVAILABLE = 'The station check is unavailable right now. Try again in a moment.'
MODE_TEXT = {'warn': 'the fax goes on and Sent details say so', 'refuse': 'Faxbot hangs up before any page'}


class StationChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # 'warn' or 'refuse'; left out: unchanged.
    mode: str | None = Field(default=None, max_length=16)
    # A station a person confirms this number answers as, such as the number on the recipient's letterhead.
    station: str | None = Field(default=None, max_length=40)


def _runtime(request):
    from .background import installation_engine
    engine, runtime = installation_engine(request.app)
    if engine is None or runtime is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    return engine, runtime.manager.store


def _number(value, request):
    from .numbers import InvalidNumber, normalize_number
    country = getattr(request.scope['faxbot.configuration'].active.values, 'fax_default_country', 'US')
    try:
        return normalize_number(value, country=country)
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


def _actor(engine, identity):
    principal = getattr(getattr(identity, 'actor', None), 'principal_id', None)
    if not principal:
        return None, None
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    with engine.connect() as connection:
        return principal, connection.execute(sa.select(principals.c.display_name).where(
            principals.c.id == principal)).scalar_one_or_none()


@router.get('/faxes/{job_id}')
async def fax_stations(job_id: str, request: Request, identity=Depends(require_identity)):
    """What Sent details say about the stations a fax's calls answered as, for anyone who may read that fax."""
    runtime = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: runtime.queries.job(identity.actor, job_id)))
    engine, _ = _runtime(request)
    return {'sentences': await run_lifecycle_step(lambda: stations.fax_sentences(engine, job_id))}


@router.get('/{number}', dependencies=[Depends(require_permission('settings:read'))])
async def get_stations(number: str, request: Request):
    target = _number(number, request)
    engine, _ = _runtime(request)
    return await run_lifecycle_step(lambda: stations.recipient_view(engine, target))


@router.put('/mailboxes/{mailbox_id}')
async def put_mailbox_mode(mailbox_id: str, payload: StationChange, request: Request,
                           identity=Depends(require_permission('settings:write'))):
    engine, configuration = _runtime(request)
    if payload.mode not in stations.MODES or payload.station:
        raise HTTPException(400, detail='Choose warn or refuse for the mailbox.')
    from .reply_number import mailbox_labels
    labels = await run_lifecycle_step(lambda: mailbox_labels(engine))
    if mailbox_id not in labels:
        raise HTTPException(400, detail='There is no such mailbox. Choose one of your mailboxes.')

    def save():
        principal, name = _actor(engine, identity)
        with configuration._locked() as connection:
            stations.set_mode_on(connection, 'mailbox', mailbox_id, payload.mode, actor_principal_id=principal,
                                 actor_name=name, now=datetime.utcnow())
    await run_lifecycle_step(save)
    from ..audit import audit_event
    audit_event('station_check_changed', scope='mailbox', mailbox_id=mailbox_id, mode=payload.mode)
    return {'mailbox_id': mailbox_id, 'mode': payload.mode,
            'sentence': f'Saved. When a number answers as another fax machine on a fax from {labels[mailbox_id]}, '
                        f'{MODE_TEXT[payload.mode]}.'}


@router.put('/{number}')
async def put_stations(number: str, payload: StationChange, request: Request,
                       identity=Depends(require_permission('settings:write'))):
    target = _number(number, request)
    engine, configuration = _runtime(request)
    if payload.mode is None and not payload.station:
        raise HTTPException(400, detail='Choose warn or refuse, or enter the station this number answers as.')
    if payload.mode is not None and payload.mode not in stations.MODES:
        raise HTTPException(400, detail='Choose warn or refuse.')

    def save():
        principal, name = _actor(engine, identity)
        now = datetime.utcnow()
        with configuration._locked() as connection:
            if payload.mode is not None:
                stations.set_mode_on(connection, 'recipient', target, payload.mode, actor_principal_id=principal,
                                     actor_name=name, now=now)
            if payload.station:
                stations.confirm_on(connection, target, payload.station, actor_principal_id=principal,
                                    actor_name=name, now=now)
        return stations.recipient_view(engine, target)
    try:
        view = await run_lifecycle_step(save)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from None
    from ..audit import audit_event
    audit_event('station_check_changed', scope='recipient', number=target, mode=payload.mode,
                confirmed=bool(payload.station))
    parts = ['Saved.']
    if payload.mode is not None:
        parts.append(f'When this number answers as another fax machine, {MODE_TEXT[view["mode"]]}.')
    if payload.station:
        parts.append('Faxbot expects this number to answer as that station too.')
    sentence = ' '.join(parts)
    return {**view, 'sentence': sentence}
