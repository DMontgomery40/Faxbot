"""Dense pages over HTTP: one number's page settings and what its machine takes, and long pages per route."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step

router = APIRouter()


def _engine(request):
    from ..routing.background import installation_engine
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return engine


def _number(value, request):
    from ..routing.numbers import InvalidNumber, normalize_number
    country = request.scope['faxbot.configuration'].active.values.fax_default_country
    try:
        return normalize_number(value, country=country)
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


def _actor(identity):
    return str(getattr(getattr(identity, 'actor', None), 'principal_id', None) or 'settings')


class RecipientPages(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # As the receiving machine allows ('allow') or never; left out keeps the current choice.
    packing: Optional[Literal['allow', 'never']] = None
    # Leave out blank page bottoms for a machine without error correction: on, off, or None for the
    # installation's setting. Left out keeps the current choice.
    trim_blank: Optional[StrictBool] = None


class RoutePages(BaseModel):
    model_config = ConfigDict(extra='forbid')
    long_pages: Optional[StrictBool] = None
    trim_blank: Optional[StrictBool] = None


@router.get('/routing/destinations/{number}/pages', dependencies=[Depends(require_permission('settings:read'))])
async def get_recipient_pages(number: str, request: Request):
    """Recipients, Details: how long a page this number's machine takes, and its page settings."""
    from .capability import PageRecordError
    from .views import recipient_view
    target = _number(number, request)
    try:
        return await run_lifecycle_step(lambda: recipient_view(_engine(request), target))
    except PageRecordError:
        raise HTTPException(503, detail='Page settings are unavailable. Try again.') from None


@router.put('/routing/destinations/{number}/pages')
async def put_recipient_pages(number: str, payload: RecipientPages, request: Request,
                              identity=Depends(require_permission('settings:write'))):
    from .capability import PageRecordError, records_for
    from .views import recipient_view
    target = _number(number, request)
    changes = {name: getattr(payload, name) for name in payload.model_fields_set}
    if 'packing' in changes and changes['packing'] is None:
        raise HTTPException(400, detail="Choose 'As the receiving machine allows' or 'Never'.")

    def save():
        engine = _engine(request)
        records_for(engine).set_recipient_settings(target, actor=_actor(identity), **changes)
        return recipient_view(engine, target)
    try:
        result = await run_lifecycle_step(save)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from None
    except PageRecordError:
        raise HTTPException(503, detail='Page settings could not be saved. Try again.') from None
    from ..audit import audit_event
    audit_event('recipient_page_settings', number=target, **{name: changes[name] for name in sorted(changes)})
    return result


@router.get('/routing/page-routes', dependencies=[Depends(require_permission('settings:read'))])
async def get_route_pages(request: Request):
    """Providers: long pages for each route, and the installation's setting for blank page bottoms."""
    from .capability import PageRecordError
    from .views import route_views
    try:
        return {'routes': await run_lifecycle_step(lambda: route_views(_engine(request)))}
    except PageRecordError:
        raise HTTPException(503, detail='Page settings are unavailable. Try again.') from None


@router.put('/routing/page-routes/{route}')
async def put_route_pages(route: str, payload: RoutePages, request: Request,
                          identity=Depends(require_permission('settings:write'))):
    from .capability import PageRecordError, page_models, records_for
    from .views import route_views
    if route not in page_models():
        raise HTTPException(404, detail='Choose one of your delivery routes.')
    changes = {name: getattr(payload, name) for name in payload.model_fields_set}

    def save():
        engine = _engine(request)
        records_for(engine).set_route_settings(route, actor=_actor(identity), **changes)
        return route_views(engine, [route])[0]
    try:
        result = await run_lifecycle_step(save)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from None
    except PageRecordError:
        raise HTTPException(503, detail='Page settings could not be saved. Try again.') from None
    from ..audit import audit_event
    audit_event('route_page_settings', route=route, **{name: changes[name] for name in sorted(changes)})
    return result
