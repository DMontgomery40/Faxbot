"""
Redis cache manager with fallback to in-memory cache.

Provides unified caching interface for hierarchical configuration with
Redis backend and local memory fallback for resilience.
"""

import json
import time
from typing import Any, Dict, Optional, Union
import asyncio


class CacheManager:
    """
    Unified cache manager with Redis primary and in-memory fallback.

    Handles cache operations for hierarchical configuration system with
    automatic fallback when Redis is unavailable.
    """

    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url
        self.redis_client = None
        self.local_cache: Dict[str, Dict[str, Any]] = {}
        self.stats = {
            "hits": 0,
            "misses": 0,
            "errors": 0,
            "redis_available": False,
        }

        if redis_url:
            self._init_redis()

    def _init_redis(self) -> None:
        """Initialize Redis connection if available."""
        try:
            import redis.asyncio as redis
            self.redis_client = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
            self.stats["redis_available"] = True
        except ImportError:
            # Redis not available
            self.redis_client = None
            self.stats["redis_available"] = False
        except Exception:
            # Redis connection failed
            self.redis_client = None
            self.stats["redis_available"] = False

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache (Redis first, then local fallback)."""
        try:
            # Try Redis first
            if self.redis_client and self.stats["redis_available"]:
                try:
                    value = await self.redis_client.get(key)
                    if value is not None:
                        self.stats["hits"] += 1
                        return json.loads(value)
                except Exception:
                    self.stats["errors"] += 1
                    self.stats["redis_available"] = False

            # Fallback to local cache
            if key in self.local_cache:
                entry = self.local_cache[key]
                if entry["expires_at"] > time.time():
                    self.stats["hits"] += 1
                    return entry["value"]
                else:
                    # Expired
                    del self.local_cache[key]

            self.stats["misses"] += 1
            return None

        except Exception:
            self.stats["errors"] += 1
            return None

    async def set(self, key: str, value: Any, ttl: int = 300) -> bool:
        """Set value in cache with TTL (Redis first, local as backup)."""
        try:
            json_value = json.dumps(value)

            # Try Redis first
            if self.redis_client and self.stats["redis_available"]:
                try:
                    await self.redis_client.setex(key, ttl, json_value)
                except Exception:
                    self.stats["errors"] += 1
                    self.stats["redis_available"] = False

            # Always store in local cache as backup
            self.local_cache[key] = {
                "value": value,
                "expires_at": time.time() + ttl,
            }

            return True

        except Exception:
            self.stats["errors"] += 1
            return False

    async def delete(self, key: str) -> bool:
        """Delete key from both Redis and local cache."""
        try:
            success = False

            # Try Redis first
            if self.redis_client and self.stats["redis_available"]:
                try:
                    await self.redis_client.delete(key)
                    success = True
                except Exception:
                    self.stats["errors"] += 1
                    self.stats["redis_available"] = False

            # Remove from local cache
            if key in self.local_cache:
                del self.local_cache[key]
                success = True

            return success

        except Exception:
            self.stats["errors"] += 1
            return False

    async def flush_all(self) -> bool:
        """Flush all cache entries."""
        try:
            # Try Redis first
            if self.redis_client and self.stats["redis_available"]:
                try:
                    await self.redis_client.flushdb()
                except Exception:
                    self.stats["errors"] += 1
                    self.stats["redis_available"] = False

            # Clear local cache
            self.local_cache.clear()
            return True

        except Exception:
            self.stats["errors"] += 1
            return False

    async def flush_pattern(self, pattern: str) -> bool:
        """Flush cache entries matching pattern (Redis only)."""
        try:
            if self.redis_client and self.stats["redis_available"]:
                try:
                    keys = await self.redis_client.keys(pattern)
                    if keys:
                        await self.redis_client.delete(*keys)
                    return True
                except Exception:
                    self.stats["errors"] += 1
                    self.stats["redis_available"] = False
                    return False

            # For local cache, do a simple prefix match
            keys_to_delete = [k for k in self.local_cache.keys() if pattern.replace("*", "") in k]
            for key in keys_to_delete:
                del self.local_cache[key]
            return True

        except Exception:
            self.stats["errors"] += 1
            return False

    async def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        stats = dict(self.stats)
        stats["local_cache_size"] = len(self.local_cache)

        if self.redis_client and self.stats["redis_available"]:
            try:
                info = await self.redis_client.info("memory")
                stats["redis_memory_used"] = info.get("used_memory_human", "unknown")
                stats["redis_connected_clients"] = info.get("connected_clients", 0)
            except Exception:
                stats["redis_available"] = False

        return stats

    async def health_check(self) -> Dict[str, Any]:
        """Check cache health status."""
        result = {
            "redis_available": False,
            "local_cache_available": True,
            "error": None,
        }

        if self.redis_client:
            try:
                await self.redis_client.ping()
                result["redis_available"] = True
                self.stats["redis_available"] = True
            except Exception as e:
                result["error"] = str(e)
                self.stats["redis_available"] = False

        return result
