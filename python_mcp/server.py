"""Faxbot MCP SSE server (Python), kept for clients that only speak the
deprecated HTTP+SSE transport. New integrations should use http_server.py.

Endpoints: ``GET /sse`` and ``POST /messages/``. Every request must carry the
caller's own Faxbot API key (``Authorization: Bearer`` or ``X-API-Key``); with
OAuth configured, the token subject is mapped to a key from
``MCP_OAUTH_SUBJECT_KEYS_FILE``. ``API_KEY`` is never used by this server.

Environment: FAX_API_URL, PORT (default 3003), MCP_ALLOWED_HOSTS,
MCP_ALLOWED_ORIGINS, OAUTH_ISSUER, OAUTH_AUDIENCE, OAUTH_JWKS_URL,
MCP_OAUTH_SUBJECT_KEYS_FILE, MCP_RESOURCE_URL.
Run: uvicorn server:app --host 0.0.0.0 --port 3003
"""
import os
from typing import Optional

from starlette.applications import Starlette

if __package__:
    from .http_server import FAX_API_URL, OAUTH_AUDIENCE, OAUTH_ISSUER, OAUTH_JWKS_URL, create_network_app
    from .transport_config import APIConfigurationProvider
else:
    from http_server import FAX_API_URL, OAUTH_AUDIENCE, OAUTH_ISSUER, OAUTH_JWKS_URL, create_network_app
    from transport_config import APIConfigurationProvider


def create_app(*, api_base_url: str = FAX_API_URL, api_key: str = '',
               api_config_provider: Optional[APIConfigurationProvider] = None,
               require_oauth: bool = True, oauth_issuer: str = OAUTH_ISSUER,
               oauth_audience: str = OAUTH_AUDIENCE, oauth_jwks_url: str = OAUTH_JWKS_URL, **options) -> Starlette:
    """SSE app. ``api_key`` is accepted for compatibility and unused."""
    return create_network_app('sse', api_base_url=api_base_url, api_config_provider=api_config_provider,
                              require_oauth=require_oauth, oauth_issuer=oauth_issuer,
                              oauth_audience=oauth_audience, oauth_jwks_url=oauth_jwks_url, **options)


app = create_app(require_oauth=bool(OAUTH_ISSUER))


def main() -> None:
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT', '3003')))


if __name__ == '__main__':
    main()
