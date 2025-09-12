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
import base64
import pathlib
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
async def send_fax(to: str, fileContent: Optional[str] = None, fileName: Optional[str] = None, fileType: Optional[str] = None, filePath: Optional[str] = None, fileUrl: Optional[str] = None) -> str:  # noqa: N803
    """Send a fax. Preferred: provide filePath to a local PDF/TXT. Fallback: base64 fileContent."""
    # Preferred: filePath
    if filePath:
        import os
        import pathlib
        p = str(pathlib.Path(filePath).expanduser().resolve())
        if not os.path.exists(p):
            raise ValueError(f"File not found: {p}")
        if p.lower().endswith('.pdf'):
            with open(p, 'rb') as fh:
                data = fh.read()
            b64 = base64.b64encode(data).decode('ascii')
            job = await _api_send(to, os.path.basename(p), b64, 'pdf')
            used_name = os.path.basename(p)
        elif p.lower().endswith('.txt'):
            with open(p, 'rb') as fh:
                data = fh.read()  # preserve exact bytes
            b64 = base64.b64encode(data).decode('ascii')
            job = await _api_send(to, os.path.basename(p), b64, 'txt')
            used_name = os.path.basename(p)
        else:
            raise ValueError('filePath must point to a PDF or TXT file')
        return (
            f"Fax queued successfully!\n\nJob ID: {job['id']}\nRecipient: {to}\nFile: {used_name}\nStatus: {job['status']}\n"
            f"\nUse get_fax_status with job ID '{job['id']}' to check progress."
        )
    # Optional: fileUrl
    if fileUrl and not filePath:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(fileUrl)
            resp.raise_for_status()
            ct = (resp.headers.get('content-type') or '').lower()
            name_guess = pathlib.Path(fileUrl.split('?')[0].split('#')[0]).name or ('document.pdf' if 'pdf' in ct else 'document.txt')
            if ('pdf' not in ct) and (not name_guess.lower().endswith('.pdf')) and ('text/plain' not in ct) and (not name_guess.lower().endswith('.txt')):
                raise ValueError('Unsupported content-type for fileUrl (expect PDF or text/plain)')
            b64 = base64.b64encode(resp.content).decode('ascii')
            ft = 'pdf' if ('pdf' in ct or name_guess.lower().endswith('.pdf')) else 'txt'
            job = await _api_send(to, name_guess, b64, ft)
            return (
                f"Fax queued successfully!\n\nJob ID: {job['id']}\nRecipient: {to}\nFile: {name_guess}\nStatus: {job['status']}\n"
            )
    # Fallback: base64
    if not (fileContent and fileName):
        raise ValueError('Missing required parameters: fileContent and fileName (or provide filePath)')
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


@mcp.tool()
async def list_inbound(limit: Optional[int] = 20, cursor: Optional[str] = None) -> str:
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
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
        headers = {"X-API-Key": API_KEY} if API_KEY else {}
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
    return await get_fax_status(id)


@mcp.tool()
async def get_inbound_pdf(inboundId: str, asBase64: Optional[bool] = False) -> str:  # noqa: N803
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{FAX_API_URL}/inbound/{inboundId}/pdf", headers=headers)
        if resp.status_code == 404:
            raise ValueError(f"Inbound not found: {inboundId}")
        resp.raise_for_status()
        if asBase64:
            return base64.b64encode(resp.content).decode('ascii')
        return f"/inbound/{inboundId}/pdf"


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
