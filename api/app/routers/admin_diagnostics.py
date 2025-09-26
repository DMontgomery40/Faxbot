import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Request
from sse_starlette.sse import EventSourceResponse  # type: ignore

from api.app.main import require_admin  # reuse admin dependency
from api.app.services.events import EventEmitter


router = APIRouter(prefix="/admin/diagnostics", tags=["Diagnostics"], dependencies=[Depends(require_admin)])


@router.get("/events/recent")
async def recent_events(
    request: Request,
    limit: int = 50,
    provider_id: Optional[str] = None,
    event_type: Optional[str] = None,
    from_db: bool = False
):
    """Get recent events with filtering options."""
    emitter: EventEmitter = request.app.state.event_emitter  # type: ignore
    events = await emitter.get_recent_events(
        limit=limit,
        provider_id=provider_id,
        event_type=event_type,
        from_db=from_db
    )
    return {
        "events": [
            {
                "id": e.id,
                "type": e.type.value,
                "occurred_at": e.occurred_at.isoformat(),
                "provider_id": e.provider_id,
                "external_id": e.external_id,
                "job_id": e.job_id,
                "user_id": e.user_id,
                "payload_meta": e.payload_meta,
                "correlation_id": e.correlation_id,
            }
            for e in events
        ],
        "total": len(events),
        "source": "database" if from_db else "memory",
    }


@router.get("/events/sse")
async def events_sse(
    request: Request,
    admin_auth = Depends(require_admin)
):
    """Server-Sent Events stream for real-time event monitoring."""
    emitter: EventEmitter = request.app.state.event_emitter  # type: ignore
    queue = await emitter.add_subscriber()

    async def event_stream():
        try:
            # Send initial keepalive
            yield {"event": "connected", "data": '{"status": "connected"}'}

            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {"event": "event", "data": msg}
                except asyncio.TimeoutError:
                    # Send keepalive every 30 seconds
                    yield {"event": "keepalive", "data": '{"ping": true}'}
        except Exception:
            pass
        finally:
            await emitter.remove_subscriber(queue)

    return EventSourceResponse(event_stream())


@router.get("/events/types")
async def get_event_types():
    """Get available event types for filtering."""
    from api.app.services.events import EventType

    return {
        "event_types": [
            {"value": event_type.value, "label": event_type.value.replace(".", " ").title()}
            for event_type in EventType
        ]
    }

