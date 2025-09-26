from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional


class CacheManager:
    """Lightweight cache manager with in-memory storage.

    Phase 3 prefers Redis, but this fallback keeps semantics consistent
    so v4 endpoints can operate without external services during dev.
    """

    def __init__(self) -> None:
        self._store: Dict[str, tuple[Any, Optional[float]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            rec = self._store.get(key)
            if not rec:
                return None
            val, exp = rec
            if exp is not None and exp < time.time():
                self._store.pop(key, None)
                return None
            return val

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        exp = (time.time() + ttl) if ttl else None
        async with self._lock:
            self._store[key] = (value, exp)

    async def delete_pattern(self, pattern: str) -> int:
        """Delete keys that contain the substring pattern.
        Returns the count of deleted entries.
        """
        async with self._lock:
            keys = [k for k in self._store.keys() if pattern in k]
            for k in keys:
                self._store.pop(k, None)
            return len(keys)

    async def flush_all(self) -> None:
        async with self._lock:
            self._store.clear()

    async def get_stats(self) -> Dict[str, Any]:
        async with self._lock:
            return {
                "backend": "memory",
                "items": len(self._store),
            }

