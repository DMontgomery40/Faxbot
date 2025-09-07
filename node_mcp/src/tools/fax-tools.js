import { McpError, ErrorCode } from '@modelcontextprotocol/sdk/types.js';
import { sendFax as apiSendFax, getFaxStatus as apiGetFaxStatus } from '../shared/fax-client.js';
import fs from 'fs';
import path from 'path';

export const faxTools = [
  {
    name: 'send_fax',
    description: 'Send a fax to a recipient. Preferred: provide filePath to a local PDF. Fallback: base64 fileContent.',
    inputSchema: {
      type: 'object',
      properties: {
        to: { type: 'string', description: 'Fax number (e.g., +1234567890)' },
        filePath: { type: 'string', description: 'Absolute or relative path to PDF or TXT file (preferred)' },
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
  const { to, fileContent, fileName, filePath } = args || {};
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

export default { faxTools, handleSendFaxTool, handleGetFaxStatusTool };
