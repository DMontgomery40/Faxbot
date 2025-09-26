"""
Hierarchical Rate Limiting Middleware for Faxbot v4.

Implements per-endpoint, per-user rate limiting using the hierarchical configuration
system. Supports tenant/department/group/user overrides with Redis backend.
"""

import time
from typing import Any, Dict, Optional, Tuple
from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from ..config.hierarchical_provider import HierarchicalConfigProvider, UserContext


class HierarchicalRateLimiter(BaseHTTPMiddleware):
    """
    Rate limiting middleware using hierarchical configuration.

    Provides fine-grained rate limiting based on:
    - API endpoint patterns
    - User/tenant context
    - Configurable limits per hierarchy level
    """

    def __init__(self, app, config_provider: Optional[HierarchicalConfigProvider] = None):
        super().__init__(app)
        self.config_provider = config_provider
        self.rate_buckets: Dict[str, Dict[str, Any]] = {}

        # Default endpoint configurations
        self.endpoint_configs = {
            "/fax": {"base_key": "api.fax.rate_limit_rpm", "default_limit": 60},
            "/fax/": {"base_key": "api.fax.rate_limit_rpm", "default_limit": 60},
            "/admin/": {"base_key": "api.admin.rate_limit_rpm", "default_limit": 300},
            "/callbacks/": {"base_key": "api.callbacks.rate_limit_rpm", "default_limit": 1000},
            "default": {"base_key": "api.rate_limit_rpm", "default_limit": 120},
        }

    async def dispatch(self, request: Request, call_next) -> Response:
        """Apply hierarchical rate limiting to requests."""
        if not self.config_provider:
            # No hierarchical config available, fall back to basic rate limiting
            return await call_next(request)

        # Skip rate limiting for certain paths
        if self._should_skip_rate_limiting(request):
            return await call_next(request)

        try:
            # Get user context from request
            user_ctx = await self._extract_user_context(request)

            # Determine endpoint config
            endpoint_config = self._get_endpoint_config(request.url.path)

            # Get effective rate limit for this user/endpoint
            rate_limit = await self._get_effective_rate_limit(
                endpoint_config["base_key"],
                user_ctx,
                endpoint_config["default_limit"]
            )

            # Check rate limit
            if rate_limit > 0:  # 0 means unlimited
                bucket_key = self._get_bucket_key(user_ctx, request.url.path)
                if self._is_rate_limited(bucket_key, rate_limit):
                    return self._rate_limit_response(rate_limit)

        except Exception:
            # If rate limiting fails, don't block the request
            pass

        return await call_next(request)

    def _should_skip_rate_limiting(self, request: Request) -> bool:
        """Check if rate limiting should be skipped for this request."""
        path = request.url.path

        # Skip health checks and metrics
        if path in ["/health", "/metrics", "/openapi.json", "/docs", "/redoc"]:
            return True

        # Skip static assets
        if any(path.startswith(prefix) for prefix in ["/static/", "/assets/"]):
            return True

        return False

    async def _extract_user_context(self, request: Request) -> UserContext:
        """Extract user context from request for hierarchical config lookup."""
        # Default context
        user_ctx = UserContext(user_id=None, tenant_id=None, department=None, groups=[])

        # Try to get API key info from request state
        api_key_info = getattr(request.state, "api_key_info", None)
        if api_key_info:
            user_ctx.user_id = api_key_info.get("key_id")
            # In a full implementation, we'd look up tenant/department from user ID

        # For demo/testing, extract from headers if present
        tenant_id = request.headers.get("X-Tenant-ID")
        if tenant_id:
            user_ctx.tenant_id = tenant_id

        department = request.headers.get("X-Department")
        if department:
            user_ctx.department = department

        groups = request.headers.get("X-Groups", "").split(",")
        if groups and groups != [""]:
            user_ctx.groups = [g.strip() for g in groups]

        return user_ctx

    def _get_endpoint_config(self, path: str) -> Dict[str, Any]:
        """Get rate limiting configuration for an endpoint."""
        for pattern, config in self.endpoint_configs.items():
            if pattern != "default" and path.startswith(pattern):
                return config
        return self.endpoint_configs["default"]

    async def _get_effective_rate_limit(
        self,
        config_key: str,
        user_ctx: UserContext,
        default_limit: int
    ) -> int:
        """Get the effective rate limit for this user/endpoint combination."""
        try:
            config_value = await self.config_provider.get_effective(
                config_key, user_ctx, default_limit
            )
            return int(config_value.value)
        except Exception:
            return default_limit

    def _get_bucket_key(self, user_ctx: UserContext, path: str) -> str:
        """Generate a unique bucket key for rate limiting."""
        # Use user_id if available, otherwise use a generic key
        user_part = user_ctx.user_id or "anonymous"

        # Group similar endpoints together
        path_part = "fax" if path.startswith("/fax") else path.split("/")[1] if "/" in path else "default"

        return f"rate:{user_part}:{path_part}"

    def _is_rate_limited(self, bucket_key: str, limit: int) -> bool:
        """Check if the request should be rate limited."""
        now = int(time.time())
        current_minute = now // 60

        # Clean up old buckets (keep only current minute to bound memory)
        self._cleanup_buckets(current_minute)

        # Get or create bucket
        bucket = self.rate_buckets.get(bucket_key, {"count": 0, "window": current_minute})

        # Reset bucket if we're in a new window
        if bucket["window"] != current_minute:
            bucket = {"count": 0, "window": current_minute}

        # Check limit
        if bucket["count"] >= limit:
            return True

        # Increment counter
        bucket["count"] += 1
        self.rate_buckets[bucket_key] = bucket

        return False

    def _cleanup_buckets(self, current_minute: int) -> None:
        """Remove expired rate limiting buckets."""
        try:
            expired_keys = [
                key for key, bucket in self.rate_buckets.items()
                if bucket.get("window", 0) < current_minute
            ]
            for key in expired_keys:
                self.rate_buckets.pop(key, None)
        except Exception:
            pass

    def _rate_limit_response(self, limit: int) -> Response:
        """Return a rate limit exceeded response."""
        retry_after = 60  # Wait until next minute window

        content = {
            "error": "Rate limit exceeded",
            "detail": f"Maximum {limit} requests per minute allowed",
            "retry_after_seconds": retry_after
        }

        headers = {
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0",
        }

        return Response(
            content=str(content),
            status_code=429,
            headers=headers,
            media_type="application/json"
        )