"""The Faxbot MCP tools and inbound PDF resource, shared by every Python transport.

Tool names and arguments are the public contract: send_fax, get_fax_status,
get_fax, list_inbound and get_inbound_pdf. Each call resolves its Faxbot base
URL and API key through ``resolve(ctx)``; transports decide where the key comes
from (the environment for stdio, the caller's own request for HTTP and SSE).
"""
from __future__ import annotations

import asyncio
import base64
import os
import pathlib
import re
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal, Optional

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import (BlobResourceContents, CallToolResult, EmbeddedResource, ResourceLink, TextContent,
                       ToolAnnotations)
from pydantic import BaseModel, Field

if __package__:
    from .transport_config import APIConfiguration
else:
    from transport_config import APIConfiguration

SERVER_NAME = 'Faxbot MCP (Python)'
SERVER_VERSION = '3.0.0'
INBOUND_PDF_URI = 'faxbot://inbound/{inbound_id}/pdf'

# One send_fax call sends the same fax again, with the same operation id, at most this many more times
# after a transport failure or HTTP 502/503/504. Read at call time so tests can shorten the waits.
SEND_RETRIES = 2
SEND_RETRY_BACKOFF = 0.5  # seconds before the first retry; each later retry waits twice as long
_RETRYABLE_STATUSES = frozenset({502, 503, 504})
# The request may or may not have reached Faxbot.
_TRANSPORT_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
_OPERATION_ID = re.compile(r'[\x21-\x7e]{1,128}')

Resolver = Callable[[Context], APIConfiguration]
OperationId = Annotated[Optional[str], Field(description=(
    'Leave empty for a new fax. To finish a send_fax call that failed without a confirmation, pass the '
    'operationId from its error with the same number and document.'))]


class FaxJob(BaseModel):
    id: str
    status: str
    to: Optional[str] = None
    pages: Optional[int] = None
    error: Optional[str] = None
    backend: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SentFax(FaxJob):
    operationId: str  # noqa: N815 - the id that finishes this same fax if a later call repeats it


class InboundFax(BaseModel):
    id: str
    status: Optional[str] = None
    fr: Optional[str] = None
    to: Optional[str] = None
    pages: Optional[int] = None
    backend: Optional[str] = None
    received_at: Optional[str] = None


class InboundList(BaseModel):
    items: list[InboundFax]


class FaxDetails(BaseModel):
    direction: Literal['outbound', 'inbound']
    id: str
    status: Optional[str] = None
    fr: Optional[str] = None
    to: Optional[str] = None
    pages: Optional[int] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    received_at: Optional[str] = None


