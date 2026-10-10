"""Line inventory routes (N19): your lines matched to carrier discontinuance lists and contract end dates.

Reads need ``settings:read``; importing an inventory or a carrier list needs ``settings:write``. Nothing here
orders, ports or cancels a line, or contacts a carrier.
"""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import require_identity
from ..access.route_policy import require_permission
from .http import _call, _store
from .number_http import _actor_name


router = APIRouter(prefix='/routing', tags=['Line inventory'])
MAX_INVENTORY_FILE = 20 * 1024 * 1024
# AT&T's workbook is about 5 MB; leave room for a carrier's larger list saved as CSV.
MAX_LIST_FILE = 120 * 1024 * 1024


def _values(request):
    return request.scope['faxbot.configuration'].active.values


def _who(engine, identity):
    return {'id': getattr(identity.actor, 'principal_id', None), 'name': _actor_name(engine, identity.actor)}


def _day(text, what):
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise HTTPException(400, detail=f'Write the {what} as 2026-11-04.') from None


async def _read(file, limit, what):
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, detail=f'The {what} is larger than {limit // (1024 * 1024)} MB.')
    return data


@router.get('/line-inventory', dependencies=[Depends(require_permission('settings:read'))])
async def line_inventory(request: Request):
    """Your line inventory, each line's match against the carrier lists you imported, and its dates."""
    from .inventory import view
    store = _store(request)
    values = _values(request)
    return await _call(lambda: view(store.engine, values))


@router.post('/line-inventory/files', dependencies=[Depends(require_permission('settings:write'))])
async def import_line_inventory(request: Request, identity=Depends(require_identity), file: UploadFile = File(...),
                                date_order: str = Form(default='mdy', max_length=3)):
    """Import your line inventory (CSV or Excel). It replaces the current one, which is kept as history; contract
    end dates become each line's contract-end notice."""
    from .inventory import InventoryError, import_inventory, parse_inventory, view
    store = _store(request)
    values = _values(request)
    data = await _read(file, MAX_INVENTORY_FILE, 'inventory')

    def save():
        try:
            parsed = parse_inventory(data, default_country=values.fax_default_country, date_order=date_order)
            import_inventory(store.engine, parsed.items, file_name=file.filename, actor=_who(store.engine, identity))
        except InventoryError as error:
            raise HTTPException(400, detail=str(error)) from None
        return {**view(store.engine, values), 'imported': len(parsed.items), 'skipped': parsed.skipped,
                'skipped_count': parsed.skipped_count}
    return await _call(save)


@router.post('/carrier-lists/files', dependencies=[Depends(require_permission('settings:write'))])
async def import_carrier_list(request: Request, identity=Depends(require_identity), file: UploadFile = File(...),
                              carrier: str | None = Form(default=None, max_length=100),
                              kind: str | None = Form(default=None, max_length=16),
                              source_url: str | None = Form(default=None, max_length=512),
                              file_date: str | None = Form(default=None, max_length=10),
                              date_order: str = Form(default='mdy', max_length=3)):
    """Import a carrier's list of discontinued or grandfathered service areas: AT&T's workbook as published, or any
    carrier's list as CSV. The same carrier's earlier list of that kind is kept as history."""
    from .inventory import InventoryError, import_carrier_list as save_list, parse_carrier_list, view
    store = _store(request)
    values = _values(request)
    data = await _read(file, MAX_LIST_FILE, 'list')
    dated = _day(file_date, "list's date")

    def save():
        try:
            parsed = parse_carrier_list(data, carrier=carrier or None, kind=kind or None,
                                        date_order=date_order if date_order in ('mdy', 'dmy') else 'mdy')
            save_list(store.engine, parsed, source_url=source_url or None, file_date=dated, file_name=file.filename,
                      actor=_who(store.engine, identity))
        except InventoryError as error:
            raise HTTPException(400, detail=str(error)) from None
        return {**view(store.engine, values), 'imported': len(parsed.items), 'skipped': parsed.skipped,
                'skipped_count': parsed.skipped_count, 'layout': parsed.layout}
    return await _call(save)


# -- the POTS-replacement counter-quote (N23) ----------------------------------------------------------------------

class QuoteIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='', max_length=100)
    published: str | None = Field(default=None, max_length=40)
    per_line: str = Field(default='', max_length=32)
    currency: str = Field(default='USD', min_length=3, max_length=3)
    term_months: int | None = Field(default=None, ge=1, le=120)
    lines_quoted: int | None = Field(default=None, ge=1, le=100_000)
    ports_per_device: int | None = Field(default=None, ge=1, le=64)
    device_price: str = Field(default='', max_length=32)
    source_url: str | None = Field(default=None, max_length=512)
    source_date: str | None = Field(default=None, max_length=10)
    note: str = Field(default='', max_length=2000)


class QuoteName(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)


@router.get('/pots-quotes', dependencies=[Depends(require_permission('settings:read'))])
async def pots_quotes(request: Request):
    """Each POTS-replacement quote you entered, with the fax lines it could leave out and one shared trunk instead."""
    from .pots_quote import view
    store = _store(request)
    values = _values(request)
    return await _call(lambda: view(store.engine, values, routes=store))


@router.put('/pots-quotes', dependencies=[Depends(require_permission('settings:write'))])
async def record_pots_quote(payload: QuoteIn, request: Request, identity=Depends(require_identity)):
    """Record a POTS-replacement quote (or start from a published price). Earlier entries stay as history."""
    from .pots_quote import QuoteError, record_published, record_quote, view
    store = _store(request)
    values = _values(request)
    dated = _day(payload.source_date, "quote's date")

    def save():
        try:
            if payload.published:
                record_published(store.engine, payload.published, lines_quoted=payload.lines_quoted,
                                 term_months=payload.term_months, actor=_who(store.engine, identity))
            else:
                record_quote(store.engine, payload.name, per_line=payload.per_line, currency=payload.currency,
                             term_months=payload.term_months, lines_quoted=payload.lines_quoted,
                             ports_per_device=payload.ports_per_device, device_price=payload.device_price or None,
                             source_url=payload.source_url or None, source_date=dated, note=payload.note or None,
                             actor=_who(store.engine, identity))
        except QuoteError as error:
            raise HTTPException(400, detail=str(error)) from None
        return view(store.engine, values, routes=store)
    return await _call(save)


@router.post('/pots-quotes/remove', dependencies=[Depends(require_permission('settings:write'))])
async def remove_pots_quote(payload: QuoteName, request: Request, identity=Depends(require_identity)):
    """Withdraw a POTS-replacement quote; its history stays."""
    from .pots_quote import QuoteError, remove_quote, view
    store = _store(request)
    values = _values(request)

    def save():
        try:
            remove_quote(store.engine, payload.name, actor=_who(store.engine, identity))
        except QuoteError as error:
            raise HTTPException(404, detail=str(error)) from None
        return view(store.engine, values, routes=store)
    return await _call(save)
