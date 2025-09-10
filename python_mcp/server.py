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
import asyncio
import pathlib
import os
import time
from typing import Dict, Any, Optional

import httpx
from jose import jwt
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Route, Mount

try:
    # FastMCP available in mcp >= 0.3
    from mcp.server.fastmcp import FastMCP
except Exception:
    # Fallback import path if package layout differs
    from mcp.server.fastmcp import FastMCP  # type: ignore


# ===== Config =====
OAUTH_ISSUER = (os.getenv("OAUTH_ISSUER") or "").rstrip("/")
OAUTH_AUDIENCE = os.getenv("OAUTH_AUDIENCE") or ""
OAUTH_JWKS_URL = os.getenv("OAUTH_JWKS_URL") or (f"{OAUTH_ISSUER}/.well-known/jwks.json" if OAUTH_ISSUER else "")
FAX_API_URL = os.getenv("FAX_API_URL", "http://localhost:8080").rstrip("/")
API_KEY = os.getenv("API_KEY", "")


# ===== JWT validation helpers =====
_jwks_cache: Dict[str, Any] = {"ts": 0, "jwks": None}
_jwks_ttl = 300  # seconds


async def fetch_jwks(client: httpx.AsyncClient) -> Dict[str, Any]:
    now = time.time()
    if _jwks_cache["jwks"] and (now - _jwks_cache["ts"]) < _jwks_ttl:
        return _jwks_cache["jwks"]
    if not OAUTH_JWKS_URL:
        raise RuntimeError("OAUTH_JWKS_URL is not configured")
    resp = await client.get(OAUTH_JWKS_URL, timeout=10.0)
    resp.raise_for_status()
    jwks = resp.json()
    _jwks_cache["jwks"] = jwks
    _jwks_cache["ts"] = now
    return jwks


def _find_jwk_for_kid(jwks: Dict[str, Any], kid: str) -> Optional[Dict[str, Any]]:
    keys = jwks.get("keys") or []
    for k in keys:
        if k.get("kid") == kid:
            return k
    return None


async def verify_bearer_token(auth_header: str) -> Dict[str, Any]:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise ValueError("Missing bearer token")
    token = auth_header.split(" ", 1)[1].strip()

    # Decode header to find kid
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise ValueError("Token missing 'kid' header")

    async with httpx.AsyncClient() as client:
        jwks = await fetch_jwks(client)
    jwk = _find_jwk_for_kid(jwks, kid)
    if not jwk:
        raise ValueError("No matching JWK for token")

    # Verify using python-jose
    claims = jwt.decode(
        token,
        jwk,
        algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
        audience=OAUTH_AUDIENCE,
        issuer=OAUTH_ISSUER,
        options={"verify_aud": True, "verify_exp": True, "verify_nbf": True},
    )
    return claims


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

    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY

    files = {
        "to": (None, to),
        "file": (file_name, data, content_type),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{FAX_API_URL}/fax", headers=headers, files=files)
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
    headers = {}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{FAX_API_URL}/fax/{job_id}", headers=headers)
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
    headers = {}
    if API_KEY:
        headers['X-API-Key'] = API_KEY
    params = {}
    if limit is not None:
        params['limit'] = limit
    if cursor:
        params['cursor'] = cursor
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{FAX_API_URL}/inbound", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict) and 'items' in data:
        lines = [f"• {x.get('id')} from {x.get('fr') or x.get('from') or 'unknown'} → {x.get('to') or 'unknown'}{(' ('+str(x.get('pages'))+'p)') if x.get('pages') else ''}" for x in data['items']]
        return "Inbound List\n\n" + "\n".join(lines)
    return "Inbound List\n\n" + str(data)


@mcp.tool()
async def get_fax(id: str) -> str:  # noqa: N803
    if id.lower().startswith('in_'):
        headers = {'X-API-Key': API_KEY} if API_KEY else {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{FAX_API_URL}/inbound/{id}", headers=headers)
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
    headers = {'X-API-Key': API_KEY} if API_KEY else {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{FAX_API_URL}/inbound/{inboundId}/pdf", headers=headers)
        if resp.status_code == 404:
            raise ValueError(f"Inbound not found: {inboundId}")
        resp.raise_for_status()
        if asBase64:
            return base64.b64encode(resp.content).decode('ascii')
        # Hint path (clients with HTTP access can fetch directly)
        return f"/inbound/{inboundId}/pdf"



# Build underlying SSE ASGI app from FastMCP
inner_app = mcp.sse_app()


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # Allow health without auth
        if request.url.path == "/health":
            return await call_next(request)
        auth = request.headers.get("authorization")
        try:
            claims = await verify_bearer_token(auth or "")
            # Attach claims for downstream use if needed
            request.state.user = claims
        except Exception:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


async def health(_request: Request):
    return JSONResponse({"status": "ok", "transport": "sse", "server": "faxbot-mcp", "version": "2.0.0"})


app = Starlette(
    routes=[
        Route('/health', health, methods=['GET']),
        Mount('/', app=inner_app),
    ],
)
app.add_middleware(AuthMiddleware)
