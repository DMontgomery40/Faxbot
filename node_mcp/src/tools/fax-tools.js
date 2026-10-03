// The Faxbot MCP tools and inbound PDF resource, shared by every Node transport.
// Tool names and arguments are the public contract: send_fax, get_fax_status,
// get_fax, list_inbound and get_inbound_pdf.
import fs from 'node:fs/promises';
import path from 'node:path';
import * as z from 'zod/v4';
import { McpServer, ResourceTemplate } from '@modelcontextprotocol/server';
import { createFaxClient } from '../shared/fax-client.js';

export const SERVER_INFO = { name: 'faxbot-mcp', version: '3.0.0' };
export const INBOUND_PDF_URI = 'faxbot://inbound/{inbound_id}/pdf';

const fileType = z.enum(['pdf', 'txt']).optional().describe('Override the type detected from fileName');
const FaxJob = z.object({
  id: z.string(),
  status: z.string(),
  to: z.string().optional(),
  pages: z.number().int().optional(),
  error: z.string().optional(),
  backend: z.string().optional(),
  created_at: z.string().optional(),
  updated_at: z.string().optional(),
});
const InboundFax = z.object({
  id: z.string(),
  status: z.string().optional(),
  fr: z.string().optional(),
  to: z.string().optional(),
  pages: z.number().int().optional(),
  backend: z.string().optional(),
  received_at: z.string().optional(),
});
const FaxDetails = InboundFax.extend({
  direction: z.enum(['outbound', 'inbound']),
  error: z.string().optional(),
  created_at: z.string().optional(),
  updated_at: z.string().optional(),
});

const write = { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true };
const read = { readOnlyHint: true, openWorldHint: true };

// Keep only fields the output schema declares, dropping nulls.
function pick(schema, data) {
  return Object.fromEntries(Object.keys(schema.shape)
    .filter((key) => data?.[key] !== null && data?.[key] !== undefined)
    .map((key) => [key, data[key]]));
}

function result(text, structuredContent) {
  return { content: [{ type: 'text', text }], structuredContent };
}

function detectType(name, override) {
  const type = override || { pdf: 'pdf', txt: 'txt' }[String(name).split('.').pop().toLowerCase()];
  if (!type) throw new Error("fileType must be 'pdf' or 'txt'");
  return type;
}

async function submit(client, to, name, buffer, type) {
  if (!buffer?.length) throw new Error('File content is empty');
  const job = await client.sendFax(to, buffer, name, detectType(name, type));
  return result(`Fax queued. Job ID: ${job.id}. Status: ${job.status}. Use get_fax_status to check progress.`,
    { id: job.id, status: job.status });
}

function decode(fileContent) {
  const clean = fileContent.replace(/\s+/g, ''); // allow line-wrapped base64
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(clean)) throw new Error('fileContent must be base64 encoded');
  return Buffer.from(clean, 'base64');
}

