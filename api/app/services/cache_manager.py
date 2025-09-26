import json
from datetime import datetime, timedelta
from typing import Any, Optional, Dict

try:
    import redis.asyncio as redis  # type: ignore
except Exception:  # pragma: no cover - optional runtime dep
    redis = None  # type: ignore


class CacheManager:
    """Redis-backed cache with a simple local fallback.

    - If REDIS_URL is provided and redis library is available, uses Redis for get/set.
    - Always mirrors values into a local in-process cache with a short TTL so we
      remain resilient if Redis blips.
    """

    def __init__(self, redis_url: Optional[str] = None):
        self.redis_available = False
        self.redis_client = None
        self.local_cache: Dict[str, Any] = {}
        self.local_expiry: Dict[str, datetime] = {}

        if redis_url and redis is not None:
            try:
                self.redis_client = redis.from_url(redis_url)
                self.redis_available = True
            except Exception:
                # Degrade gracefully; local cache only
                self.redis_available = False

    async def get(self, key: str) -> Optional[Any]:
        # Try Redis first
        if self.redis_available and self.redis_client:
            try:
                raw = await self.redis_client.get(key)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass

        # Fallback to local
        exp = self.local_expiry.get(key)
        if exp and exp > datetime.utcnow():
            return self.local_cache.get(key)
        # Expired
        self.local_cache.pop(key, None)
        self.local_expiry.pop(key, None)
        return None

    async def set(self, key: str, value: Any, ttl: int = 300) -> bool:
        # Redis
        if self.redis_available and self.redis_client:
            try:
                await self.redis_client.setex(key, ttl, json.dumps(value, default=str))
            except Exception:
                pass
        # Local mirror with capped TTL (≤ 60s)
        self.local_cache[key] = value
        self.local_expiry[key] = datetime.utcnow() + timedelta(seconds=min(ttl, 60))
        return True

    async def delete(self, key: str) -> bool:
        if self.redis_available and self.redis_client:
            try:
                await self.redis_client.delete(key)
            except Exception:
                pass
        self.local_cache.pop(key, None)
        self.local_expiry.pop(key, None)
        return True

    async def delete_pattern(self, pattern: str) -> int:
        deleted = 0
        # Redis pattern delete (SCAN based)
        if self.redis_available and self.redis_client:
            try:
                async for key in self.redis_client.scan_iter(match=pattern):
                    await self.redis_client.delete(key)
                    deleted += 1
            except Exception:
                pass
        # Local pattern delete
        for key in list(self.local_cache.keys()):
            if _pattern_match(key, pattern):
                self.local_cache.pop(key, None)
                self.local_expiry.pop(key, None)
                deleted += 1
        return deleted

    async def flush_all(self) -> None:
        # Clear local
        self.local_cache.clear()
        self.local_expiry.clear()
        # Best-effort Redis flush for this DB only
        if self.redis_available and self.redis_client:
            try:
                await self.redis_client.flushdb()
            except Exception:
                pass

    async def get_stats(self) -> Dict[str, Any]:
        return {
            "redis_available": bool(self.redis_available),
            "local_size": len(self.local_cache),
        }


def _pattern_match(key: str, pattern: str) -> bool:
    # Very small helper; Redis pattern only used with '*' wildcards in our usage
    if pattern == key:
        return True
    if "*" not in pattern:
        return False
    prefix = pattern.split("*")[0]
    return key.startswith(prefix)

