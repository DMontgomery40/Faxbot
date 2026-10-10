"""An analog line through a gateway over HTTP (``analog.py``): Providers → the trunk page, and the command line.

- ``GET /routing/analog-lines/{account}``: whether the trunk account is an analog line, its local calling area and
  its prices; ``sip`` is the first trunk.
- ``PUT /routing/analog-lines/{account}/local-calls``: import the line's local prefixes from a file you saved
  (its text), and its prices.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from . import analog


router = APIRouter(prefix='/routing/analog-lines', tags=['Delivery routes'])


class LocalCalls(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # The saved file's text: a Local Calling Guide page (HTML or XML), a CSV, or one prefix a line.
    text: str = Field(min_length=1, max_length=analog.MAX_BYTES)
    filename: str | None = Field(default=None, max_length=200)
    # The calling plan your line has, when the file lists several.
    plan: str | None = Field(default=None, max_length=100)
    # Your line's own prefix (303-426) or number, checked against the list.
    line: str | None = Field(default=None, max_length=40)
    # Per minute for calls outside the local area (0 when the plan includes them), and the line's monthly fee.
    toll_per_minute: str | None = Field(default=None, max_length=16)
    monthly_fee: str | None = Field(default=None, max_length=16)
    increment: int = Field(default=60, ge=1, le=3600)
    source_url: str | None = Field(default=None, max_length=512)


def _parts(request):
    from .background import installation_engine
    from .store import RouteStore
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return engine, RouteStore(engine), request.scope['faxbot.configuration'].active.values


@router.get('/{account}', dependencies=[Depends(require_permission('settings:read'))])
async def get_line(account: str, request: Request):
    engine, store, values = _parts(request)
    return await run_lifecycle_step(lambda: analog.line_view(engine, store, values, account.strip()[:32]))


@router.put('/{account}/local-calls', dependencies=[Depends(require_permission('settings:write'))])
async def put_local_calls(account: str, payload: LocalCalls, request: Request):
    engine, store, values = _parts(request)

    def save():
        return analog.import_local_calls(
            engine, store, values, account.strip()[:32], payload.text, filename=payload.filename or '',
            plan=payload.plan, line=payload.line, toll_per_minute=payload.toll_per_minute,
            monthly_fee=payload.monthly_fee, increment=payload.increment, source_url=payload.source_url or None)
    from .costs import InvalidRateCard
    from .database import DeliveryStoreError
    from .store import RoutingInputError
    try:
        view = await run_lifecycle_step(save)
    except (analog.AnalogLineError, InvalidRateCard, RoutingInputError) as error:
        raise HTTPException(400, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Rate cards cannot be saved right now. Try again in a moment.') from None
    from ..audit import audit_event
    audit_event('analog_local_calls_imported', account=view['account'], prefixes=view['local_prefixes'])
    return {**view, 'saved': f"Saved. {view['sentence']}"}