// Local file and URL reads are offered only where the caller owns the host (stdio).
export function faxToolDefinitions(client, { localFiles }) {
  const sendDescription = 'Send a fax to a phone number. Provide a PDF or TXT document as base64 fileContent with fileName.';
  const send = localFiles
    ? {
        config: {
          title: 'Send fax',
          description: `${sendDescription} Prefer filePath (a local PDF or TXT) or fileUrl.`,
          inputSchema: z.object({
            to: z.string().min(1).describe('Destination fax number, for example +15551234567'),
            filePath: z.string().optional().describe('Path to a local PDF or TXT file'),
            fileUrl: z.string().url().optional().describe('HTTP(S) URL of a PDF or TXT document'),
            fileContent: z.string().optional().describe('Base64 encoded PDF or TXT content'),
            fileName: z.string().optional().describe('File name, for example letter.pdf'),
            fileType,
          }),
        },
        async handler({ to, filePath, fileUrl, fileContent, fileName, fileType: type }) {
          if (filePath) {
            const resolved = path.resolve(filePath);
            const extension = path.extname(resolved).slice(1).toLowerCase();
            if (!['pdf', 'txt'].includes(extension)) throw new Error('filePath must point to a PDF or TXT file');
            return submit(client, to, path.basename(resolved), await fs.readFile(resolved), extension);
          }
          if (fileUrl) {
            const response = await fetch(fileUrl, { signal: AbortSignal.timeout(30000) });
            if (!response.ok) throw new Error(`fileUrl returned HTTP ${response.status}`);
            const contentType = (response.headers.get('content-type') || '').toLowerCase();
            const name = new URL(fileUrl).pathname.split('/').pop() || 'document';
            const kind = contentType.includes('pdf') || name.toLowerCase().endsWith('.pdf') ? 'pdf'
              : contentType.includes('text/plain') || name.toLowerCase().endsWith('.txt') ? 'txt' : null;
            if (!kind) throw new Error('fileUrl must return a PDF or plain text document');
            return submit(client, to, name, Buffer.from(await response.arrayBuffer()), kind);
          }
          if (!fileContent || !fileName) throw new Error('Provide filePath, fileUrl, or fileContent with fileName');
          return submit(client, to, fileName, decode(fileContent), type);
        },
      }
    : {
        config: {
          title: 'Send fax',
          description: sendDescription,
          inputSchema: z.object({
            to: z.string().min(1).describe('Destination fax number, for example +15551234567'),
            fileContent: z.string().min(1).describe('Base64 encoded PDF or TXT content'),
            fileName: z.string().min(1).describe('File name, for example letter.pdf'),
            fileType,
          }),
        },
        handler: ({ to, fileContent, fileName, fileType: type }) => submit(client, to, fileName, decode(fileContent), type),
      };

  return [
    { name: 'send_fax', config: { ...send.config, outputSchema: FaxJob, annotations: write }, handler: send.handler },
    {
      name: 'get_fax_status',
      config: {
        title: 'Fax status',
        description: 'Check the status of a fax sent with send_fax.',
        inputSchema: z.object({ jobId: z.string().min(1).describe('Job ID returned by send_fax') }),
        outputSchema: FaxJob,
        annotations: read,
      },
      async handler({ jobId }) {
        const job = await client.getFaxStatus(jobId);
        const lines = [`Job ID: ${job.id}`, `Status: ${job.status}`, `Recipient: ${job.to}`];
        if (job.pages) lines.push(`Pages: ${job.pages}`);
        if (job.error) lines.push(`Error: ${job.error}`);
        if (job.updated_at) lines.push(`Updated: ${job.updated_at}`);
        return result(lines.join('\n'), pick(FaxJob, job));
      },
    },
    {
      name: 'get_fax',
      config: {
        title: 'Fax details',
        description: 'Get details for a sent fax job id or a received (inbound) fax id.',
        inputSchema: z.object({ id: z.string().min(1).describe('Outbound job id or inbound fax id') }),
        outputSchema: FaxDetails,
        annotations: read,
      },
      async handler({ id }) {
        let data = null;
        if (!/^in_/i.test(id)) {
          try {
            data = { ...(await client.getFaxStatus(id)), direction: 'outbound' };
          } catch (error) {
            if (error?.status !== 404) throw error;
          }
        }
        data ??= { ...(await client.getInbound(id)), direction: 'inbound' };
        const details = pick(FaxDetails, data);
        return result(Object.entries(details).map(([key, value]) => `${key}: ${value}`).join('\n'), details);
      },
    },
    {
      name: 'list_inbound',
      config: {
        title: 'Received faxes',
        description: 'List recently received faxes (metadata only).',
        inputSchema: z.object({ limit: z.number().int().min(0).default(20).describe('Maximum number of faxes to return') }),
        outputSchema: z.object({ items: z.array(InboundFax) }),
        annotations: read,
      },
      async handler({ limit = 20 }) {
        const items = (await client.listInbound()).slice(0, limit).map((item) => pick(InboundFax, item));
        const lines = items.map((item) => `${item.id} from ${item.fr || 'unknown'} to ${item.to || 'unknown'}${item.pages ? ` (${item.pages} pages)` : ''}`);
        return result(lines.join('\n') || 'No received faxes.', { items });
      },
    },
    {
      name: 'get_inbound_pdf',
      config: {
        title: 'Received fax PDF',
        description: 'Get a received fax PDF. Returns a resource link by default; set asBase64 to embed the PDF in the result.',
        inputSchema: z.object({
          inboundId: z.string().min(1).describe('Inbound fax id'),
          asBase64: z.boolean().default(false).describe('Embed the PDF instead of returning a resource link'),
        }),
        annotations: read,
      },
      async handler({ inboundId, asBase64 = false }) {
        const uri = INBOUND_PDF_URI.replace('{inbound_id}', encodeURIComponent(inboundId));
        if (!asBase64) {
          return { content: [{ type: 'resource_link', uri, name: `${inboundId}.pdf`, mimeType: 'application/pdf' }] };
        }
        const { buffer } = await client.downloadInboundPdf(inboundId);
        return { content: [{ type: 'resource', resource: { uri, mimeType: 'application/pdf', blob: buffer.toString('base64') } }] };
      },
    },
  ];
}

export function buildServer({ baseUrl = process.env.FAX_API_URL || 'http://localhost:8080', apiKey = '', localFiles = false } = {}) {
  const client = createFaxClient({ baseUrl, apiKey });
  const server = new McpServer(SERVER_INFO, { capabilities: { tools: {}, resources: {} } });
  for (const tool of faxToolDefinitions(client, { localFiles })) {
    server.registerTool(tool.name, tool.config, tool.handler);
  }
  server.registerResource('inbound_pdf', new ResourceTemplate(INBOUND_PDF_URI, { list: undefined }),
    { title: 'Received fax PDF', mimeType: 'application/pdf' },
    async (uri, { inbound_id: inboundId }) => {
      const { buffer, contentType } = await client.downloadInboundPdf(decodeURIComponent(String(inboundId)));
      return { contents: [{ uri: uri.href, mimeType: contentType, blob: buffer.toString('base64') }] };
    });
  return server;
}
