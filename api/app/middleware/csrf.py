from __future__ import annotations

"""
Phase 2: Minimal CSRF middleware for cookie-based sessions.

Disabled by default. Mount conditionally when cookie sessions are enabled.
"""

import hmac
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware


class CSRFMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, cookie_name: str = "fb_csrf", header_name: str = "x-csrf-token"):
        super().__init__(app)
        self.cookie_name = cookie_name
        self.header_name = header_name
        self._methods = {"POST", "PUT", "PATCH", "DELETE"}

    async def dispatch(self, request: Request, call_next):
        if request.method in self._methods:
            cookie = request.cookies.get(self.cookie_name)
            header = request.headers.get(self.header_name)
            if not cookie or not header or not hmac.compare_digest(str(cookie), str(header)):
                raise HTTPException(status_code=403, detail="CSRF token invalid")
        return await call_next(request)

