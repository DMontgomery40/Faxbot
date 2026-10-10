"""Exchange rates (Savings & optimization → Prices & plans → Exchange rates; brief 99, item 1).

Reads need ``settings:read``; setting or clearing your own rate, turning the reference rates on or off, and
reading them now need ``settings:write``. Nothing here changes a stored price: rates only compare them.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import require_identity
from ..access.route_policy import require_permission
from .http import _call, _store
from .inventory_http import _who


router = APIRouter(prefix='/routing/exchange-rates', tags=['Delivery routes'])


def _zone(request):
    return request.scope['faxbot.configuration'].active.values.time_zone


def _view(request, store):
    from .exchange import view
    return view(store.engine, zone_name=_zone(request))


@router.get('', dependencies=[Depends(require_permission('settings:read'))])
async def exchange_rates(request: Request):
    """The reference rates Faxbot read, your own rates, and how old each may be to compare prices."""
    store = _store(request)
    return await _call(lambda: _view(request, store))


class OwnRate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base: str = Field(min_length=3, max_length=3)
    quote: str = Field(min_length=3, max_length=3)
    rate: str = Field(min_length=1, max_length=32)
    rate_date: str | None = Field(default=None, max_length=10)
    note: str | None = Field(default=None, max_length=500)


class Pair(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base: str = Field(min_length=3, max_length=3)
    quote: str = Field(min_length=3, max_length=3)


class Reference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


@router.put('/own', dependencies=[Depends(require_permission('settings:write'))])
async def set_own_rate(payload: OwnRate, request: Request, identity=Depends(require_identity)):
    """Set your own rate for a currency pair, such as the one on your invoice; it overrides the reference rate."""
    from .exchange import RateError, set_own
    store = _store(request)

    def change():
        who = _who(store.engine, identity)
        try:
            set_own(store.engine, payload.base, payload.quote, payload.rate, rate_date=payload.rate_date,
                    note=payload.note, actor_id=who['id'], actor_name=who['name'])
        except RateError as error:
            raise HTTPException(400, detail=str(error)) from None
        return _view(request, store)
    return await _call(change)


@router.post('/own/clear', dependencies=[Depends(require_permission('settings:write'))])
async def clear_own_rate(payload: Pair, request: Request, identity=Depends(require_identity)):
    """Stop using your own rate for a pair (either direction); the reference rate counts again."""
    from .exchange import RateError, clear_own
    store = _store(request)

    def change():
        who = _who(store.engine, identity)
        try:
            cleared = clear_own(store.engine, payload.base, payload.quote, actor_id=who['id'],
                                actor_name=who['name'])
        except RateError as error:
            raise HTTPException(400, detail=str(error)) from None
        if not cleared:
            raise HTTPException(404, detail=f'You have not set a rate between {payload.base.upper()} and '
                                            f'{payload.quote.upper()}.')
        return _view(request, store)
    return await _call(change)


@router.put('/reference', dependencies=[Depends(require_permission('settings:write'))])
async def set_reference(payload: Reference, request: Request, identity=Depends(require_identity)):
    """Turn reading the ECB reference rates on or off."""
    from .exchange import set_reference as save
    store = _store(request)

    def change():
        who = _who(store.engine, identity)
        save(store.engine, payload.enabled, actor_id=who['id'], actor_name=who['name'])
        return _view(request, store)
    return await _call(change)


@router.post('/refresh', dependencies=[Depends(require_permission('settings:write'))])
async def refresh_rates(request: Request):
    """Read the ECB reference rates now. A failure keeps the rates already stored and says why."""
    from .exchange import refresh
    store = _store(request)

    def run():
        found = refresh(store.engine)
        return {**_view(request, store), 'refreshed': {'outcome': found['outcome'], 'error': found['error'],
                                                       'date': found['day'].isoformat() if found['day'] else None}}
    return await _call(run)
