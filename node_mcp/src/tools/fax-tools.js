import { McpError, ErrorCode } from '@modelcontextprotocol/sdk/types.js';
import axios from 'axios';
import { sendFax as apiSendFax, getFaxStatus as apiGetFaxStatus, listInbound as apiListInbound, getInbound as apiGetInbound, downloadInboundPdf as apiDownloadInboundPdf } from '../shared/fax-client.js';
import fs from 'fs';
import path from 'path';

export const faxTools = [
  {
    name: 'send_fax',
    description: 'Send a fax to a recipient. Preferred: provide filePath to a local PDF/TXT, or fileUrl. Fallback: base64 fileContent.',
    inputSchema: {
      type: 'object',
      properties: {
        to: { type: 'string', description: 'Fax number (e.g., +1234567890)' },
        filePath: { type: 'string', description: 'Absolute or relative path to PDF or TXT file (preferred)' },
        fileUrl: { type: 'string', description: 'HTTP(S) URL to fetch the file from (PDF or TXT)' },
        fileContent: { type: 'string', description: 'Base64 encoded file content (PDF or plain text)' },
        fileName: { type: 'string', description: 'File name, e.g., document.pdf' },
        fileType: { type: 'string', enum: ['pdf', 'txt'], description: 'Optional override of file type' }
      },
      required: ['to']
    }
  },
  {
    name: 'get_fax_status',
    description: 'Check the status of a previously sent fax job',
    inputSchema: {
      type: 'object',
      properties: { jobId: { type: 'string', description: 'Job ID from send_fax' } },
      required: ['jobId']
    }
  },
  {
    name: 'get_fax',
    description: 'Get fax details by id. Supports outbound jobs (jobId) and inbound ids.',
    inputSchema: {
      type: 'object',
      properties: {
        id: { type: 'string', description: 'Outbound job id (e.g., fbj_*) or inbound id (e.g., in_*)' }
      },
      required: ['id']
    }
  },
  {
    name: 'list_inbound',
    description: 'List recent inbound faxes (metadata only).',
    inputSchema: {
      type: 'object',
      properties: {
        limit: { type: 'number', description: 'Max items to return (default 20)' },
        cursor: { type: 'string', description: 'Cursor token for pagination if supported' }
      }
    }
  },
  {
    name: 'get_inbound_pdf',
    description: 'Download inbound fax PDF and return a base64 string (small files) or a hint URL. Use with care for large files.',
    inputSchema: {
      type: 'object',
      properties: {
        inboundId: { type: 'string', description: 'Inbound fax id' },
        asBase64: { type: 'boolean', description: 'If true, return base64; otherwise return a hint URL', default: false }
      },
      required: ['inboundId']
    }
  }
];

function detectTypeFromName(name) {
  const ext = (name || '').split('.').pop()?.toLowerCase();
  if (ext === 'pdf') return 'pdf';
  if (ext === 'txt') return 'txt';
  return undefined;
}

function validatePhone(to) {
  const phoneRegex = /^[\+]?[\d\s\-\(\)]{7,20}$/;
  return phoneRegex.test(String(to).replace(/\s/g, ''));
}

