from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Request
from sse_starlette.sse import EventSourceResponse  # type: ignore

from api.app.main import require_admin  # reuse admin dependency
from api.app.services.events import EventEmitter


router = APIRouter(prefix="/admin/diagnostics", tags=["Diagnostics"], dependencies=[Depends(require_admin)])


@router.get("/events/recent")
async def recent_events(request: Request, limit: int = 50, provider_id: Optional[str] = None):
    emitter: EventEmitter = request.app.state.event_emitter  # type: ignore
    events = await emitter.get_recent_events(limit=limit, provider_id=provider_id)
    return {
        "events": [
            {
                "id": e.id,
                "type": e.type.value,
                "occurred_at": e.occurred_at.isoformat(),
                "provider_id": e.provider_id,
                "external_id": e.external_id,
                "job_id": e.job_id,
                "payload_meta": e.payload_meta,
            }
            for e in events
        ],
        "total": len(events),
    }


@router.get("/events/sse")
async def events_sse(request: Request):
    emitter: EventEmitter = request.app.state.event_emitter  # type: ignore
    queue = await emitter.add_subscriber()

    async def event_stream():
        try:
            while True:
                msg = await queue.get()
                yield {"event": "event", "data": msg}
        except Exception:
            pass
        finally:
            await emitter.remove_subscriber(queue)

    return EventSourceResponse(event_stream())

