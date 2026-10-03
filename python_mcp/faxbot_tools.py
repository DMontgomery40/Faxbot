"""Faxbot MCP tools shared by the stdio, Streamable HTTP and SSE servers.

Credential rule: on HTTP transports every Faxbot call uses the API key that the
caller presented on that MCP request (resolved by transport_config); stdio uses
the one configured integration key.
"""
import base64
import os
import pathlib
from collections.abc import Callable
from typing import Annotated, Any, Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ResourceLink, TextContent

if __package__:
    from .transport_config import CALLER_KEY_SCOPE
else:
    from transport_config import CALLER_KEY_SCOPE

SERVER_NAME = "Faxbot MCP (Python)"
SERVER_VERSION = "3.0.0"
TOOL_NAMES = ("send_fax", "get_fax_status", "get_fax", "list_inbound", "get_inbound_pdf")
INBOUND_PDF_URI = "faxbot://inbound/{inbound_id}/pdf"
_FILE_TYPES = {"pdf": "application/pdf", "txt": "text/plain"}


class FaxJob(BaseModel):
    """Outbound fax job as returned by GET /fax/{id}."""
    model_config = ConfigDict(extra="allow")
    id: str
    status: str
    to: Optional[str] = None
    backend: Optional[str] = None
    pages: Optional[int] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class InboundFax(BaseModel):
    """Inbound fax metadata as returned by GET /inbound."""
    model_config = ConfigDict(extra="allow")
    id: str
    fr: Optional[str] = None
    to: Optional[str] = None
    status: Optional[str] = None
    backend: Optional[str] = None
    pages: Optional[int] = None
    received_at: Optional[str] = None


class InboundList(BaseModel):
    items: list[InboundFax]


class FaxLookup(BaseModel):
    kind: Literal["outbound", "inbound"]
    fax: dict[str, Any]


class FaxbotAPIError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Faxbot API returned {status}: {detail}")
        self.status = status