export async function handleSendFaxTool(args) {
  const { to, fileContent, fileName, filePath, fileUrl } = args || {};
  let { fileType } = args || {};
  if (!to) throw new McpError(ErrorCode.InvalidParams, 'Missing required parameter: to');
  if (!validatePhone(to)) throw new McpError(ErrorCode.InvalidParams, 'Invalid recipient number format');

  // Preferred: filePath (preserve fidelity — upload original PDF/TXT)
  if (filePath && typeof filePath === 'string') {
    const resolved = path.isAbsolute(filePath) ? filePath : path.resolve(process.cwd(), filePath);
    try {
      const st = await fs.promises.stat(resolved);
      if (!st.isFile()) throw new Error('Not a file');
    } catch {
      throw new McpError(ErrorCode.InvalidParams, `File not found or not a file: ${resolved}`);
    }
    const ext = (resolved.split('.').pop() || '').toLowerCase();
    const base = path.basename(resolved);
    if (ext === 'pdf') {
      const buf = await fs.promises.readFile(resolved);
      const result = await apiSendFax(to, buf, 'pdf', base);
      return { content: [{ type: 'text', text: `Fax queued. Job ID: ${result.id}` }] };
    } else if (ext === 'txt') {
      const text = await fs.promises.readFile(resolved, 'utf8');
      const result = await apiSendFax(to, text, 'txt', base);
      return { content: [{ type: 'text', text: `Fax queued. Job ID: ${result.id}` }] };
    } else {
      throw new McpError(ErrorCode.InvalidParams, 'filePath must point to a PDF or TXT file');
    }
  }

  // Optional: fileUrl (HTTP/HTTPS)
  if (fileUrl && typeof fileUrl === 'string') {
    try {
      const resp = await axios.get(fileUrl, { responseType: 'arraybuffer', timeout: 30000 });
      const ct = String(resp.headers['content-type'] || '').toLowerCase();
      const nameGuess = (new URL(fileUrl)).pathname.split('/').pop() || (ct.includes('pdf') ? 'document.pdf' : 'document.txt');
      const isPdf = ct.includes('pdf') || nameGuess.toLowerCase().endsWith('.pdf');
      const isTxt = ct.includes('text/plain') || nameGuess.toLowerCase().endsWith('.txt');
      if (!isPdf && !isTxt) throw new Error('Unsupported content-type for fileUrl (expect PDF or text/plain)');
      const result = await apiSendFax(to, Buffer.from(resp.data), isPdf ? 'pdf' : 'txt', nameGuess);
      return { content: [{ type: 'text', text: `Fax queued. Job ID: ${result.id}` }] };
    } catch (err) {
      throw new McpError(ErrorCode.InvalidParams, `Failed to fetch fileUrl: ${(err && err.message) || 'unknown error'}`);
    }
  }

  // Backward-compatible base64 path
  if (!fileContent || !fileName) {
    throw new McpError(ErrorCode.InvalidParams, 'Missing required parameters: fileContent and fileName (or provide filePath)');
  }
  if (!fileType) fileType = detectTypeFromName(fileName);
  if (!['pdf', 'txt'].includes(fileType || '')) {
    throw new McpError(ErrorCode.InvalidParams, 'fileType must be either "pdf" or "txt"');
  }
  let buffer;
  try {
    buffer = Buffer.from(fileContent, 'base64');
  } catch {
    throw new McpError(ErrorCode.InvalidParams, 'Invalid base64 encoded file content');
  }
  if (!buffer || buffer.length === 0) {
    throw new McpError(ErrorCode.InvalidParams, 'File content is empty');
  }
  try {
    const result = await apiSendFax(to, fileType === 'pdf' ? buffer : buffer.toString('utf8'), fileType, fileName);
    return {
      content: [
        { type: 'text', text: `Fax queued successfully! Job ID: ${result.id}` },
      ],
    };
  } catch (err) {
    const e = err;
    const status = e?.response?.status;
    const detail = e?.response?.data?.detail || e?.response?.statusText;
    if (status === 401) throw new McpError(ErrorCode.InvalidParams, 'Invalid API key or authentication failed');
    if (status === 413) throw new McpError(ErrorCode.InvalidParams, 'File too large - exceeds maximum size limit');
    if (status === 415) throw new McpError(ErrorCode.InvalidParams, 'Unsupported file type - only PDF and TXT are allowed');
    throw new McpError(ErrorCode.InternalError, `Fax API error${status ? ' (' + status + ')' : ''}: ${detail || (err instanceof Error ? err.message : 'Unknown error')}`);
  }
}

export async function handleGetFaxStatusTool(args) {
  const { jobId } = args || {};
  if (!jobId) throw new McpError(ErrorCode.InvalidParams, 'Job ID is required');
  try {
    const job = await apiGetFaxStatus(jobId);
    let statusText = `Fax Job Status\n\n`;
    statusText += `Job ID: ${job.id}\n`;
    statusText += `Status: ${job.status}\n`;
    statusText += `Recipient: ${job.to}\n`;
    if (job.pages) statusText += `Pages: ${job.pages}\n`;
    if (job.created_at) statusText += `Created: ${new Date(job.created_at).toLocaleString()}\n`;
    if (job.updated_at) statusText += `Updated: ${new Date(job.updated_at).toLocaleString()}\n`;
    if (job.error) statusText += `Error: ${job.error}\n`;
    return { content: [{ type: 'text', text: statusText }] };
  } catch (err) {
    const e = err;
    const status = e?.response?.status;
    if (status === 404) throw new McpError(ErrorCode.InvalidParams, `Fax job not found: ${jobId}`);
    if (status === 401) throw new McpError(ErrorCode.InvalidParams, 'Invalid API key or authentication failed');
    throw new McpError(ErrorCode.InternalError, `Fax API error${status ? ' (' + status + ')' : ''}: ${e?.response?.statusText || (err instanceof Error ? err.message : 'Unknown error')}`);
  }
}

