"""Plans and other carriers: the contract view of each plan, and what other carriers would have cost (B11, B13).

``GET /routing/plans`` answers, for each flat, allowance or committed plan, the
budget or allowance used and left this billing period, the overage so far,
what is committed, a day-by-day burn-down and whose bill a fax falls on when
both sides are your own accounts. The budgets themselves are the setting
``plan_budgets``, saved through ``PUT /admin/settings`` like every setting.

``GET /routing/plans/allocation`` answers, for each plan whose allowance or
normal-use budget is limited, how much is left, which waiting faxes get it,
which go another way and for how much, and what is kept for faxes not sent yet
(``plan_allocation.view``). Every amount is an estimate.

``GET /routing/recommendations/carriers`` prices your last 30 days of faxing at
each carrier's published prices. Advice only: nothing is switched.
"""
from fastapi import APIRouter, Depends, Request

from ..access.route_policy import require_permission
from .background import installation_engine
from .http import _call, _store


router = APIRouter(prefix='/routing', tags=['Delivery routes'])


@router.get('/plans', dependencies=[Depends(require_permission('settings:read'))])
async def plans(request: Request):
    """Each plan's budget or allowance this billing period, what is committed, and the burn-down (estimates)."""
    from .plan_check import contract_report
    store = _store(request)
    values = request.scope['faxbot.configuration'].active.values
    return await _call(lambda: contract_report(store, values))


@router.get('/plans/allocation', dependencies=[Depends(require_permission('settings:read'))])
async def plan_allocation(request: Request):
    """Each limited plan's pages or minutes left, which waiting faxes get them, and what is kept for later."""
    from .plan_allocation import view
    store = _store(request)
    values = request.scope['faxbot.configuration'].active.values
    return await _call(lambda: view(store, values))


@router.get('/recommendations/carriers', dependencies=[Depends(require_permission('settings:read'))])
async def carrier_recommendations(request: Request):
    """What your last 30 days of faxing would have cost at each carrier's published prices. Advice only."""
    from .carrier_compare import compare
    store = _store(request)
    engine, _ = installation_engine(request.app)
    values = request.scope['faxbot.configuration'].active.values
    return await _call(lambda: compare(engine or store.engine, values))
