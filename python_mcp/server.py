"""
Faxbot MCP SSE server (Python) with OAuth2 Bearer (JWT) authentication.

Environment variables:
- OAUTH_ISSUER: OIDC issuer URL (e.g., https://example.auth0.com)
- OAUTH_AUDIENCE: Expected audience claim (e.g., faxbot-mcp)
- OAUTH_JWKS_URL: Optional override for JWKS endpoint
- FAX_API_URL: Faxbot API base URL (default http://localhost:8080)
- API_KEY: Optional API key for Faxbot REST API
- PORT: Port to bind (default 3003)

Run (example):
    cd python_mcp
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    export OAUTH_ISSUER=https://example.auth0.com
    export OAUTH_AUDIENCE=faxbot-mcp
    export OAUTH_JWKS_URL=https://example.auth0.com/.well-known/jwks.json
    export FAX_API_URL=http://localhost:8080
    export API_KEY=my_api_key
    uvicorn server:app --host 0.0.0.0 --port 3003
"""
import base64
import pathlib
import os
import inspect
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.routing import Route, Mount

from mcp.server.fastmcp import FastMCP

if __package__:
    from .transport_config import (APIConfiguration, APIConfigurationProvider, BearerTokenVerifier,
                                   OAuthConfiguration, OAuthMiddleware, bind_tool, current_api_configuration)
else:
    from transport_config import (APIConfiguration, APIConfigurationProvider, BearerTokenVerifier,
                                  OAuthConfiguration, OAuthMiddleware, bind_tool, current_api_configuration)


# ===== Config =====
OAUTH_ISSUER = (os.getenv("OAUTH_ISSUER") or "").rstrip("/")
OAUTH_AUDIENCE = os.getenv("OAUTH_AUDIENCE") or ""
OAUTH_JWKS_URL = os.getenv("OAUTH_JWKS_URL") or (f"{OAUTH_ISSUER}/.well-known/jwks.json" if OAUTH_ISSUER else "")
FAX_API_URL = os.getenv("FAX_API_URL", "http://localhost:8080").rstrip("/")
API_KEY = os.getenv("API_KEY", "")


# ===== JWT validation helpers =====
_default_verifier = BearerTokenVerifier(OAuthConfiguration(OAUTH_ISSUER, OAUTH_AUDIENCE, OAUTH_JWKS_URL))
_jwks_cache = _default_verifier.cache


async def fetch_jwks(client: httpx.AsyncClient) -> Dict[str, Any]:
    return await _default_verifier.fetch_jwks(client)


def _find_jwk_for_kid(jwks: Dict[str, Any], kid: str) -> Optional[Dict[str, Any]]:
    keys = jwks.get("keys") or []
    for k in keys:
        if k.get("kid") == kid:
            return k
    return None


async def verify_bearer_token(auth_header: str) -> Dict[str, Any]:
    return await _default_verifier.verify(auth_header)


