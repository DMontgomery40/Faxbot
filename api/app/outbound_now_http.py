"""``GET /outbound/now``: the live outbound board (``outbound_now.py``) for Overview, Sent and ``faxbot status``.

Anyone signed in may ask; each person sees only the faxes Sent lists for them, and the counts cover only those.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import outbound_now
from .access.http import private_operation, require_identity
from .config_runtime import run_lifecycle_step


router = APIRouter(tags=['Faxes'])
UNAVAILABLE = 'The outbound board is unavailable right now. Try again in a moment.'


@router.get('/outbound/now')
async def outbound_board(request: Request, identity=Depends(require_identity)):
    """What is sending now, what waits and why, what needs you, how today went, and how full your lines are."""
    from .main import mask_phone
    from .routing.background import installation_engine
    engine, _ = installation_engine(request.app)
    access = getattr(request.app.state, 'access_runtime', None)
    if engine is None or access is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    values = request.scope['faxbot.configuration'].active.values
    return await run_lifecycle_step(private_operation(
        lambda: outbound_now.board_for(engine, access, identity.actor, values, mask=mask_phone)))
