"""NTT Data Connect (Recipients → Details → Data Connect; brief 99, item 2).

Reads need ``settings:read``; recording whether a number's machine takes Data Connect, and turning it on or off for
an NTT Hikari Denwa trunk, need ``settings:write``. Nothing here places a call.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import require_identity
from ..access.route_policy import require_permission
from .http import _call, _number, _store
from .inventory_http import _who


router = APIRouter(prefix='/routing', tags=['Delivery routes'])


def _values(request):
    return request.scope['faxbot.configuration'].active.values


class Capable(BaseModel):
    model_config = ConfigDict(extra='forbid')
    capable: bool
    note: str | None = Field(default=None, max_length=500)


class TrunkSwitch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


@router.get('/destinations/{number}/data-connect', dependencies=[Depends(require_permission('settings:read'))])
async def recipient_data_connect(number: str, request: Request):
    """Whether this number's fax machine takes NTT Data Connect calls, the trunks that place them, and NTT's limits."""
    from .data_connect_store import recipient_view
    number = _number(number, request)
    store = _store(request)
    return await _call(lambda: recipient_view(store.engine, _values(request), number))


@router.put('/destinations/{number}/data-connect', dependencies=[Depends(require_permission('settings:write'))])
async def set_recipient_data_connect(number: str, payload: Capable, request: Request,
                                     identity=Depends(require_identity)):
    """Record whether this number's fax machine takes NTT Data Connect calls."""
    from .data_connect_store import recipient_view, set_fact
    number = _number(number, request)
    store = _store(request)

    def change():
        who = _who(store.engine, identity)
        set_fact(store.engine, number, payload.capable, note=payload.note, actor_id=who['id'],
                 actor_name=who['name'])
        return recipient_view(store.engine, _values(request), number)
    return await _call(change)


@router.put('/data-connect/trunks/{account}', dependencies=[Depends(require_permission('settings:write'))])
async def set_trunk_data_connect(account: str, payload: TrunkSwitch, request: Request,
                                 identity=Depends(require_identity)):
    """Turn NTT Data Connect calls on or off for one NTT Hikari Denwa trunk."""
    from .data_connect_store import DataConnectError, set_trunk, trunk_state
    store = _store(request)

    def change():
        who = _who(store.engine, identity)
        try:
            set_trunk(store.engine, _values(request), account, payload.enabled, actor_id=who['id'],
                      actor_name=who['name'])
        except DataConnectError as error:
            raise HTTPException(400, detail=str(error)) from None
        enabled, reason, _ = trunk_state(store.engine, account)
        return {'account': account, 'enabled': enabled, 'reason': reason}
    return await _call(change)