class FaxbotAPI:
    """Minimal Faxbot REST client bound to one base URL and one caller key."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key} if api_key else {}

    async def _request(self, method: str, path: str, *, expected: int = 200, timeout: float = 15.0, **kwargs) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, f"{self.base_url}{path}", headers=self.headers, **kwargs)
        if response.status_code != expected:
            try:
                detail = response.json().get("detail") or response.text
            except Exception:
                detail = response.text
            raise FaxbotAPIError(response.status_code, str(detail)[:300])
        return response

    async def send_fax(self, to: str, file_name: str, data: bytes, content_type: str) -> dict[str, Any]:
        files = {"to": (None, to), "file": (file_name, data, content_type)}
        return (await self._request("POST", "/fax", expected=202, timeout=60.0, files=files)).json()

    async def get_fax(self, job_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/fax/{job_id}")).json()

    async def list_inbound(self) -> list[dict[str, Any]]:
        data = (await self._request("GET", "/inbound")).json()
        # The API returns a bare list; tolerate an {"items": [...]} envelope as well.
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise FaxbotAPIError(502, "Unexpected /inbound response shape")
        return items

    async def get_inbound(self, inbound_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/inbound/{inbound_id}")).json()

    async def get_inbound_pdf(self, inbound_id: str) -> bytes:
        return (await self._request("GET", f"/inbound/{inbound_id}/pdf", timeout=60.0)).content


def _tool_error(error: FaxbotAPIError) -> ToolError:
    messages = {
        401: "Faxbot rejected the API key.",
        403: "This API key is not allowed to do that.",
        404: "Not found.",
        413: "The file is larger than the server allows.",
        415: "Only PDF and TXT files can be faxed.",
    }
    return ToolError(messages.get(error.status, str(error)))


def _detect_type(file_name: str, file_type: Optional[str]) -> str:
    kind = (file_type or pathlib.PurePath(file_name).suffix.lstrip(".")).lower()
    if kind not in _FILE_TYPES:
        raise ToolError("fileType must be 'pdf' or 'txt'.")
    return kind


def build_server(*, api_base_url: Callable[[], str], stdio_api_key: Optional[str] = None) -> MCPServer:
    """Create an MCP server exposing the Faxbot tools.

    ``stdio_api_key`` is set only for the stdio server, which represents one
    configured integration identity and may read local files. HTTP servers pass
    None: each call then requires the caller's own key and never reads files or
    URLs on the server's behalf.
    """
    local = stdio_api_key is not None
    server = MCPServer(name=SERVER_NAME, version=SERVER_VERSION)

    def api(ctx: Context) -> FaxbotAPI:
        request = ctx.request_context.request if ctx is not None else None
        if request is None:
            if not local:
                raise ToolError("This request carries no Faxbot API key.")
            return FaxbotAPI(api_base_url(), stdio_api_key)
        key = getattr(request, "scope", {}).get(CALLER_KEY_SCOPE)
        if not key:
            raise ToolError("This request carries no Faxbot API key.")
        return FaxbotAPI(api_base_url(), key)

    async def load_document(file_path, file_url, file_content, file_name, file_type):
        if file_path and local:
            path = pathlib.Path(file_path).expanduser().resolve()
            if not path.is_file():
                raise ToolError(f"File not found: {path}")
            kind = _detect_type(path.name, file_type)
            return path.name, path.read_bytes(), kind
        if file_url and local:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(file_url)
            if response.status_code != 200:
                raise ToolError(f"Could not download fileUrl (HTTP {response.status_code}).")
            content_type = (response.headers.get("content-type") or "").lower()
            name = pathlib.PurePosixPath(httpx.URL(file_url).path).name or "document"
            guessed = "pdf" if "pdf" in content_type else "txt" if "text/plain" in content_type else None
            kind = _detect_type(name, file_type or guessed)
            return name, response.content, kind
        if file_path or file_url:
            raise ToolError("filePath and fileUrl are available only on the local stdio server; send fileContent instead.")
        if not (file_content and file_name):
            raise ToolError("Provide fileContent (base64) and fileName.")
        try:
            data = base64.b64decode(file_content, validate=True)
        except Exception:
            raise ToolError("fileContent is not valid base64.") from None
        if not data:
            raise ToolError("fileContent is empty.")
        return file_name, data, _detect_type(file_name, file_type)

    send_description = (
        "Send a fax. Provide fileContent (base64) with fileName; PDF and TXT are accepted."
        + (" Locally you may instead give filePath or fileUrl." if local else "")
    )

    async def send(ctx, to, file_content, file_name, file_type, file_path=None, file_url=None):
        name, data, kind = await load_document(file_path, file_url, file_content, file_name, file_type)
        try:
            job = FaxJob.model_validate(await api(ctx).send_fax(to, name, data, _FILE_TYPES[kind]))
        except FaxbotAPIError as error:
            raise _tool_error(error) from None
        text = f"Fax {job.id} to {to} is {job.status}. Use get_fax_status to follow it."
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=job.model_dump(mode="json"))

    if local:
        @server.tool(name="send_fax", description=send_description)
        async def send_fax_local(
            to: Annotated[str, Field(description="Destination fax number, for example +15551234567.")],
            ctx: Context,
            fileContent: Annotated[Optional[str], Field(description="Base64-encoded PDF or TXT content.")] = None,  # noqa: N803
            fileName: Annotated[Optional[str], Field(description="File name such as document.pdf.")] = None,  # noqa: N803
            fileType: Annotated[Optional[Literal["pdf", "txt"]], Field(description="Overrides the type inferred from the name.")] = None,  # noqa: N803
            filePath: Annotated[Optional[str], Field(description="Local PDF or TXT path.")] = None,  # noqa: N803
            fileUrl: Annotated[Optional[str], Field(description="HTTP(S) URL of a PDF or TXT file.")] = None,  # noqa: N803
        ) -> Annotated[CallToolResult, FaxJob]:
            return await send(ctx, to, fileContent, fileName, fileType, filePath, fileUrl)
    else:
        @server.tool(name="send_fax", description=send_description)
        async def send_fax_remote(
            to: Annotated[str, Field(description="Destination fax number, for example +15551234567.")],
            fileContent: Annotated[str, Field(description="Base64-encoded PDF or TXT content.")],  # noqa: N803
            fileName: Annotated[str, Field(description="File name such as document.pdf.")],  # noqa: N803
            ctx: Context,
            fileType: Annotated[Optional[Literal["pdf", "txt"]], Field(description="Overrides the type inferred from fileName.")] = None,  # noqa: N803
        ) -> Annotated[CallToolResult, FaxJob]:
            return await send(ctx, to, fileContent, fileName, fileType)

    @server.tool(name="get_fax_status", description="Get the status of an outbound fax job.")
    async def get_fax_status(
        jobId: Annotated[str, Field(description="Job id returned by send_fax.")],  # noqa: N803
        ctx: Context,
    ) -> Annotated[CallToolResult, FaxJob]:
        try:
            job = FaxJob.model_validate(await api(ctx).get_fax(jobId))
        except FaxbotAPIError as error:
            raise _tool_error(error) from None
        text = f"Fax {job.id} is {job.status}." + (f" {job.error}" if job.error else "")
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=job.model_dump(mode="json"))

    @server.tool(name="get_fax", description="Get an outbound job or an inbound fax by id.")
    async def get_fax(
        id: Annotated[str, Field(description="Outbound job id or inbound fax id.")],  # noqa: A002
        ctx: Context,
    ) -> Annotated[CallToolResult, FaxLookup]:
        client = api(ctx)
        try:
            lookup = FaxLookup(kind="outbound", fax=FaxJob.model_validate(await client.get_fax(id)).model_dump(mode="json"))
        except FaxbotAPIError as error:
            if error.status != 404:
                raise _tool_error(error) from None
            try:
                lookup = FaxLookup(kind="inbound", fax=InboundFax.model_validate(await client.get_inbound(id)).model_dump(mode="json"))
            except FaxbotAPIError as inbound_error:
                raise _tool_error(inbound_error) from None
        fax = lookup.fax
        if lookup.kind == "outbound":
            text = f"Outbound fax {fax['id']} is {fax['status']}."
        else:
            text = f"Inbound fax {fax['id']} from {fax.get('fr') or 'unknown'}."
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=lookup.model_dump(mode="json"))

    @server.tool(name="list_inbound", description="List recent inbound faxes (metadata only).")
    async def list_inbound(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=100, description="Maximum number of faxes to return.")] = 20,
    ) -> Annotated[CallToolResult, InboundList]:
        try:
            items = [InboundFax.model_validate(item) for item in (await api(ctx).list_inbound())[:limit]]
        except FaxbotAPIError as error:
            raise _tool_error(error) from None
        lines = [f"{item.id} from {item.fr or 'unknown'} to {item.to or 'unknown'}" for item in items]
        text = f"{len(items)} inbound fax(es)." + ("\n" + "\n".join(lines) if lines else "")
        result = InboundList(items=items)
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=result.model_dump(mode="json"))

    @server.tool(name="get_inbound_pdf",
                 description="Get an inbound fax PDF as a resource link, or embedded when asBase64 is true.")
    async def get_inbound_pdf(
        inboundId: Annotated[str, Field(description="Inbound fax id.")],  # noqa: N803
        ctx: Context,
        asBase64: Annotated[bool, Field(description="Embed the PDF bytes instead of returning a link.")] = False,  # noqa: N803
    ) -> CallToolResult:
        uri = INBOUND_PDF_URI.format(inbound_id=inboundId)
        if not asBase64:
            link = ResourceLink(type="resource_link", uri=uri, name=f"inbound-{inboundId}.pdf", mime_type="application/pdf")
            return CallToolResult(content=[link])
        try:
            data = await api(ctx).get_inbound_pdf(inboundId)
        except FaxbotAPIError as error:
            raise _tool_error(error) from None
        blob = BlobResourceContents(uri=uri, mime_type="application/pdf", blob=base64.b64encode(data).decode("ascii"))
        return CallToolResult(content=[EmbeddedResource(type="resource", resource=blob)])

    @server.resource(INBOUND_PDF_URI, name="inbound_pdf", mime_type="application/pdf",
                     description="PDF of an inbound fax.")
    async def inbound_pdf(inbound_id: str, ctx: Context) -> bytes:
        try:
            return await api(ctx).get_inbound_pdf(inbound_id)
        except (FaxbotAPIError, ToolError) as error:
            raise ResourceError(str(error)) from None

    return server


def environment_api_url() -> str:
    return os.getenv("FAX_API_URL", "http://localhost:8080").rstrip("/")
