"""Dense pages over HTTP: one number's page settings and what its machine takes, and long pages per route."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool
import sqlalchemy as sa

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
    # Lighten shaded areas for this recipient (pages/friendly.py): always, never, or None for the setting all
    # faxes use. Left out keeps the current choice.
    shading: Optional[Literal['always', 'never']] = None


def _recipient(engine, target, request):
    """Recipients, Details: dense pages' view and this recipient's choice for lightening shaded areas."""
    from . import friendly
    from .views import recipient_view
    values = request.scope['faxbot.configuration'].active.values
    return {**recipient_view(engine, target), **friendly.recipient_view(engine, target, values)}


class RoutePages(BaseModel):
    model_config = ConfigDict(extra='forbid')
    long_pages: Optional[StrictBool] = None
    trim_blank: Optional[StrictBool] = None


@router.get('/routing/destinations/{number}/pages', dependencies=[Depends(require_permission('settings:read'))])
async def get_recipient_pages(number: str, request: Request):
    """Recipients, Details: how long a page this number's machine takes, and its page settings."""
    from .capability import PageRecordError
    target = _number(number, request)
    try:
        return await run_lifecycle_step(lambda: _recipient(_engine(request), target, request))
    except PageRecordError:
        raise HTTPException(503, detail='Page settings are unavailable. Try again.') from None


@router.put('/routing/destinations/{number}/pages')
async def put_recipient_pages(number: str, payload: RecipientPages, request: Request,
                              identity=Depends(require_permission('settings:write'))):
    from . import friendly
    from .capability import PageRecordError, records_for
    target = _number(number, request)
    changes = {name: getattr(payload, name) for name in payload.model_fields_set}
    if 'packing' in changes and changes['packing'] is None:
        raise HTTPException(400, detail="Choose 'As the receiving machine allows' or 'Never'.")

    def save():
        engine = _engine(request)
        pages = {name: value for name, value in changes.items() if name != 'shading'}
        if pages:
            records_for(engine).set_recipient_settings(target, actor=_actor(identity), **pages)
        if 'shading' in changes:
            friendly.set_recipient_choice(engine, target, changes['shading'], actor=_actor(identity))
        return _recipient(engine, target, request)
    try:
        result = await run_lifecycle_step(save)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from None
    except sa.exc.SQLAlchemyError:
        raise HTTPException(503, detail='Page settings could not be saved. Try again.') from None
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


@router.get('/routing/recommendations/fax-friendly', dependencies=[Depends(require_permission('settings:read'))])
async def fax_friendly_recommendation(request: Request):
    """Costs, Recommendations: whether lightening shaded areas and removing specks would have saved time on your
    recent faxes (pages/friendly.py), or what it saved when it is on."""
    from . import friendly
    configuration = request.scope['faxbot.configuration'].active

    def build():
        from ..routing.background import installation_engine
        engine, _ = installation_engine(request.app)
        if engine is None:
            raise HTTPException(503, detail='Installation configuration is not ready.')
        values = configuration.values
        # Every route's pages can be lightened now, a provider that fetches the document included (it fetches
        # the attempt's own pages, pages/sending.fetched_pdf), so how the route sends changes nothing here.
        return friendly.recommendation(engine, values.fax_data_dir, choice=friendly.documents_choice(values))
    try:
        return await run_lifecycle_step(build)
    except sa.exc.SQLAlchemyError:
        raise HTTPException(503, detail='Recommendations are unavailable. Try again.') from None