export async function handleGetFaxTool(args) {
  const { id } = args || {};
  if (!id) throw new McpError(ErrorCode.InvalidParams, 'id is required');
  // Heuristic: inbound often starts with in_, outbound with fbj_ or job_
  const looksInbound = /^in[_-]/i.test(id);
  try {
    if (looksInbound) {
      const fx = await apiGetInbound(id);
      const lines = [
        'Inbound Fax',
        `ID: ${fx.id}`,
        `From: ${fx.fr || fx.from || 'unknown'}`,
        `To: ${fx.to || 'unknown'}`,
        fx.pages ? `Pages: ${fx.pages}` : undefined,
        fx.received_at ? `Received: ${fx.received_at}` : undefined,
      ].filter(Boolean).join('\n');
      const data = { type: 'inbound', id: fx.id, from: fx.fr || fx.from, to: fx.to, pages: fx.pages, received_at: fx.received_at };
      return { content: [{ type: 'text', text: lines }, { type: 'text', text: JSON.stringify(data) }] };
    } else {
      const job = await apiGetFaxStatus(id);
      const lines = [
        'Outbound Fax',
        `Job ID: ${job.id}`,
        `Status: ${job.status}`,
        job.to ? `Recipient: ${job.to}` : undefined,
        job.pages ? `Pages: ${job.pages}` : undefined,
        job.created_at ? `Created: ${job.created_at}` : undefined,
        job.updated_at ? `Updated: ${job.updated_at}` : undefined,
        job.error ? `Error: ${job.error}` : undefined,
      ].filter(Boolean).join('\n');
      const data = { type: 'outbound', ...job };
      return { content: [{ type: 'text', text: lines }, { type: 'text', text: JSON.stringify(data) }] };
    }
  } catch (err) {
    const e = err;
    const status = e?.response?.status;
    if (status === 404) throw new McpError(ErrorCode.InvalidParams, `Fax not found: ${id}`);
    if (status === 401) throw new McpError(ErrorCode.InvalidParams, 'Invalid API key or authentication failed');
    throw new McpError(ErrorCode.InternalError, `Fax API error${status ? ' (' + status + ')' : ''}: ${e?.response?.statusText || (err instanceof Error ? err.message : 'Unknown error')}`);
  }
}

export async function handleListInboundTool(args) {
  const { limit = 20, cursor } = args || {};
  const data = await apiListInbound({ limit, cursor });
  const summary = Array.isArray(data?.items) ? data.items.map(x => `• ${x.id} from ${x.fr || x.from || 'unknown'} → ${x.to || 'unknown'} ${x.pages ? '(' + x.pages + 'p)' : ''}`).join('\n') : JSON.stringify(data);
  return { content: [{ type: 'text', text: `Inbound List\n\n${summary}` }, { type: 'text', text: JSON.stringify(data) }] };
}

export async function handleGetInboundPdfTool(args) {
  const { inboundId, asBase64 = false } = args || {};
  if (!inboundId) throw new McpError(ErrorCode.InvalidParams, 'inboundId is required');
  if (asBase64) {
    const { buffer } = await apiDownloadInboundPdf(inboundId);
    const b64 = buffer.toString('base64');
    return { content: [{ type: 'text', text: b64 }] };
  } else {
    // Return a MCP resource reference that clients can read via ReadResource
    const uri = `faxbot:inbound/${inboundId}/pdf`;
    return { content: [{ type: 'resource', resource: { uri, mimeType: 'application/pdf' } }] };
  }
}

export default { faxTools, handleSendFaxTool, handleGetFaxStatusTool, handleGetFaxTool, handleListInboundTool, handleGetInboundPdfTool };

export default { faxTools, handleSendFaxTool, handleGetFaxStatusTool };
