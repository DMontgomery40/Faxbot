"""
Faxbot MCP Streamable HTTP server (Python). This is the primary remote transport.

Every MCP request must carry the caller's own Faxbot API key, as X-API-Key or
Authorization: Bearer <key>; the server forwards it to Faxbot as X-API-Key.
With OAuth enabled (OAUTH_ISSUER, OAUTH_AUDIENCE, optional OAUTH_JWKS_URL) the
bearer token is a JWT and the Faxbot key comes from X-API-Key or from
MCP_SUBJECT_KEYS_FILE, a JSON object mapping token subjects to keys.

Usage:
    pip install -r requirements.txt
    export FAX_API_URL=http://localhost:8080
    uvicorn http_server:app --host 127.0.0.1 --port 3004
"""
import os
from contextlib import asynccontextmanager
from typing import Optional

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

if __package__:
    from .faxbot_tools import SERVER_VERSION, build_server, environment_api_url
    from .transport_config import (APIConfiguration, APIConfigurationProvider, BearerTokenVerifier,
                                   CallerCredentialMiddleware, OAuthConfiguration, SubjectKeyMap)
else:
    from faxbot_tools import SERVER_VERSION, build_server, environment_api_url
    from transport_config import (APIConfiguration, APIConfigurationProvider, BearerTokenVerifier,
                                  CallerCredentialMiddleware, OAuthConfiguration, SubjectKeyMap)

FAX_API_URL = environment_api_url()
OAUTH_ISSUER = (os.getenv("OAUTH_ISSUER") or "").rstrip("/")
OAUTH_AUDIENCE = os.getenv("OAUTH_AUDIENCE") or ""
OAUTH_JWKS_URL = os.getenv("OAUTH_JWKS_URL") or ""
SUBJECT_KEYS_FILE = os.getenv("MCP_SUBJECT_KEYS_FILE") or ""


def health(_):
    return JSONResponse({"status": "ok", "transport": "streamable-http", "server": "faxbot-mcp", "version": SERVER_VERSION})


def secure(application: Starlette, *, require_oauth: bool, oauth_issuer: str, oauth_audience: str,
           oauth_jwks_url: str, subject_keys_file: str) -> Starlette:
    verifier = None
    if require_oauth:
        verifier = BearerTokenVerifier(OAuthConfiguration(oauth_issuer, oauth_audience, oauth_jwks_url)).verify
    application.add_middleware(CallerCredentialMiddleware, verifier=verifier,
                               subject_keys=SubjectKeyMap(subject_keys_file), issuer=oauth_issuer.rstrip("/"))
    return application


def create_app(*, api_base_url: str = FAX_API_URL, api_key: str = "",
               api_config_provider: Optional[APIConfigurationProvider] = None,
               require_oauth: bool = bool(OAUTH_ISSUER), oauth_issuer: str = OAUTH_ISSUER,
               oauth_audience: str = OAUTH_AUDIENCE, oauth_jwks_url: str = OAUTH_JWKS_URL,
               subject_keys_file: str = SUBJECT_KEYS_FILE) -> Starlette:
    """Build the Streamable HTTP app.

    Only the base URL is taken from ``api_base_url``/``api_config_provider``.
    ``api_key`` and a provider's key are ignored: HTTP callers always use their own key.
    """
    fixed = APIConfiguration(api_base_url, api_key)
    provider = api_config_provider if api_config_provider is not None else lambda: fixed

    def new_server():
        return build_server(api_base_url=lambda: provider().api_base_url)

    # SDK session managers are single-use. Each lifespan owns a fresh server/manager.
    server = new_server()
    transport_mount = Mount("/", app=server.streamable_http_app())

    @asynccontextmanager
    async def lifespan(application: Starlette):
        server = new_server()
        inner = server.streamable_http_app()
        transport_mount.app = inner
        application.state.mcp = server
        async with inner.router.lifespan_context(inner):
            yield

    application = Starlette(routes=[Route("/health", health), transport_mount], lifespan=lifespan)
    application.state.mcp = server
    secure(application, require_oauth=require_oauth, oauth_issuer=oauth_issuer, oauth_audience=oauth_audience,
           oauth_jwks_url=oauth_jwks_url, subject_keys_file=subject_keys_file)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in (os.getenv("MCP_HTTP_CORS_ORIGIN") or "*").split(",") if origin.strip()],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "Mcp-Session-Id", "MCP-Protocol-Version",
                       "Mcp-Method", "Mcp-Name", "Last-Event-ID"],
        expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    )
    return application


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "3004")))


if __name__ == "__main__":
    main()
