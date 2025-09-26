import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class EventType(str, Enum):
    PROVIDER_HEALTH_CHANGED = "provider.health.changed"
    WEBHOOK_RECEIVED = "webhook.received"
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
    def __init__(self) -> None:
        self._events: List[CanonicalEvent] = []
        self._max_events = 200
        self._subscribers: List[asyncio.Queue[str]] = []
        self._lock = asyncio.Lock()

    async def emit_event(self, etype: EventType, **kwargs: Any) -> None:
        ev = CanonicalEvent(
            id=str(kwargs.get("id") or datetime.utcnow().timestamp()),
            type=etype,
            occurred_at=datetime.utcnow(),
            job_id=kwargs.get("job_id"),
            provider_id=kwargs.get("provider_id"),
            external_id=kwargs.get("external_id"),
            user_id=kwargs.get("user_id"),
            payload_meta=kwargs.get("payload_meta") or {},
            correlation_id=kwargs.get("correlation_id"),
        )
        self._events.append(ev)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]
        # SSE broadcast (sanitized json)
        msg = {
            "id": ev.id,
            "type": ev.type.value,
            "occurred_at": ev.occurred_at.isoformat(),
            "provider_id": ev.provider_id,
            "external_id": ev.external_id,
            "job_id": ev.job_id,
            "payload_meta": ev.payload_meta,
        }
        async with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(json_dumps(msg))
                except Exception:
                    pass

    async def get_recent_events(self, limit: int = 50, provider_id: Optional[str] = None) -> List[CanonicalEvent]:
        items = list(self._events)
        if provider_id:
            items = [e for e in items if e.provider_id == provider_id]
        return items[-limit:]

    async def add_subscriber(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def remove_subscriber(self, q: asyncio.Queue[str]) -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, separators=(",", ":"))

