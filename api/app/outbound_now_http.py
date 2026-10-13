"""``GET /outbound/now``: the live outbound board (``outbound_now.py``) for Overview, Sent and ``faxbot status``.

Anyone signed in may ask; each person sees only the faxes Sent lists for them, and the counts cover only those.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from . import outbound_now
from .access.http import private_operation, require_identity
from .config_runtime import run_lifecycle_step


router = APIRouter(tags=['Faxes'])
UNAVAILABLE = "Outgoing faxes can't be shown right now. Try again in a moment."
BUSY = "Faxbot is busy right now, so outgoing faxes can't be shown. Try again in a moment."
# Seconds the console waits before asking again after a 503 (its poll backs off on 503 and 429).
RETRY_SECONDS = 2


@router.get('/outbound/now')
async def outbound_board(request: Request, identity=Depends(require_identity)):
    """What is sending now, what waits and why, what needs you, how today went, and how full your lines are."""
    from .main import mask_phone
    from .routing.background import installation_engine
    engine, _ = installation_engine(request.app)
    access = getattr(request.app.state, 'access_runtime', None)
    if engine is None or access is None:
        raise HTTPException(503, detail=UNAVAILABLE, headers={'Retry-After': str(RETRY_SECONDS)})
    values = request.scope['faxbot.configuration'].active.values

    def read():
        try:
            return outbound_now.board_for(engine, access, identity.actor, values, mask=mask_phone)
        except outbound_now.DatabaseBusy:
            return None
    # A busy access check answers 503 with Retry-After through the access error handler, and a throttled sign-in
    # 429 with Retry-After; a database too busy to read any fax answers the same way here.
    board = await run_lifecycle_step(private_operation(read))
    if board is None:
        raise HTTPException(503, detail=BUSY, headers={'Retry-After': str(RETRY_SECONDS)})
    return board