# ===== Faxbot HTTP helpers =====
async def api_send_fax(to: str, file_name: str, file_b64: str, file_type: Optional[str] = None) -> Dict[str, Any]:
    if not to or not file_name or not file_b64:
        raise ValueError("Missing required parameters: to, fileName, fileContent")
    # Determine mime type
    ext = (file_name.rsplit(".", 1)[-1] or "").lower()
    if not file_type:
        if ext == "pdf":
            file_type = "pdf"
        elif ext == "txt":
            file_type = "txt"
        else:
            raise ValueError("Unsupported file type; specify 'fileType' as 'pdf' or 'txt'")
    if file_type not in {"pdf", "txt"}:
        raise ValueError("fileType must be 'pdf' or 'txt'")

    content_type = "application/pdf" if file_type == "pdf" else "text/plain"
    data = base64.b64decode(file_b64)
    if not data:
        raise ValueError("File content is empty")

    configuration = current_api_configuration(FAX_API_URL, API_KEY)
    headers = {"X-API-Key": configuration.api_key} if configuration.api_key else {}

    files = {
        "to": (None, to),
        "file": (file_name, data, content_type),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{configuration.api_base_url}/fax", headers=headers, files=files)
        if resp.status_code != 202:
            detail = None
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Fax API error {resp.status_code}: {detail}")
        return resp.json()


async def api_get_status(job_id: str) -> Dict[str, Any]:
    if not job_id:
        raise ValueError("jobId is required")
    configuration = current_api_configuration(FAX_API_URL, API_KEY)
    headers = {"X-API-Key": configuration.api_key} if configuration.api_key else {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{configuration.api_base_url}/fax/{job_id}", headers=headers)
        if resp.status_code != 200:
            detail = None
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Fax API error {resp.status_code}: {detail}")
        return resp.json()


# ===== MCP (FastMCP) =====
mcp = FastMCP(name="Faxbot MCP (Python)")


@mcp.tool()
async def send_fax(to: str, fileContent: Optional[str] = None, fileName: Optional[str] = None, fileType: Optional[str] = None, filePath: Optional[str] = None, fileUrl: Optional[str] = None) -> str:  # noqa: N803
    """Send a fax using the Faxbot REST API.

    Args:
        to: Destination fax number (e.g., +15551234567)
        fileContent: Base64-encoded PDF or TXT content (fallback)
        fileName: File name (e.g., document.pdf) (fallback)
        fileType: Optional override ('pdf' or 'txt') (fallback)
        filePath: Absolute/relative path to PDF/TXT (preferred)
    Returns:
        Human-readable confirmation text containing job ID and status.
    """
    if filePath:
        p = str(pathlib.Path(filePath).expanduser().resolve())
        if not os.path.exists(p):
            raise ValueError(f"File not found: {p}")
        if p.lower().endswith('.pdf'):
            with open(p, 'rb') as fh:
                data = fh.read()
            b64 = base64.b64encode(data).decode('ascii')
            job = await api_send_fax(to, os.path.basename(p), b64, 'pdf')
        elif p.lower().endswith('.txt'):
            with open(p, 'rb') as fh:
                data = fh.read()
            b64 = base64.b64encode(data).decode('ascii')
            job = await api_send_fax(to, os.path.basename(p), b64, 'txt')
        else:
            raise ValueError('filePath must point to a PDF or TXT file')
    elif fileUrl:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(fileUrl)
            resp.raise_for_status()
            ct = (resp.headers.get('content-type') or '').lower()
            name_guess = pathlib.Path(fileUrl.split('?')[0].split('#')[0]).name or ('document.pdf' if 'pdf' in ct else 'document.txt')
            if ('pdf' not in ct) and (not name_guess.lower().endswith('.pdf')) and ('text/plain' not in ct) and (not name_guess.lower().endswith('.txt')):
                raise ValueError('Unsupported content-type for fileUrl (expect PDF or text/plain)')
            b64 = base64.b64encode(resp.content).decode('ascii')
            ft = 'pdf' if ('pdf' in ct or name_guess.lower().endswith('.pdf')) else 'txt'
            job = await api_send_fax(to, name_guess, b64, ft)
    else:
        if not (fileContent and fileName):
            raise ValueError('Missing required parameters: fileContent and fileName (or provide filePath)')
        job = await api_send_fax(to, fileName, fileContent, fileType)
    return (
        f"Fax queued successfully!\n\nJob ID: {job['id']}\nRecipient: {to}\nFile: {fileName or os.path.basename(filePath) if filePath else ''}\nStatus: {job['status']}\n"
        f"\nUse get_fax_status with job ID '{job['id']}' to check progress."
    )


@mcp.tool()
async def get_fax_status(jobId: str) -> str:  # noqa: N803
    """Retrieve the status of a fax job by ID."""
    job = await api_get_status(jobId)
    lines = [
        "Fax Job Status\n",
        f"Job ID: {job['id']}",
        f"Status: {job['status']}",
        f"Recipient: {job.get('to')}",
    ]
    if job.get('pages'):
        lines.append(f"Pages: {job['pages']}")
    lines.append(f"Created: {job.get('created_at')}")
    lines.append(f"Updated: {job.get('updated_at')}")
    if job.get('error'):
        lines.append(f"Error: {job['error']}")
    return "\n".join(lines)


@mcp.tool()
async def list_inbound(limit: Optional[int] = 20, cursor: Optional[str] = None) -> str:
    configuration = current_api_configuration(FAX_API_URL, API_KEY)
    headers = {'X-API-Key': configuration.api_key} if configuration.api_key else {}
    params = {}
    if limit is not None:
        params['limit'] = limit
    if cursor:
        params['cursor'] = cursor
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{configuration.api_base_url}/inbound", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict) and 'items' in data:
        lines = [f"• {x.get('id')} from {x.get('fr') or x.get('from') or 'unknown'} → {x.get('to') or 'unknown'}{(' ('+str(x.get('pages'))+'p)') if x.get('pages') else ''}" for x in data['items']]
        return "Inbound List\n\n" + "\n".join(lines)
    return "Inbound List\n\n" + str(data)


@mcp.tool()
async def get_fax(id: str) -> str:  # noqa: N803
    if id.lower().startswith('in_'):
        configuration = current_api_configuration(FAX_API_URL, API_KEY)
        headers = {'X-API-Key': configuration.api_key} if configuration.api_key else {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{configuration.api_base_url}/inbound/{id}", headers=headers)
            if resp.status_code == 404:
                raise ValueError(f"Inbound not found: {id}")
            resp.raise_for_status()
            fx = resp.json()
        parts = [
            "Inbound Fax",
            f"ID: {fx.get('id')}",
            f"From: {fx.get('fr') or fx.get('from')}",
            f"To: {fx.get('to')}",
        ]
        if fx.get('pages'):
            parts.append(f"Pages: {fx['pages']}")
        if fx.get('received_at'):
            parts.append(f"Received: {fx['received_at']}")
        return "\n".join([p for p in parts if p])
    # else outbound
    return await get_fax_status(id)


@mcp.tool()
async def get_inbound_pdf(inboundId: str, asBase64: Optional[bool] = False) -> str:  # noqa: N803
    configuration = current_api_configuration(FAX_API_URL, API_KEY)
    headers = {'X-API-Key': configuration.api_key} if configuration.api_key else {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{configuration.api_base_url}/inbound/{inboundId}/pdf", headers=headers)
        if resp.status_code == 404:
            raise ValueError(f"Inbound not found: {inboundId}")
        resp.raise_for_status()
        if asBase64:
            return base64.b64encode(resp.content).decode('ascii')
        # Hint path (clients with HTTP access can fetch directly)
        return f"/inbound/{inboundId}/pdf"



class _SseAsgiEndpoint:
    """The SDK endpoint streams its response directly through ASGI send."""
    def __init__(self, endpoint):
        self.endpoint = endpoint

    async def __call__(self, scope, receive, send):
        # FastMCP 1.x returns an empty Response after completing the stream.
        # A normal HTTP Route would send that response a second time on disconnect.
        await self.endpoint(Request(scope, receive, send))


def _sse_app(server: FastMCP = mcp):
    application = server.sse_app()
    for index, route in enumerate(application.routes):
        if isinstance(route, Route) and route.path == server.settings.sse_path:
            # Authenticated SDK endpoints are already ASGI callables; retain them.
            if inspect.iscoroutinefunction(route.endpoint):
                if route.app.__module__ != "starlette.routing":
                    raise RuntimeError("Unexpected SDK SSE route wrapper; cannot safely adapt it")
                application.routes[index] = Route(
                    route.path, endpoint=_SseAsgiEndpoint(route.endpoint),
                    methods=route.methods, name=route.name, include_in_schema=route.include_in_schema,
                )
    return application


# Keep the SDK transport, security checks and original endpoint paths.
inner_app = _sse_app()


class AuthMiddleware(OAuthMiddleware):
    def __init__(self, app, *, verifier=verify_bearer_token):
        super().__init__(app, verifier=verifier)


async def health(_request: Request):
    return JSONResponse({"status": "ok", "transport": "sse", "server": "faxbot-mcp", "version": "2.0.0"})


def create_app(*, api_base_url: str = FAX_API_URL, api_key: str = API_KEY,
               api_config_provider: Optional[APIConfigurationProvider] = None,
               require_oauth: bool = True, oauth_issuer: str = OAUTH_ISSUER,
               oauth_audience: str = OAUTH_AUDIENCE, oauth_jwks_url: str = OAUTH_JWKS_URL) -> Starlette:
    """Create an isolated embedded transport; OAuth is fixed until restart.

    A provider is read once per tool invocation so an installation can rotate its
    active API key without keeping the initial SSE connection's settings frame.
    """
    fixed = APIConfiguration(api_base_url, api_key)
    provider = api_config_provider if api_config_provider is not None else lambda: fixed

    def new_server():
        configured = FastMCP(name='Faxbot MCP (Python)')
        for tool in (send_fax, get_fax_status, list_inbound, get_fax, get_inbound_pdf):
            configured.tool()(bind_tool(tool, provider))
        return configured

    configured = new_server()
    transport_mount = Mount('/', app=_sse_app(configured))

    @asynccontextmanager
    async def lifespan(application: Starlette):
        configured = new_server()
        transport = _sse_app(configured)
        application.state.mcp = configured
        transport_mount.app = transport
        async with transport.router.lifespan_context(transport):
            yield

    application = Starlette(routes=[Route('/health', health, methods=['GET']), transport_mount], lifespan=lifespan)
    application.state.mcp = configured
    if require_oauth:
        verifier = BearerTokenVerifier(OAuthConfiguration(oauth_issuer, oauth_audience, oauth_jwks_url))
        application.add_middleware(AuthMiddleware, verifier=verifier.verify)
    return application


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "3003")))


if __name__ == "__main__":
    main()
