"""
Faxbot MCP streamable HTTP server (Python).

Exposes the Streamable HTTP transport for MCP, mounted under /mcp endpoints.
Standalone defaults do not add OAuth2; embedded factories can require it.

Usage:
    cd python_mcp
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    export FAX_API_URL=http://localhost:8080
    export API_KEY=your_api_key
    uvicorn http_server:app --host 0.0.0.0 --port 3004
"""
import os
from contextlib import asynccontextmanager
from typing import Optional

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.middleware.cors import CORSMiddleware

from mcp.server.fastmcp import FastMCP

import httpx

if __package__:
    from .transport_config import (APIConfiguration, APIConfigurationProvider, BearerTokenVerifier,
                                   OAuthConfiguration, OAuthMiddleware, bind_tool, current_api_configuration)
else:
    from transport_config import (APIConfiguration, APIConfigurationProvider, BearerTokenVerifier,
                                  OAuthConfiguration, OAuthMiddleware, bind_tool, current_api_configuration)

FAX_API_URL = os.getenv("FAX_API_URL", "http://localhost:8080").rstrip("/")
API_KEY = os.getenv("API_KEY", "")

mcp = FastMCP(name="Faxbot MCP (Python)")


async def _api_send(to: str, file_name: str, file_b64: str, file_type: Optional[str]):
    import base64
    if not to or not file_name or not file_b64:
        raise ValueError("Missing required parameters: to, fileName, fileContent")
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
    files = {"to": (None, to), "file": (file_name, data, content_type)}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{configuration.api_base_url}/fax", headers=headers, files=files)
        resp.raise_for_status()
        return resp.json()


async def _api_status(job_id: str):
    configuration = current_api_configuration(FAX_API_URL, API_KEY)
    headers = {"X-API-Key": configuration.api_key} if configuration.api_key else {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{configuration.api_base_url}/fax/{job_id}", headers=headers)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def send_fax(to: str, fileContent: str, fileName: str, fileType: Optional[str] = None) -> str:  # noqa: N803
    job = await _api_send(to, fileName, fileContent, fileType)
    return f"Fax queued. Job ID: {job['id']} Status: {job['status']}"


@mcp.tool()
async def get_fax_status(jobId: str) -> str:  # noqa: N803
    job = await _api_status(jobId)
    return f"Job {job['id']} status: {job['status']}"


def _http_app_from_mcp(server: FastMCP):
    return server.streamable_http_app()


def health(_):
    return JSONResponse({"status": "ok", "transport": "streamable-http", "server": "faxbot-mcp", "version": "2.0.0"})


def create_app(*, api_base_url: str = FAX_API_URL, api_key: str = API_KEY,
               api_config_provider: Optional[APIConfigurationProvider] = None,
               require_oauth: bool = False, oauth_issuer: str = '',
               oauth_audience: str = '', oauth_jwks_url: str = '') -> Starlette:
    fixed = APIConfiguration(api_base_url, api_key)
    provider = api_config_provider if api_config_provider is not None else lambda: fixed

    def new_server():
        server = FastMCP(name='Faxbot MCP (Python)')
        server.tool()(bind_tool(send_fax, provider))
        server.tool()(bind_tool(get_fax_status, provider))
        return server

    # SDK session managers are single-use. Each lifespan owns a fresh server/manager.
    server = new_server()
    transport_mount = Mount('/', app=_http_app_from_mcp(server))

    @asynccontextmanager
    async def lifespan(application: Starlette):
        server = new_server()
        inner = _http_app_from_mcp(server)
        transport_mount.app = inner
        application.state.mcp = server
        async with inner.router.lifespan_context(inner):
            yield

    application = Starlette(routes=[Route('/health', health), transport_mount], lifespan=lifespan)
    application.state.mcp = server
    if require_oauth:
        verifier = BearerTokenVerifier(OAuthConfiguration(oauth_issuer, oauth_audience, oauth_jwks_url))
        application.add_middleware(OAuthMiddleware, verifier=verifier.verify)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
        allow_methods=["*"],
    )
    return application


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "3004")))


if __name__ == "__main__":
    main()
