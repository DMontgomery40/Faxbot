"""
Faxbot MCP SSE server (Python), kept for clients that only speak the
deprecated HTTP+SSE transport. New clients should use http_server.py
(Streamable HTTP).

Credentials work as in http_server.py: each request carries the caller's own
Faxbot API key (X-API-Key or Authorization: Bearer <key>), or, with OAuth
enabled, a JWT plus X-API-Key or a MCP_SUBJECT_KEYS_FILE entry for its subject.
OAuth is required by default here; set REQUIRE_MCP_OAUTH=false to accept keys.

Usage:
    pip install -r requirements.txt
    export FAX_API_URL=http://localhost:8080
    export OAUTH_ISSUER=https://issuer.example OAUTH_AUDIENCE=faxbot-mcp
    uvicorn server:app --host 127.0.0.1 --port 3003
"""
import inspect
import os
from contextlib import asynccontextmanager
from typing import Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp.server import MCPServer

if __package__:
    from .faxbot_tools import SERVER_VERSION, build_server, environment_api_url
    from .http_server import secure
    from .transport_config import APIConfiguration, APIConfigurationProvider
else:
    from faxbot_tools import SERVER_VERSION, build_server, environment_api_url
    from http_server import secure
    from transport_config import APIConfiguration, APIConfigurationProvider

FAX_API_URL = environment_api_url()
OAUTH_ISSUER = (os.getenv("OAUTH_ISSUER") or "").rstrip("/")
OAUTH_AUDIENCE = os.getenv("OAUTH_AUDIENCE") or ""
OAUTH_JWKS_URL = os.getenv("OAUTH_JWKS_URL") or ""
REQUIRE_OAUTH = (os.getenv("REQUIRE_MCP_OAUTH") or "true").lower() != "false"
SUBJECT_KEYS_FILE = os.getenv("MCP_SUBJECT_KEYS_FILE") or ""


class _SseAsgiEndpoint:
    """The SDK endpoint streams its response directly through ASGI send."""
    def __init__(self, endpoint):
        self.endpoint = endpoint

    async def __call__(self, scope, receive, send):
        # The SDK returns an empty Response after completing the stream.
        # A normal HTTP Route would send that response a second time on disconnect.
        await self.endpoint(Request(scope, receive, send))


def _sse_app(server: MCPServer):
    application = server.sse_app()
    for index, route in enumerate(application.routes):
        if isinstance(route, Route) and route.path == "/sse" and inspect.iscoroutinefunction(route.endpoint):
            if route.app.__module__ != "starlette.routing":
                raise RuntimeError("Unexpected SDK SSE route wrapper; cannot safely adapt it")
            application.routes[index] = Route(
                route.path, endpoint=_SseAsgiEndpoint(route.endpoint),
                methods=route.methods, name=route.name, include_in_schema=route.include_in_schema,
            )
    return application


async def health(_request: Request):
    return JSONResponse({"status": "ok", "transport": "sse", "server": "faxbot-mcp", "version": SERVER_VERSION})


def create_app(*, api_base_url: str = FAX_API_URL, api_key: str = "",
               api_config_provider: Optional[APIConfigurationProvider] = None,
               require_oauth: bool = REQUIRE_OAUTH, oauth_issuer: str = OAUTH_ISSUER,
               oauth_audience: str = OAUTH_AUDIENCE, oauth_jwks_url: str = OAUTH_JWKS_URL,
               subject_keys_file: str = SUBJECT_KEYS_FILE) -> Starlette:
    """Build the SSE app. Only the base URL is taken from the configuration; callers use their own key."""
    fixed = APIConfiguration(api_base_url, api_key)
    provider = api_config_provider if api_config_provider is not None else lambda: fixed

    def new_server():
        return build_server(api_base_url=lambda: provider().api_base_url)

    configured = new_server()
    transport_mount = Mount("/", app=_sse_app(configured))

    @asynccontextmanager
    async def lifespan(application: Starlette):
        configured = new_server()
        transport = _sse_app(configured)
        application.state.mcp = configured
        transport_mount.app = transport
        async with transport.router.lifespan_context(transport):
            yield

    application = Starlette(routes=[Route("/health", health, methods=["GET"]), transport_mount], lifespan=lifespan)
    application.state.mcp = configured
    return secure(application, require_oauth=require_oauth, oauth_issuer=oauth_issuer, oauth_audience=oauth_audience,
                  oauth_jwks_url=oauth_jwks_url, subject_keys_file=subject_keys_file)


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "3003")))


if __name__ == "__main__":
    main()
