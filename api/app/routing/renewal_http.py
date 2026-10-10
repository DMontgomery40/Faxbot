"""Fax server renewal routes (N20, N24): channels at measured peak and the renewal page.

Reads need ``settings:read``; importing call records or routing and recording a renewal need ``settings:write``.
Nothing here changes a licence, cancels a renewal or contacts a vendor.
"""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import require_identity
from ..access.route_policy import require_permission
from .http import _call, _store
from .inventory_http import _read, _who


router = APIRouter(prefix='/routing', tags=['Fax server renewal'])
MAX_CALL_FILE = 200 * 1024 * 1024
MAX_ROUTES_FILE = 20 * 1024 * 1024


def _values(request):
    return request.scope['faxbot.configuration'].active.values


def _day(text, what):
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise HTTPException(400, detail=f'Write the {what} as 2027-05-31.') from None


@router.get('/channels', dependencies=[Depends(require_permission('settings:read'))])
async def channels(request: Request):
    """Calls at once at their peak, for Faxbot's own trunk and each fax server whose call records you imported."""
    from .channel_peak import view
    store = _store(request)
    values = _values(request)
    return await _call(lambda: view(store.engine, values))


@router.post('/channels/files', dependencies=[Depends(require_permission('settings:write'))])
async def import_channel_calls(request: Request, identity=Depends(require_identity), file: UploadFile = File(...),
                               system: str = Form(..., min_length=1, max_length=100),
                               source_format: str | None = Form(default=None, max_length=16),
                               licensed: int | None = Form(default=None, ge=1, le=10_000),
                               time_zone: str | None = Form(default=None, max_length=64),
                               date_order: str = Form(default='mdy', max_length=3),
                               numbers: str | None = Form(default=None, max_length=20_000)):
    """Import another fax server's or phone system's call records for one system."""
    from .channel_peak import CallFileError, import_calls, parse_calls, view
    store = _store(request)
    values = _values(request)
    data = await _read(file, MAX_CALL_FILE, 'file')
    zone = time_zone or getattr(values, 'time_zone', '') or None

    def save():
        try:
            parsed = parse_calls(data, source_format=source_format or None, time_zone=zone,
                                 date_order=date_order if date_order in ('mdy', 'dmy') else 'mdy',
                                 numbers=[item for item in (numbers or '').replace('\n', ',').split(',')
                                          if item.strip()])
            import_calls(store.engine, parsed, system=system, data=data, file_name=file.filename, licensed=licensed,
                         time_zone=zone, actor=_who(store.engine, identity))
        except CallFileError as error:
            raise HTTPException(400, detail=str(error)) from None
        return {**view(store.engine, values), 'imported': len(parsed.calls), 'format': parsed.source_format,
                'skipped': parsed.skipped, 'skipped_count': parsed.skipped_count}
    return await _call(save)


@router.delete('/channels/imports/{import_id}', dependencies=[Depends(require_permission('settings:write'))])
async def remove_channel_import(import_id: str, request: Request, identity=Depends(require_identity)):
    """Leave one imported file of call records out of the report; its calls stay as history."""
    from .channel_peak import CallFileError, remove_import, view
    store = _store(request)
    values = _values(request)

    def save():
        try:
            remove_import(store.engine, import_id, actor=_who(store.engine, identity))
        except CallFileError as error:
            raise HTTPException(404, detail=str(error)) from None
        return view(store.engine, values)
    return await _call(save)


@router.get('/renewals', dependencies=[Depends(require_permission('settings:read'))])
async def renewals(request: Request):
    """One page per fax server renewal: amount and date, channels at peak, the parallel run, what is left to move."""
    from .renewal import view
    store = _store(request)
    values = _values(request)
    return await _call(lambda: view(store.engine, values))


class RenewalIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    system: str = Field(min_length=1, max_length=100)
    renews_on: str = Field(min_length=10, max_length=10)
    amount: str = Field(min_length=1, max_length=32)
    currency: str = Field(default='USD', min_length=3, max_length=3)
    product: str = Field(default='', max_length=100)
    licensed_channels: int | None = Field(default=None, ge=1, le=10_000)
    source_url: str | None = Field(default=None, max_length=512)
    note: str = Field(default='', max_length=2000)
    parallel_numbers: list[str] = Field(default_factory=list, max_length=500)
    parallel_since: str | None = Field(default=None, max_length=10)


class SystemIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    system: str = Field(min_length=1, max_length=100)


@router.put('/renewals', dependencies=[Depends(require_permission('settings:write'))])
async def record_renewal(payload: RenewalIn, request: Request, identity=Depends(require_identity)):
    """Record a fax server's renewal and the numbers Faxbot runs beside it. Earlier entries stay as history."""
    from .renewal import RenewalError, record_renewal as save_renewal, view
    store = _store(request)
    values = _values(request)
    renews, since = _day(payload.renews_on, 'renewal date'), _day(payload.parallel_since, 'start of the run')

    def save():
        try:
            save_renewal(store.engine, payload.system, renews_on=renews, amount=payload.amount,
                         currency=payload.currency, product=payload.product or None,
                         licensed_channels=payload.licensed_channels, source_url=payload.source_url or None,
                         note=payload.note or None, parallel_numbers=payload.parallel_numbers, parallel_since=since,
                         default_country=values.fax_default_country, actor=_who(store.engine, identity))
        except RenewalError as error:
            raise HTTPException(400, detail=str(error)) from None
        return view(store.engine, values)
    return await _call(save)


@router.post('/renewals/remove', dependencies=[Depends(require_permission('settings:write'))])
async def remove_renewal(payload: SystemIn, request: Request, identity=Depends(require_identity)):
    """Withdraw a fax server's renewal; its history stays."""
    from .renewal import RenewalError, remove_renewal as withdraw, view
    store = _store(request)
    values = _values(request)

    def save():
        try:
            withdraw(store.engine, payload.system, actor=_who(store.engine, identity))
        except RenewalError as error:
            raise HTTPException(404, detail=str(error)) from None
        return view(store.engine, values)
    return await _call(save)


@router.post('/renewals/routes', dependencies=[Depends(require_permission('settings:write'))])
async def import_routes(request: Request, identity=Depends(require_identity), file: UploadFile = File(...),
                        system: str = Form(..., min_length=1, max_length=100)):
    """Import a fax server's number-to-user routing (number, user, email, cover sheet); it replaces the system's
    earlier routing, which is kept as history."""
    from .renewal import RenewalError, import_routes as save_routes, parse_routes, view
    store = _store(request)
    values = _values(request)
    data = await _read(file, MAX_ROUTES_FILE, 'file')

    def save():
        try:
            routes, skipped = parse_routes(data, default_country=values.fax_default_country)
            save_routes(store.engine, system, routes, file_name=file.filename, actor=_who(store.engine, identity))
        except RenewalError as error:
            raise HTTPException(400, detail=str(error)) from None
        return {**view(store.engine, values), 'imported': len(routes), 'skipped': skipped[:20],
                'skipped_count': len(skipped)}
    return await _call(save)
