from __future__ import annotations

import asyncio
import time
import json
import os
from typing import Any, Dict, Optional

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


class CacheManager:
    """Cache manager with Redis primary and in-memory fallback.

    Phase 3 implementation with Redis support for distributed caching
    and automatic fallback to in-memory storage when Redis is unavailable.
    """

    def __init__(self, redis_url: Optional[str] = None) -> None:
        self._store: Dict[str, tuple[Any, Optional[float]]] = {}
        self._lock = asyncio.Lock()
        self.redis_client = None
        self.redis_available = False

        # Try to connect to Redis if available
        if REDIS_AVAILABLE:
            redis_url = redis_url or os.getenv('REDIS_URL', 'redis://localhost:6379/0')
            try:
                self.redis_client = redis.from_url(
                    redis_url,
                    encoding='utf-8',
                    decode_responses=True
                )
                # We'll test connection on first use
                self.redis_available = True
            except Exception as e:
                print(f"[CacheManager] Redis connection failed: {e}")
                self.redis_available = False

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache (Redis first, then memory fallback)."""

        # Try Redis first
        if self.redis_available and self.redis_client:
            try:
                value = await self.redis_client.get(key)
                if value:
                    try:
                        return json.loads(value)
                    except json.JSONDecodeError:
                        return value
            except Exception as e:
                # Redis failed, fall back to memory
                print(f"[CacheManager] Redis get failed, using memory: {e}")
                self.redis_available = False

        # Fall back to in-memory store
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
        """Set value in cache (Redis + memory)."""

        # Try Redis first
        if self.redis_available and self.redis_client:
            try:
                json_value = json.dumps(value) if not isinstance(value, str) else value
                if ttl:
                    await self.redis_client.setex(key, ttl, json_value)
                else:
                    await self.redis_client.set(key, json_value)
            except Exception as e:
                # Redis failed, continue with memory
                print(f"[CacheManager] Redis set failed, using memory: {e}")
                self.redis_available = False

        # Also store in memory (as backup or primary)
        exp = (time.time() + ttl) if ttl else None
        async with self._lock:
            self._store[key] = (value, exp)

    async def delete_pattern(self, pattern: str) -> int:
        """Delete keys matching pattern.
        Returns the count of deleted entries.
        """
        deleted_count = 0

        # Try Redis first
        if self.redis_available and self.redis_client:
            try:
                # Convert pattern to Redis glob pattern
                redis_pattern = f"*{pattern}*" if '*' not in pattern else pattern
                cursor = 0
                while True:
                    cursor, keys = await self.redis_client.scan(
                        cursor, match=redis_pattern, count=100
                    )
                    if keys:
                        deleted_count += await self.redis_client.delete(*keys)
                    if cursor == 0:
                        break
            except Exception as e:
                print(f"[CacheManager] Redis delete_pattern failed: {e}")
                self.redis_available = False

        # Also clear from memory
        async with self._lock:
            keys = [k for k in self._store.keys() if pattern in k]
            for k in keys:
                self._store.pop(k, None)
            deleted_count = max(deleted_count, len(keys))

        return deleted_count

    async def flush_all(self) -> None:
        """Clear all cache entries."""

        # Try Redis first
        if self.redis_available and self.redis_client:
            try:
                await self.redis_client.flushdb()
            except Exception as e:
                print(f"[CacheManager] Redis flush failed: {e}")
                self.redis_available = False

        # Clear memory store
        async with self._lock:
            self._store.clear()

    async def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        stats = {
            "backend": "redis" if self.redis_available else "memory",
            "memory_items": len(self._store),
        }

        # Add Redis stats if available
        if self.redis_available and self.redis_client:
            try:
                info = await self.redis_client.info('stats')
                stats.update({
                    "redis_connected": True,
                    "redis_total_connections": info.get('total_connections_received', 0),
                    "redis_keys": await self.redis_client.dbsize(),
                })
            except Exception:
                stats["redis_connected"] = False

        return stats

