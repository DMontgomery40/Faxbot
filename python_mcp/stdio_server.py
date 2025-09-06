"""
Faxbot MCP stdio server (Python).

Provides the same tools as the SSE server: send_fax and get_fax_status.
Intended for local assistant integrations that speak MCP over stdio.

Usage:
    cd python_mcp
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    export FAX_API_URL=http://localhost:8080
    export API_KEY=your_api_key
    python stdio_server.py
"""
import asyncio
import os
from typing import Optional, Dict, Any

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP  # type: ignore


FAX_API_URL = os.getenv("FAX_API_URL", "http://localhost:8080").rstrip("/")
API_KEY = os.getenv("API_KEY", "")

mcp = FastMCP(name="Faxbot MCP (Python)")


async def _api_send(to: str, file_name: str, file_b64: str, file_type: Optional[str]) -> Dict[str, Any]:
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
        if resp.status_code != 202:
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Fax API error {resp.status_code}: {detail}")
        return resp.json()


async def _api_status(job_id: str) -> Dict[str, Any]:
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{FAX_API_URL}/fax/{job_id}", headers=headers)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Fax API error {resp.status_code}: {detail}")
        return resp.json()


@mcp.tool()
async def send_fax(to: str, fileContent: str, fileName: str, fileType: Optional[str] = None) -> str:  # noqa: N803
    job = await _api_send(to, fileName, fileContent, fileType)
    return (
        f"Fax queued successfully!\n\nJob ID: {job['id']}\nRecipient: {to}\nFile: {fileName}\nStatus: {job['status']}\n"
        f"\nUse get_fax_status with job ID '{job['id']}' to check progress."
    )


@mcp.tool()
async def get_fax_status(jobId: str) -> str:  # noqa: N803
    job = await _api_status(jobId)
    parts = [
        f"Job ID: {job['id']}",
        f"Status: {job['status']}",
        f"Recipient: {job.get('to')}",
    ]
    if job.get('pages'):
        parts.append(f"Pages: {job['pages']}")
    if job.get('error'):
        parts.append(f"Error: {job['error']}")
    return "\n".join(parts)


def main() -> None:
    # FastMCP provides stdio runner; prefer run() or run_stdio() depending on version
    runner = None
    for attr in ("run_stdio", "run"):
        if hasattr(mcp, attr):
            runner = getattr(mcp, attr)
            break
    if runner is None:
        raise RuntimeError("Installed mcp does not support stdio runner API; please upgrade the 'mcp' package.")
    # Some versions require asyncio.run wrapper
    if asyncio.iscoroutinefunction(runner):
        asyncio.run(runner())
    else:
        runner()


if __name__ == "__main__":
    main()

