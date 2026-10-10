"""The caller-verification stamp over HTTP (``caller_check.py``): Received, and the trunk page.

- ``GET /caller-check/faxes/{inbound_id}``: the stamp kept with one received fax, for anyone who may read that fax.
- ``GET``/``PUT /caller-check/registered``: your registered senders for received faxes (the audited configuration).
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import private_operation, require_identity
from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from . import caller_check


router = APIRouter(prefix='/caller-check', tags=['Received faxes'])


class Registered(BaseModel):
    model_config = ConfigDict(extra='forbid')
    numbers: list[str] = Field(default_factory=list, max_length=500)


def _view(values):
    numbers = caller_check.registered(values)
    sentence = (f"{len(numbers)} registered {'sender' if len(numbers) == 1 else 'senders'}: received faxes from them "
                'show whether the network verified the caller number.' if numbers else
                'No registered senders yet: received faxes still show whether the network verified the caller '
                'number, when your carrier says.')
    return {'numbers': numbers, 'sentence': sentence}


@router.get('/faxes/{inbound_id}')
async def fax_stamp(inbound_id: str, request: Request, identity=Depends(require_identity)):
    """What Received says about who called, for anyone who may read that fax; nothing when no stamp was kept."""
    runtime = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: runtime.inbound_queries.item(identity.actor, inbound_id)))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', inbound_id):
        raise HTTPException(404, detail='No stamp for this fax.')
    from ..routing.background import installation_engine
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Received faxes are unavailable right now. Try again in a moment.')
    stamp = await run_lifecycle_step(lambda: caller_check.for_fax(engine, inbound_id))
    return {'stamp': stamp}


@router.get('/registered', dependencies=[Depends(require_permission('settings:read'))])
async def get_registered(request: Request):
    return _view(request.scope['faxbot.configuration'].active.values)


@router.put('/registered')
async def put_registered(payload: Registered, request: Request,
                         identity=Depends(require_permission('settings:write'))):
    """Replace the registered senders for received faxes, as an audited configuration change."""
    from ..access.http import runtime as access_runtime
    from ..routing.numbers import InvalidNumber, normalize_number
    from ..routing.background import installation_engine
    expected = request.scope['faxbot.configuration']
    country = getattr(expected.desired.values, 'fax_default_country', 'US') or 'US'
    numbers = []
    for text in payload.numbers:
        if not str(text).strip():
            continue
        try:
            numbers.append(normalize_number(str(text), country=country))
        except InvalidNumber as error:
            raise HTTPException(400, detail=str(error)) from None
    _, runtime = installation_engine(request.app)
    if runtime is None or not getattr(runtime, 'serving', False):
        raise HTTPException(503, detail='Installation configuration is not ready.')

    def save():
        access = access_runtime(request)
        access.configuration_access.prepare_settings_write(identity.actor, expected, expected.desired.id)
        runtime.manager.patch_authorized(expected, {caller_check.SETTING: caller_check.encode(numbers)},
                                         principal=identity.actor, control=access.control)
        return runtime.manager.store.read().desired.values
    values = await run_lifecycle_step(save)
    return {**_view(values), 'saved': 'Saved. ' + _view(values)['sentence']}