def _fields(model: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    return {name: data.get(name) for name in model.model_fields if data.get(name) is not None}


def inbound_items(data: Any) -> list[dict[str, Any]]:
    """GET /inbound returns a bare list; tolerate an ``{"items": [...]}`` envelope too."""
    items = data.get('items') if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ToolError('Faxbot returned an unexpected inbound list.')
    return [item for item in items if isinstance(item, dict)]


def _api_error(response: httpx.Response) -> str:
    try:
        detail = response.json().get('detail')
    except Exception:
        detail = response.text
    return f'Fax API error {response.status_code}: {detail or response.reason_phrase}'


async def _call(configuration: APIConfiguration, method: str, path: str, *, expect: int,
                timeout: float = 15.0, **kwargs) -> httpx.Response:
    headers = {'X-API-Key': configuration.api_key} if configuration.api_key else {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.request(method, f'{configuration.api_base_url}{path}', headers=headers, **kwargs)
    if response.status_code != expect:
        raise ToolError(_api_error(response))
    return response


def _unconfirmed(operation_id: str) -> ToolError:
    return ToolError(f'Faxbot did not confirm this fax (operationId {operation_id}). Call send_fax again with '
                     f'operationId={operation_id} and the same number and document to finish this same fax '
                     'without sending it twice.')


async def _post_fax(configuration: APIConfiguration, operation_id: str, files: dict) -> httpx.Response:
    """POST /fax with the operation id, sending the same request again after an unconfirmed attempt."""
    headers = {'Idempotency-Key': operation_id}
    if configuration.api_key:
        headers['X-API-Key'] = configuration.api_key
    retries = max(0, int(SEND_RETRIES))
    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(retries + 1):
            if attempt:
                await asyncio.sleep(SEND_RETRY_BACKOFF * 2 ** (attempt - 1))
            try:
                response = await client.post(f'{configuration.api_base_url}/fax', headers=headers, files=files)
            except _TRANSPORT_ERRORS:
                if attempt == retries:
                    raise _unconfirmed(operation_id) from None
                continue
            if response.status_code not in _RETRYABLE_STATUSES:
                return response
            if attempt == retries:
                raise _unconfirmed(operation_id)
    raise AssertionError('unreachable')


async def _submit(configuration: APIConfiguration, to: str, name: str, data: bytes, file_type: Optional[str],
                  operation_id: Optional[str] = None):
    if not to:
        raise ToolError('Missing required parameter: to')
    file_type = file_type or {'pdf': 'pdf', 'txt': 'txt'}.get(name.rsplit('.', 1)[-1].lower())
    if file_type not in {'pdf', 'txt'}:
        raise ToolError("fileType must be 'pdf' or 'txt'")
    if not data:
        raise ToolError('File content is empty')
    # A call without an operation id is a new fax; its id exists before the upload so an unconfirmed
    # send can be finished with it.
    if operation_id is None or operation_id == '':
        operation_id = str(uuid.uuid4())
    if not _OPERATION_ID.fullmatch(operation_id):
        raise ToolError('operationId must be 1 to 128 printable ASCII characters without spaces')
    content_type = 'application/pdf' if file_type == 'pdf' else 'text/plain'
    response = await _post_fax(configuration, operation_id,
                               {'to': (None, to), 'file': (name, data, content_type)})
    if response.status_code == 409:
        raise ToolError(f'{_api_error(response)} operationId {operation_id} belongs to a different number or '
                        'document; omit operationId to send a new fax.')
    if response.status_code != 202:
        raise ToolError(_api_error(response))
    job = response.json()
    text = f"Fax queued. Job ID: {job['id']}. Status: {job['status']}. Use get_fax_status to check progress."
    return CallToolResult(content=[TextContent(type='text', text=text)],
                          structured_content={'id': job['id'], 'status': job['status'], 'operationId': operation_id})


def _decode(file_content: str) -> bytes:
    try:
        return base64.b64decode(''.join(file_content.split()), validate=True)  # allow line-wrapped base64
    except Exception:
        raise ToolError('fileContent must be base64 encoded') from None


async def _job(configuration: APIConfiguration, job_id: str) -> dict[str, Any]:
    if not job_id:
        raise ToolError('jobId is required')
    return (await _call(configuration, 'GET', f'/fax/{job_id}', expect=200)).json()


def register_tools(server: MCPServer, resolve: Resolver, *, local_files: bool) -> MCPServer:
    """Register the Faxbot tools. Local file and URL reads are offered only to stdio."""
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
    read = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    send_description = ('Send a fax to a phone number. Provide a PDF or TXT document as base64 fileContent '
                        'with fileName.')
    once = (' Each call without operationId sends a new fax. If a call fails without confirming the fax, '
            'its error gives an operationId; call send_fax again with it and the same number and document '
            'to finish that same fax without sending it twice.')

    if local_files:
        @server.tool(name='send_fax', title='Send fax', annotations=write,
                     description=send_description + ' Prefer filePath (a local PDF or TXT) or fileUrl.' + once)
        async def send_fax_local(ctx: Context, to: str, filePath: Optional[str] = None,  # noqa: N803
                                 fileUrl: Optional[str] = None, fileContent: Optional[str] = None,
                                 fileName: Optional[str] = None,
                                 fileType: Optional[Literal['pdf', 'txt']] = None,
                                 operationId: OperationId = None) -> Annotated[CallToolResult, SentFax]:
            configuration = resolve(ctx)
            # Every source is read once, before any upload, so a retry sends the same document.
            if filePath:
                path = pathlib.Path(filePath).expanduser().resolve()
                if not path.is_file():
                    raise ToolError(f'File not found: {path}')
                if path.suffix.lower() not in {'.pdf', '.txt'}:
                    raise ToolError('filePath must point to a PDF or TXT file')
                return await _submit(configuration, to, path.name, path.read_bytes(), path.suffix[1:].lower(),
                                     operationId)
            if fileUrl:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    response = await client.get(fileUrl)
                response.raise_for_status()
                content_type = (response.headers.get('content-type') or '').lower()
                name = pathlib.PurePosixPath(fileUrl.split('?')[0].split('#')[0]).name or 'document'
                kind = 'pdf' if 'pdf' in content_type or name.lower().endswith('.pdf') else (
                    'txt' if 'text/plain' in content_type or name.lower().endswith('.txt') else None)
                if kind is None:
                    raise ToolError('fileUrl must return a PDF or plain text document')
                return await _submit(configuration, to, name, response.content, kind, operationId)
            if not (fileContent and fileName):
                raise ToolError('Provide filePath, fileUrl, or fileContent with fileName')
            return await _submit(configuration, to, fileName, _decode(fileContent), fileType, operationId)
    else:
        @server.tool(name='send_fax', title='Send fax', annotations=write, description=send_description + once)
        async def send_fax(ctx: Context, to: str, fileContent: str, fileName: str,  # noqa: N803
                           fileType: Optional[Literal['pdf', 'txt']] = None,
                           operationId: OperationId = None) -> Annotated[CallToolResult, SentFax]:
            return await _submit(resolve(ctx), to, fileName, _decode(fileContent), fileType, operationId)

    @server.tool(title='Fax status', annotations=read, description='Check the status of a fax sent with send_fax.')
    async def get_fax_status(ctx: Context, jobId: str) -> Annotated[CallToolResult, FaxJob]:  # noqa: N803
        job = await _job(resolve(ctx), jobId)
        lines = [f"Job ID: {job.get('id')}", f"Status: {job.get('status')}", f"Recipient: {job.get('to')}"]
        lines += [f'{label}: {job[key]}' for key, label in (('pages', 'Pages'), ('error', 'Error'),
                                                            ('updated_at', 'Updated')) if job.get(key)]
        return CallToolResult(content=[TextContent(type='text', text='\n'.join(lines))],
                              structured_content=_fields(FaxJob, job))

    @server.tool(title='Fax details', annotations=read,
                 description='Get details for a sent fax job id or a received (inbound) fax id.')
    async def get_fax(ctx: Context, id: str) -> Annotated[CallToolResult, FaxDetails]:  # noqa: A002
        configuration = resolve(ctx)
        data = None
        if not id.lower().startswith('in_'):
            try:
                data = {**(await _job(configuration, id)), 'direction': 'outbound'}
            except ToolError as error:
                if not str(error).startswith('Fax API error 404'):
                    raise
        if data is None:
            data = {**(await _call(configuration, 'GET', f'/inbound/{id}', expect=200)).json(),
                    'direction': 'inbound'}
        details = _fields(FaxDetails, data)
        text = '\n'.join(f'{key}: {value}' for key, value in details.items())
        return CallToolResult(content=[TextContent(type='text', text=text)], structured_content=details)

    @server.tool(title='Received faxes', annotations=read, description='List recently received faxes (metadata only).')
    async def list_inbound(ctx: Context, limit: int = 20) -> Annotated[CallToolResult, InboundList]:
        response = await _call(resolve(ctx), 'GET', '/inbound', expect=200)
        items = [_fields(InboundFax, item) for item in inbound_items(response.json())[:max(limit, 0)]]
        lines = [f"{item['id']} from {item.get('fr') or 'unknown'} to {item.get('to') or 'unknown'}"
                 + (f" ({item['pages']} pages)" if item.get('pages') else '') for item in items]
        return CallToolResult(content=[TextContent(type='text', text='\n'.join(lines) or 'No received faxes.')],
                              structured_content={'items': items})

    @server.tool(title='Received fax PDF', annotations=read,
                 description=('Get a received fax PDF. Returns a resource link by default; '
                              'set asBase64 to embed the PDF in the result.'))
    async def get_inbound_pdf(ctx: Context, inboundId: str, asBase64: bool = False):  # noqa: N803
        uri = INBOUND_PDF_URI.format(inbound_id=inboundId)
        if not asBase64:
            return [ResourceLink(type='resource_link', uri=uri, name=f'{inboundId}.pdf',
                                 mime_type='application/pdf')]
        response = await _call(resolve(ctx), 'GET', f'/inbound/{inboundId}/pdf', expect=200, timeout=30.0)
        blob = base64.b64encode(response.content).decode('ascii')
        return [EmbeddedResource(type='resource', resource=BlobResourceContents(
            uri=uri, mime_type='application/pdf', blob=blob))]

    @server.resource(INBOUND_PDF_URI, name='inbound_pdf', title='Received fax PDF', mime_type='application/pdf')
    async def inbound_pdf(inbound_id: str, ctx: Context) -> bytes:
        return (await _call(resolve(ctx), 'GET', f'/inbound/{inbound_id}/pdf', expect=200, timeout=30.0)).content

    return server


def environment_configuration() -> APIConfiguration:
    return APIConfiguration(os.getenv('FAX_API_URL', 'http://localhost:8080'), os.getenv('API_KEY', ''))
