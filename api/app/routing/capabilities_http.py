"""Savings & optimization → Capabilities: ``GET /routing/capabilities``.

Every mechanism in the catalogue, grouped by the outcome it serves, each with whether it is on, works here and is
proven, what it did here, what it needs, and where its setting and figures are (``routing/capabilities.py``). The
console's Capabilities page and Overview, and ``faxbot savings capabilities``, read it. It never carries money, and
like the savings map it needs settings:read.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from .background import installation_engine
from .database import DeliveryStoreError
from .store import RouteStore


router = APIRouter(prefix='/routing', tags=['Delivery routes'])


@router.get('/capabilities', dependencies=[Depends(require_permission('settings:read'))])
async def capabilities(request: Request, findings: bool = Query(default=True)):
    """Every capability, grouped by outcome, with its state here, prerequisites and links; never money.

    ``findings=false`` (the Overview's read) leaves out how many suggestions each piece of advice has and what charge
    checks found, which the Overview never shows and which would read every advice section.
    """
    from .capabilities import evaluate
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    values = request.scope['faxbot.configuration'].active.values
    try:
        routes = RouteStore(engine)
        return await run_lifecycle_step(lambda: evaluate(values, routes, engine, findings=findings))
    except DeliveryStoreError:
        raise HTTPException(503, detail='Delivery route storage is unavailable.') from None
