import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from api.app.database.async_db import AsyncSessionLocal
from api.app.models.events import CanonicalEventDB


class EventType(str, Enum):
    # Provider health events
    PROVIDER_HEALTH_CHANGED = "provider.health.changed"
    PROVIDER_ENABLED = "provider.enabled"
    PROVIDER_DISABLED = "provider.disabled"

    # Fax lifecycle events
    FAX_QUEUED = "fax.queued"
    FAX_SENT = "fax.sent"
    FAX_DELIVERED = "fax.delivered"
    FAX_FAILED = "fax.failed"
    FAX_RETRYING = "fax.retrying"

    # Webhook events
    WEBHOOK_RECEIVED = "webhook.received"
    WEBHOOK_VERIFIED = "webhook.verified"
    WEBHOOK_FAILED = "webhook.failed"

    # Configuration events
    CONFIG_CHANGED = "config.changed"

    # System events
    JOB_STATUS_CHANGED = "job.status.changed"


@dataclass
class CanonicalEvent:
    id: str
    type: EventType
    occurred_at: datetime
    job_id: Optional[str] = None
    provider_id: Optional[str] = None
    external_id: Optional[str] = None
    user_id: Optional[str] = None
    payload_meta: Dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None


class EventEmitter:
    """Enhanced event emitter with database persistence and SSE streaming."""

    def __init__(self) -> None:
        self._events: List[CanonicalEvent] = []
        self._max_events = 200
        self._subscribers: List[asyncio.Queue[str]] = []
        self._lock = asyncio.Lock()
        self._db_enabled = True

    async def emit_event(self, etype: EventType, **kwargs: Any) -> None:
        """Emit event with database persistence and SSE broadcasting."""
        event_id = kwargs.get("id") or str(uuid.uuid4())
        occurred_at = datetime.utcnow()

        ev = CanonicalEvent(
            id=event_id,
            type=etype,
            occurred_at=occurred_at,
            job_id=kwargs.get("job_id"),
            provider_id=kwargs.get("provider_id"),
            external_id=kwargs.get("external_id"),
            user_id=kwargs.get("user_id"),
            payload_meta=kwargs.get("payload_meta") or {},
            correlation_id=kwargs.get("correlation_id"),
        )

        # Store in memory for SSE
        self._events.append(ev)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]

        # Persist to database (fire-and-forget)
        if self._db_enabled:
            asyncio.create_task(self._persist_event(ev))

        # SSE broadcast (sanitized json)
        msg = {
            "id": ev.id,
            "type": ev.type.value,
            "occurred_at": ev.occurred_at.isoformat(),
            "provider_id": ev.provider_id,
            "external_id": ev.external_id,
            "job_id": ev.job_id,
            "payload_meta": ev.payload_meta,
            "correlation_id": ev.correlation_id,
        }

        async with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(json_dumps(msg))
                except Exception:
                    # Remove failed subscribers
                    try:
                        self._subscribers.remove(q)
                    except ValueError:
                        pass

    async def _persist_event(self, event: CanonicalEvent) -> None:
        """Persist event to database (non-blocking)."""
        try:
            async with AsyncSessionLocal() as session:
                db_event = CanonicalEventDB(
                    id=event.id,
                    type=event.type.value,
                    occurred_at=event.occurred_at,
                    job_id=event.job_id,
                    provider_id=event.provider_id,
                    external_id=event.external_id,
                    user_id=event.user_id,
                    correlation_id=event.correlation_id,
                    payload_meta=event.payload_meta,
                )
                session.add(db_event)
                await session.commit()
        except Exception:
            # Silently fail to avoid disrupting main flow
            # In production, this should be logged
            pass

    async def get_recent_events(
        self,
        limit: int = 50,
        provider_id: Optional[str] = None,
        event_type: Optional[str] = None,
        from_db: bool = False
    ) -> List[CanonicalEvent]:
        """Get recent events from memory or database."""
        if from_db and self._db_enabled:
            return await self._get_events_from_db(limit, provider_id, event_type)

        # Get from memory
        items = list(self._events)
        if provider_id:
            items = [e for e in items if e.provider_id == provider_id]
        if event_type:
            items = [e for e in items if e.type.value == event_type]
        return items[-limit:]

    async def _get_events_from_db(
        self,
        limit: int,
        provider_id: Optional[str] = None,
        event_type: Optional[str] = None
    ) -> List[CanonicalEvent]:
        """Get events from database with filters."""
        try:
            async with AsyncSessionLocal() as session:
                from sqlalchemy import select, desc

                query = select(CanonicalEventDB).order_by(desc(CanonicalEventDB.occurred_at))

                if provider_id:
                    query = query.where(CanonicalEventDB.provider_id == provider_id)
                if event_type:
                    query = query.where(CanonicalEventDB.type == event_type)

                query = query.limit(limit)

                result = await session.execute(query)
                db_events = result.scalars().all()

                # Convert to CanonicalEvent objects
                return [
                    CanonicalEvent(
                        id=db_event.id,
                        type=EventType(db_event.type),
                        occurred_at=db_event.occurred_at,
                        job_id=db_event.job_id,
                        provider_id=db_event.provider_id,
                        external_id=db_event.external_id,
                        user_id=db_event.user_id,
                        payload_meta=db_event.payload_meta or {},
                        correlation_id=db_event.correlation_id,
                    )
                    for db_event in reversed(db_events)
                ]
        except Exception:
            # Fall back to memory events
            return await self.get_recent_events(limit, provider_id, event_type, from_db=False)

    async def add_subscriber(self) -> asyncio.Queue[str]:
        """Add SSE subscriber queue."""
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def remove_subscriber(self, q: asyncio.Queue[str]) -> None:
        """Remove SSE subscriber queue."""
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def disable_db_persistence(self) -> None:
        """Disable database persistence (testing/fallback)."""
        self._db_enabled = False


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, separators=(",", ":"))

