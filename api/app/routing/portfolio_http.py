"""Stateless portfolio calculations for administrators; no enrollment or routing changes."""
from fastapi import APIRouter, Depends

from ..access.route_policy import require_permission
from .portfolio import PortfolioInput, plan


router = APIRouter(prefix='/routing', tags=['Delivery routes'])


@router.post('/portfolio/plan', dependencies=[Depends(require_permission('settings:read'))])
def portfolio_plan(body: PortfolioInput):
    """Compare explicit setup bundles under one budget and two declared benefit scenarios. Inputs are not saved."""
    return plan(body)
