"""Administration → System health → Receiving readiness, and receive owners (``receive_readiness.py``, brief 92, RF).

``GET /receiving/readiness`` judges each number Faxbot receives on, from receiving evidence only.
``POST /receiving/owners`` names the one endpoint that receives a number's faxes, or releases it.
``GET /<provider>-inbound[/<account>]?faxbot_reach=<code>`` answers the readiness check's own one-time code, so
Faxbot can prove its receiving address reaches it through the public address; any other request gets 404.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from . import receive_readiness as readiness
from .access.route_policy import require_permission
from .accounts import WEBHOOK_PATHS
from .config_runtime import run_lifecycle_step
from .people_time import short


router = APIRouter(tags=['Diagnostics'])
UNAVAILABLE = 'Receiving readiness is unavailable right now. Try again in a moment.'


def _engine(request):
    from .routing.background import installation_engine
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    return engine


async def trunk_registration(values):
    """'registered', 'rejected' or 'unknown' from the fax engine's trunk sign-ins; None without a trunk."""
    from .inbound.sip_handover import receives_over_trunk
    if not receives_over_trunk(values):
        return None
    from .diagnostics_report import engine_rows
    rows = await engine_rows('registrations')
    if not rows['available'] or not rows['rows']:
        return 'unknown'
    states = {str(row[1]).strip().lower() for row in rows['rows'] if len(row) > 1}
    if 'registered' in states:
        return 'registered'
    if states & {'rejected', 'unregistered', 'stopped'}:
        return 'rejected'
    return 'unknown'


@router.get('/receiving/readiness', dependencies=[Depends(require_permission('settings:read'))])
async def get_readiness(request: Request):
    """Whether each of your numbers can receive faxes now, from receiving evidence only."""
    engine = _engine(request)
    values = request.scope['faxbot.configuration'].active.values
    registration = await trunk_registration(values)

    def judge():
        views = readiness.check(engine, values, registration=registration)
        return {'numbers': [readiness.as_dict(view) for view in views], 'checked_text': short(readiness.utcnow()),
                'receiving_on': bool(getattr(values, 'inbound_enabled', False))}
    return await run_lifecycle_step(judge)


class OwnerIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    number: str = Field(min_length=3, max_length=40)
    # A receiving account's key, 'elsewhere' (a system outside this Faxbot, named by ``label``), or None to release.
    owner: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=120)
    move: bool = False
    note: str | None = Field(default=None, max_length=300)


@router.post('/receiving/owners')
async def set_owner(payload: OwnerIn, request: Request, identity=Depends(require_permission('settings:write'))):
    """Name the one receiver of a number's faxes; another receiver's number is refused unless you move it."""
    engine = _engine(request)
    values = request.scope['faxbot.configuration'].active.values
    from .routing.numbers import InvalidNumber, normalize_number
    try:
        number = normalize_number(payload.number, country=getattr(values, 'fax_default_country', 'US') or 'US')
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None
    label = payload.label
    if payload.owner and payload.owner != readiness.ELSEWHERE:
        from .accounts import account_named
        account = account_named(values, payload.owner)
        if account is None or not account.receives:
            raise HTTPException(400, detail=f'{payload.owner} is not one of your receiving accounts.')
        label = account.label

    def save():
        principal = getattr(getattr(identity, 'actor', None), 'principal_id', None)
        try:
            row = readiness.claim(engine, number, payload.owner, label=label, move=payload.move, note=payload.note,
                                  actor_id=principal)
        except readiness.OwnerConflict as error:
            raise HTTPException(409, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        sentence = (f'{row["owner_label"] or row["owner"]} now receives the faxes for {number}.' if row['owner']
                    else f'No receiver is named for {number} any more.')
        return {'number': number, 'owner': row['owner'], 'owner_label': row['owner_label'], 'sentence': sentence}
    try:
        return await run_lifecycle_step(save)
    except sa.exc.NoSuchTableError:
        raise HTTPException(503, detail=UNAVAILABLE) from None


def _reach(code):
    if not code or not readiness.take_code(code):
        raise HTTPException(404, detail='Not Found')
    return PlainTextResponse(code, headers={'Cache-Control': 'no-store'})


for _path in sorted(set(WEBHOOK_PATHS.values())):
    async def _reach_path(faxbot_reach: str | None = Query(default=None, max_length=64)):
        """Answers only this server's own one-time readiness code; anything else is 404."""
        return _reach(faxbot_reach)

    async def _reach_account(key: str, faxbot_reach: str | None = Query(default=None, max_length=64)):
        """Answers only this server's own one-time readiness code; anything else is 404."""
        return _reach(faxbot_reach)

    router.add_api_route(_path, _reach_path, methods=['GET'], include_in_schema=False)
    router.add_api_route(_path + '/{key}', _reach_account, methods=['GET'], include_in_schema=False)
