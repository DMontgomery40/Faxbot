"""Continuations over HTTP: what a broken fax may send again, and sending only its remaining pages.

Both routes authenticate here and check ``fax:read`` on the fax inside the
service. Sending also needs the person to be the fax's sender or owner, or to
hold ``fax:reconcile`` on it, and their own ``fax:send``: the continuation is
accepted as any fax is (``routing/submit.accept_generated_fax``), so the
sending rules decide its route and may hold it. Nothing here runs by itself;
there is no background task.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from ..access.http import require_identity
from ..config_runtime import run_lifecycle_step
from .background import installation_engine
from .continuation import ContinuationError, ContinuationService, ContinuationStore
from .database import DeliveryStoreError


UNAVAILABLE = 'Sending the remaining pages of a fax is temporarily unavailable.'
router = APIRouter(prefix='/continuations', tags=['Continuations'])


def continuation_service(request):
    engine, runtime = installation_engine(request.app)
    if engine is None or getattr(request.app.state, 'access_runtime', None) is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    store = getattr(request.app.state, 'continuation_store', None)
    if store is None or store.engine is not engine:
        try:
            store = ContinuationStore(engine)
        except DeliveryStoreError:
            raise HTTPException(503, detail=UNAVAILABLE) from None
        request.app.state.continuation_store = store
    snapshot = request.scope.get('faxbot.configuration')
    values = ((lambda: snapshot.active.values) if snapshot is not None
              else (lambda: runtime.manager.store.read().active.values))
    return ContinuationService(store, request.app.state.access_runtime, values=values)


async def _call(operation):
    from ..work.certainty import CertaintyError
    try:
        return await run_lifecycle_step(operation)
    except (ContinuationError, CertaintyError) as error:
        raise HTTPException(error.status, detail=error.message) from None
    except (DeliveryStoreError, sa.exc.SQLAlchemyError):
        raise HTTPException(503, detail=UNAVAILABLE) from None
    except RuntimeError as error:  # outbound delivery refused the fax, or it is being queued: a plain sentence
        raise HTTPException(409, detail=str(error)) from None


class ContinueIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    first_page: int = Field(ge=2, le=10_000)
    reason: str | None = Field(default=None, max_length=400)
    # The uncertain item's version, when the fax waits to be settled: sending its remaining pages settles it.
    version: int | None = Field(default=None, ge=1)


@router.get('/faxes/{fax_id}', summary='Whether a fax that broke part way can send only its remaining pages')
async def for_fax(fax_id: str, request: Request, identity=Depends(require_identity)):
    service = continuation_service(request)
    return await _call(lambda: service.view(identity.actor, fax_id))


@router.post('/faxes/{fax_id}', summary='Send only the remaining pages of a fax that broke part way, as a new fax')
async def send_rest(fax_id: str, body: ContinueIn, request: Request, identity=Depends(require_identity)):
    """A person's request; the fax ID of the new fax comes from the broken call, so a repeat sends nothing more.

    A fax waiting to be settled is settled as not delivered with the new fax (its ``reason`` and ``version``
    are needed then, as for settling).
    """
    from ..work.certainty_http import certainty_service, sender
    service = continuation_service(request)
    send = sender(request, identity.actor)
    current = await _call(lambda: service.view(identity.actor, fax_id, with_cost=False))
    item_id = (current.get('offer') or {}).get('open_item_id')
    item = None
    if item_id:
        if body.version is None:
            raise HTTPException(409, detail='This fax is waiting to be settled; reload it and send its remaining '
                                            'pages from there.')
        certainty = certainty_service(request)
        item = await _call(lambda: certainty.settle(identity.actor, item_id, outcome='not_delivered',
                                                    reason=body.reason or '', version=body.version, send=send,
                                                    continue_from=body.first_page))
        view = await _call(lambda: service.view(identity.actor, fax_id, with_cost=False))
    else:
        view = await _call(lambda: service.send(identity.actor, fax_id, first_page=body.first_page,
                                                reason=body.reason, send=send))
    return {**view, 'item': item}
