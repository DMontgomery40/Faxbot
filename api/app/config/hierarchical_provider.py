from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
import os

from ..config import settings
from ..services.cache_manager import CacheManager


@dataclass
class UserContext:
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    department: Optional[str] = None
    groups: Optional[list[str]] = None


class HierarchicalConfigProvider:
    """Phase 3 hierarchical configuration facade (env/default fallback).

    Minimal read-only implementation to unblock v4 endpoints and UI.
    DB-backed layers will be added in PR13/PR14; until then we expose
    effective values based on environment variables and defaults from
    `settings` only, and return empty structures for DB levels.
    """

    def __init__(self, cache: Optional[CacheManager] = None) -> None:
        self.cache_manager = cache or CacheManager()

    async def get_effective(self, key: str, ctx: Optional[UserContext]) -> Dict[str, Any]:
        """Return a dict with key, value, and source: db|env|default.
        DB layers are not yet live in this workspace; treat as env/default.
        """
        # Map a few canonical keys to env + settings for visibility in UI
        mapping: Dict[str, Tuple[str, Any]] = {
            "system.public_api_url": ("PUBLIC_API_URL", settings.public_api_url),
            "api.rate_limit_rpm": ("API_RATE_LIMIT_RPM", settings.max_requests_per_minute),
            "security.enforce_public_https": ("ENFORCE_PUBLIC_HTTPS", settings.enforce_public_https),
            "storage.s3.bucket": ("S3_BUCKET", settings.s3_bucket),
            "storage.s3.region": ("S3_REGION", settings.s3_region),
            "storage.s3.endpoint_url": ("S3_ENDPOINT_URL", settings.s3_endpoint_url),
        }
        if key not in mapping:
            # Unknown key: return no value but keep shape for UI
            return {"key": key, "value": None, "source": None}
        env_key, default_val = mapping[key]
        raw = os.getenv(env_key)
        value = raw if raw is not None else default_val
        source = "env" if raw is not None else "default"
        return {"key": key, "value": value, "source": source}

    async def get_hierarchy(self, key: str, ctx: Optional[UserContext]) -> Dict[str, Any]:
        """Return values at each level. DB layers are empty until PR13 lands."""
        eff = await self.get_effective(key, ctx)
        return {
            "key": key,
            "levels": {
                "user": None,
                "group": None,
                "department": None,
                "tenant": None,
                "global": None,
                "env": os.getenv(self._env_for_key(key)),
                "default": eff.get("value") if eff.get("source") == "default" else None,
            },
            "effective": eff,
        }

    async def get_safe_edit_keys(self) -> Dict[str, Dict[str, Any]]:
        """Expose safe keys. Keep empty for read-only stage until PR17."""
        return {}

    async def flush_cache(self, scope: Optional[str] = None) -> Dict[str, Any]:
        if not self.cache_manager:
            return {"ok": True, "deleted": 0, "scope": scope or "all", "backend": "none"}
        if scope and scope != "*":
            deleted = await self.cache_manager.delete_pattern(scope)
            return {"ok": True, "deleted": deleted, "scope": scope, "backend": "memory"}
        await self.cache_manager.flush_all()
        return {"ok": True, "deleted": None, "scope": "all", "backend": "memory"}

    def _env_for_key(self, key: str) -> str:
        m = {
            "system.public_api_url": "PUBLIC_API_URL",
            "api.rate_limit_rpm": "API_RATE_LIMIT_RPM",
            "security.enforce_public_https": "ENFORCE_PUBLIC_HTTPS",
            "storage.s3.bucket": "S3_BUCKET",
            "storage.s3.region": "S3_REGION",
            "storage.s3.endpoint_url": "S3_ENDPOINT_URL",
        }
        return m.get(key, key.upper().replace(".", "_"))

