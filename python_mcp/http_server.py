"""Faxbot MCP Streamable HTTP server (Python), the primary remote transport.

Serves MCP at ``/mcp`` in stateless mode, so 2026-07-28 clients and
initialize-handshake (2025) clients are both answered per request. Every
request must carry the caller's own Faxbot API key (``Authorization: Bearer``
or ``X-API-Key``); with OAuth configured, the token subject is mapped to a key
from ``MCP_OAUTH_SUBJECT_KEYS_FILE``. The key is forwarded to Faxbot as
``X-API-Key``. ``API_KEY`` is never used by this server.

Environment: FAX_API_URL, PORT (default 3004), MCP_ALLOWED_HOSTS,
MCP_ALLOWED_ORIGINS, OAUTH_ISSUER, OAUTH_AUDIENCE, OAUTH_JWKS_URL,
MCP_OAUTH_SUBJECT_KEYS_FILE, MCP_RESOURCE_URL.
Run: uvicorn http_server:app --host 0.0.0.0 --port 3004
"""
import inspect
import os
from contextlib import asynccontextmanager
from typing import Literal, Optional

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

if __package__:
    from .faxbot_tools import SERVER_NAME, SERVER_VERSION, register_tools
    from .transport_config import (MAX_REQUEST_BODY_SIZE, APIConfiguration, APIConfigurationProvider,
                                   BearerTokenVerifier, CallerCredentialMiddleware, OAuthConfiguration,
                                   caller_api_key, env_list, load_subject_keys)
else:
    from faxbot_tools import SERVER_NAME, SERVER_VERSION, register_tools
    from transport_config import (MAX_REQUEST_BODY_SIZE, APIConfiguration, APIConfigurationProvider,
                                  BearerTokenVerifier, CallerCredentialMiddleware, OAuthConfiguration,
                                  caller_api_key, env_list, load_subject_keys)

FAX_API_URL = os.getenv('FAX_API_URL', 'http://localhost:8080').rstrip('/')
OAUTH_ISSUER = (os.getenv('OAUTH_ISSUER') or '').rstrip('/')
OAUTH_AUDIENCE = os.getenv('OAUTH_AUDIENCE') or ''
OAUTH_JWKS_URL = os.getenv('OAUTH_JWKS_URL') or ''


class _SseAsgiEndpoint:
    """The SDK SSE endpoint streams through ASGI send and then returns an empty
    Response; a normal Route would send that response a second time."""
    def __init__(self, endpoint):
        self.endpoint = endpoint

    async def __call__(self, scope, receive, send):
        await self.endpoint(Request(scope, receive, send))


def _sse_app(server: MCPServer, security: TransportSecuritySettings):
    application = server.sse_app(transport_security=security, max_request_body_size=MAX_REQUEST_BODY_SIZE)
    for index, route in enumerate(application.routes):
        if isinstance(route, Route) and route.path == '/sse' and inspect.iscoroutinefunction(route.endpoint):
            application.routes[index] = Route(route.path, endpoint=_SseAsgiEndpoint(route.endpoint),
                                              methods=route.methods, name=route.name)
    return application


def create_network_app(transport: Literal['streamable-http', 'sse'], *, api_base_url: str,
                       api_config_provider: Optional[APIConfigurationProvider], require_oauth: bool,
                       oauth_issuer: str, oauth_audience: str, oauth_jwks_url: str,
                       subject_keys_file: Optional[str] = None, allowed_hosts: Optional[list[str]] = None,
                       allowed_origins: Optional[list[str]] = None,
                       resource_url: Optional[str] = None) -> Starlette:
    """One isolated MCP application whose tool calls use the calling client's key.

    ``api_config_provider`` (when given) is sampled once per tool call for the
    Faxbot base URL only; any key it returns is ignored, because a network
    transport must not share one key across callers.
    """
    fixed_url = api_base_url
    allowed_hosts = env_list('MCP_ALLOWED_HOSTS') if allowed_hosts is None else allowed_hosts
    allowed_origins = env_list('MCP_ALLOWED_ORIGINS') if allowed_origins is None else allowed_origins
    security = TransportSecuritySettings(enable_dns_rebinding_protection=bool(allowed_hosts),
                                         allowed_hosts=allowed_hosts, allowed_origins=allowed_origins)

    def resolve(ctx: Context) -> APIConfiguration:
        key = caller_api_key(ctx.request_context.request)
        if not key:
            raise ToolError('This request has no Faxbot API key.')
        base_url = api_config_provider().api_base_url if api_config_provider is not None else fixed_url
        return APIConfiguration(base_url, key)

    def new_transport():
        server = register_tools(MCPServer(SERVER_NAME, version=SERVER_VERSION, log_level='WARNING'),
                                resolve, local_files=False)
        if transport == 'sse':
            return server, _sse_app(server, security)
        return server, server.streamable_http_app(stateless_http=True, transport_security=security,
                                                  max_request_body_size=MAX_REQUEST_BODY_SIZE)

    # SDK session managers are single-use. Each lifespan owns a fresh server and app.
    server, inner = new_transport()
    transport_mount = Mount('/', app=inner)

    @asynccontextmanager
    async def lifespan(application: Starlette):
        server, inner = new_transport()
        transport_mount.app = inner
        application.state.mcp = server
        async with inner.router.lifespan_context(inner):
            yield

    async def health(_request):
        return JSONResponse({'status': 'ok', 'transport': transport, 'server': 'faxbot-mcp',
                             'version': SERVER_VERSION})

    application = Starlette(routes=[Route('/health', health, methods=['GET']), transport_mount], lifespan=lifespan)
    application.state.mcp = server
    verifier = None
    subject_keys_file = os.getenv('MCP_OAUTH_SUBJECT_KEYS_FILE', '') if subject_keys_file is None else subject_keys_file
    if require_oauth:
        verifier = BearerTokenVerifier(OAuthConfiguration(oauth_issuer, oauth_audience, oauth_jwks_url)).verify
    application.add_middleware(
        CallerCredentialMiddleware, verifier=verifier, subject_keys=lambda: load_subject_keys(subject_keys_file),
        allowed_origins=allowed_origins, authorization_server=oauth_issuer.rstrip('/'),
        resource_url=(os.getenv('MCP_RESOURCE_URL', '') if resource_url is None else resource_url) if require_oauth else '',
    )
    return application


def create_app(*, api_base_url: str = FAX_API_URL, api_key: str = '',
               api_config_provider: Optional[APIConfigurationProvider] = None,
               require_oauth: bool = False, oauth_issuer: str = '',
               oauth_audience: str = '', oauth_jwks_url: str = '', **options) -> Starlette:
    """Streamable HTTP app. ``api_key`` is accepted for compatibility and unused."""
    return create_network_app('streamable-http', api_base_url=api_base_url, api_config_provider=api_config_provider,
                              require_oauth=require_oauth, oauth_issuer=oauth_issuer,
                              oauth_audience=oauth_audience, oauth_jwks_url=oauth_jwks_url, **options)


app = create_app(require_oauth=bool(OAUTH_ISSUER), oauth_issuer=OAUTH_ISSUER, oauth_audience=OAUTH_AUDIENCE,
                 oauth_jwks_url=OAUTH_JWKS_URL)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT', '3004')))


if __name__ == '__main__':
    main()
