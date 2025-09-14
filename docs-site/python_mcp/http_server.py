"""
Faxbot MCP streamable HTTP server (Python).

Exposes the Streamable HTTP transport for MCP, mounted under /mcp endpoints.
This variant does not add OAuth2; place behind your own gateway if needed.

Usage:
    cd python_mcp
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    export FAX_API_URL=http://localhost:8080
    export API_KEY=your_api_key
    uvicorn http_server:app --host 0.0.0.0 --port 3004
"""
import os
from typing import Optional

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.middleware.cors import CORSMiddleware

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP  # type: ignore

import httpx

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
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    files = {"to": (None, to), "file": (file_name, data, content_type)}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{FAX_API_URL}/fax", headers=headers, files=files)
        resp.raise_for_status()
        return resp.json()


async def _api_status(job_id: str):
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{FAX_API_URL}/fax/{job_id}", headers=headers)
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
    # Newer versions provide .http_app(); fallback to .streamable_http_app()
    if hasattr(server, "http_app"):
        return server.http_app()
    if hasattr(server, "streamable_http_app"):
        return server.streamable_http_app()
    raise RuntimeError("Installed 'mcp' package does not expose a Streamable HTTP app. Please upgrade 'mcp'.")


def health(_):
    return JSONResponse({"status": "ok", "transport": "streamable-http", "server": "faxbot-mcp", "version": "2.0.0"})


inner = _http_app_from_mcp(mcp)
app = Starlette(
    routes=[
        Route('/health', health),
        Mount('/', app=inner),
    ]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_headers=["*"] ,
    expose_headers=["Mcp-Session-Id"],
    allow_methods=["*"],
)

