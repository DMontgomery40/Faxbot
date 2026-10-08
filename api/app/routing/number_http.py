"""Number advice routes: where each number should live (M24), your NPI record (M18 b), the check before a first
fax (M18 c), and prices by where calls start in the US (jurisdiction).

Reads of advice need ``settings:read``; saving an NPI, reading the registry for it and importing a price file
need ``settings:write``. The check before a first fax is for anyone who may send (``check_send``), like the
Send page's other checks, and never stops a fax. Nothing here ports a number, changes caller ID or places a call.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import private_operation, require_identity
from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from .http import _call, _number, _store


router = APIRouter(prefix='/routing', tags=['Number advice'])
MAX_PRICE_FILE = 40 * 1024 * 1024


def _values(request):
    return request.scope['faxbot.configuration'].active.values


def _actor_name(engine, actor):
    import sqlalchemy as sa
    from .database import read_connection
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    with read_connection(engine) as connection:
        return connection.execute(sa.select(principals.c.display_name).where(
            principals.c.id == getattr(actor, 'principal_id', None))).scalar()


@router.get('/recommendations/numbers', dependencies=[Depends(require_permission('settings:read'))])
async def number_placement(request: Request, days: int = Query(default=30, ge=7, le=183)):
    """Where each of your numbers should live: what it costs at each of your accounts, and how to move it (advice)."""
    from .number_placement import placement
    store = _store(request)
    values = _values(request)
    return await _call(lambda: placement(store.engine, values, routes=store, days=days))


@router.get('/recommendations/facts', dependencies=[Depends(require_permission('settings:read'))])
async def fact_advice(request: Request, days: int = Query(default=90, ge=7, le=183)):
    """What one missing fact (a partner, a recipient's approval, a price, a plan's allowance) would have saved, per
    recipient, over the last ``days`` days. Advice only: nothing is enrolled, approved or entered."""
    from .fact_advice import advice
    store = _store(request)
    values = _values(request)
    return await _call(lambda: advice(store.engine, values, routes=store, days=days))


@router.get('/recommendations/sites', dependencies=[Depends(require_permission('settings:read'))])
async def site_recommendations(request: Request, days: int = Query(default=30, ge=7, le=183)):
    """Which site's trunk US calls would cost less from, where a carrier prices calls within a state differently."""
    from .jurisdiction import site_advice
    store = _store(request)
    values = _values(request)
    return await _call(lambda: site_advice(store.engine, values, days=days))


# -- your NPI record ---------------------------------------------------------------------------------------------

class NpiIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    npi: str = Field(min_length=10, max_length=10)
    label: str = Field(default='', max_length=200)


def _npi_errors(operation):
    from .nppes import NppesInputError

    def run():
        try:
            return operation()
        except NppesInputError as error:
            raise HTTPException(400, detail=str(error)) from None
    return run


@router.get('/npi', dependencies=[Depends(require_permission('settings:read'))])
async def read_npi(request: Request):
    """Your NPIs and the numbers each one's newest registry read lists, with when it was read."""
    from .nppes import own_record
    store = _store(request)
    return await _call(lambda: own_record(store.engine))


def _refreshed(engine):
    from .nppes import own_record, refresh_own
    outcome = refresh_own(engine)
    view = own_record(engine)
    if outcome['failed']:
        view['problem'] = 'Faxbot could not reach NPPES just now; your NPI is saved and Faxbot will read it next time.'
    elif outcome['missing']:
        view['problem'] = f"NPPES has no record for NPI {outcome['missing'][0]}; check the number."
    return view


@router.post('/npi', dependencies=[Depends(require_permission('settings:write'))])
async def add_npi(payload: NpiIn, request: Request, identity=Depends(require_identity)):
    """Add one of your NPIs (one per location if you have several), then read its record from NPPES once."""
    from .nppes import NppesStore
    store = _store(request)

    def add():
        NppesStore(store.engine).add(payload.npi, payload.label, by_name=_actor_name(store.engine, identity.actor))
        return _refreshed(store.engine)
    return await _call(_npi_errors(add))


@router.delete('/npi/{npi}', dependencies=[Depends(require_permission('settings:write'))])
async def remove_npi(npi: str, request: Request, identity=Depends(require_identity)):
    """Stop treating an NPI as yours; what was read stays as history."""
    from .nppes import NppesStore, own_record
    store = _store(request)

    def remove():
        NppesStore(store.engine).remove(npi, by_name=_actor_name(store.engine, identity.actor))
        return own_record(store.engine)
    return await _call(_npi_errors(remove))


@router.post('/npi/check', dependencies=[Depends(require_permission('settings:write'))])
async def check_npi(request: Request):
    """Read each of your NPIs from NPPES now and keep what it lists."""
    store = _store(request)
    return await _call(lambda: _refreshed(store.engine))


# -- the check before a first fax ---------------------------------------------------------------------------------

@router.get('/recipient-check')
async def recipient_check(request: Request, to: str = Query(min_length=1, max_length=64),
                          name: str | None = Query(default=None, max_length=200),
                          identity=Depends(require_identity)):
    """Before the first fax to ``to``: whether NPPES lists that number for the provider ``name``. A warning at most;
    it never stops a fax."""
    from .nppes import recipient_check as check
    access = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: access.outbound.check_send(identity.actor)))
    number = _number(to, request)
    store = _store(request)
    values = _values(request)
    return await _call(lambda: check(store.engine, values, number, name=name))


# -- prices by jurisdiction ------------------------------------------------------------------------------------------

@router.post('/jurisdiction-rates', dependencies=[Depends(require_permission('settings:write'))])
async def import_jurisdiction_rates(request: Request, file: UploadFile = File(...),
                                    carrier: str = Form(..., max_length=64),
                                    source_url: str | None = Form(default=None, max_length=512),
                                    read_on: str | None = Form(default=None, max_length=10)):
    """Import a carrier's US prices for calls within one state and between states (a CSV such as AnveoDirect's
    rate file). The carrier's earlier prices are kept as history."""
    from .jurisdiction import JurisdictionInputError, import_rows, parse_rows
    store = _store(request)
    route = carrier.strip().lower()
    route = route if route.startswith('sip-') else f'sip-{route}'
    data = await file.read(MAX_PRICE_FILE + 1)
    if len(data) > MAX_PRICE_FILE:
        raise HTTPException(413, detail='The price file is larger than 40 MB.')
    try:
        captured = datetime.strptime(read_on, '%Y-%m-%d') if read_on else None
    except ValueError:
        raise HTTPException(400, detail='Write the date the prices were read as 2026-10-08.') from None

    def save():
        try:
            rates = parse_rows(data.decode('utf-8-sig', errors='replace'), route, source_url=source_url or None,
                               captured_on=captured)
        except JurisdictionInputError as error:
            raise HTTPException(400, detail=str(error)) from None
        return {'prices': import_rows(store.engine, route, rates)}
    return await _call(save)
