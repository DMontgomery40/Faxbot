#!/usr/bin/env node
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ErrorCode,
  GetPromptRequestSchema,
  ListPromptsRequestSchema,
  ListToolsRequestSchema,
  ReadResourceRequestSchema,
  McpError,
} from '@modelcontextprotocol/sdk/types.js';
import { downloadInboundPdf as apiDownloadInboundPdf } from '../shared/fax-client.js';
import { faxTools, handleSendFaxTool, handleGetFaxStatusTool, handleGetFaxTool, handleListInboundTool, handleGetInboundPdfTool } from '../tools/fax-tools.js';
import { listPrompts } from '../prompts/index.js';

function buildServer() {
  const server = new Server(
    { name: 'faxbot-mcp', version: '2.0.0' },
    { capabilities: { tools: {}, prompts: {} } }
  );

  // Tools
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: faxTools }));
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    switch (name) {
      case 'send_fax':
        return await handleSendFaxTool(args);
      case 'get_fax_status':
        return await handleGetFaxStatusTool(args);
      case 'get_fax':
        return await handleGetFaxTool(args);
      case 'list_inbound':
        return await handleListInboundTool(args);
      case 'get_inbound_pdf':
        return await handleGetInboundPdfTool(args);
      default:
        throw new McpError(ErrorCode.MethodNotFound, `Unknown tool: ${name}`);
    }
  });

  // Prompts
  server.setRequestHandler(ListPromptsRequestSchema, async () => ({ prompts: listPrompts() }));
  server.setRequestHandler(GetPromptRequestSchema, async (_request) => {
    throw new McpError(ErrorCode.MethodNotFound, 'No prompts are defined');
  });

  // Resources: support reading inbound PDFs via a custom URI scheme
  server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
    const uri = request.params?.uri || '';
    if (typeof uri !== 'string') throw new McpError(ErrorCode.InvalidParams, 'Invalid uri');
    // faxbot:inbound/<id>/pdf
    const m = uri.match(/^faxbot:inbound\/(.+)\/pdf$/i);
    if (!m) throw new McpError(ErrorCode.InvalidParams, `Unsupported resource uri: ${uri}`);
    const inboundId = m[1];
    const { buffer, contentType } = await apiDownloadInboundPdf(inboundId);
    return {
      contents: [
        {
          uri,
          mimeType: contentType || 'application/pdf',
          blob: buffer.toString('base64'),
        },
      ],
    };
  });

  return server;
}

async function main() {
  const transport = new StdioServerTransport();
  const server = buildServer();
  console.log('Starting Faxbot Node MCP (stdio)');
  await server.connect(transport);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    console.error('Failed to start stdio server:', err);
    process.exit(1);
  });
}

export { buildServer };
