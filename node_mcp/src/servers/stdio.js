#!/usr/bin/env node
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ErrorCode,
  GetPromptRequestSchema,
  ListPromptsRequestSchema,
  ListToolsRequestSchema,
  McpError,
} from '@modelcontextprotocol/sdk/types.js';
import { faxTools, handleSendFaxTool, handleGetFaxStatusTool, handleFaxbotPdfTool } from '../tools/fax-tools.js';
import { listPrompts } from '../prompts/index.js';
import { extractTextFromPDF } from '../shared/pdf-extractor.js';
import { sendFax } from '../shared/fax-client.js';
import fs from 'fs';
import path from 'path';

const MAX_TEXT_SIZE = parseInt(process.env.MAX_TEXT_SIZE || '100000', 10);

async function executeFaxbotPdf(args) {
  const { pdf_path, to, header_text } = args || {};
  if (!pdf_path || !to) {
    throw new McpError(ErrorCode.InvalidParams, 'pdf_path and to are required');
  }
  const resolved = path.isAbsolute(pdf_path) ? pdf_path : path.resolve(process.cwd(), pdf_path);
  try {
    await fs.promises.access(resolved, fs.constants.R_OK);
  } catch {
    throw new McpError(ErrorCode.InvalidParams, `File not found or unreadable: ${resolved}`);
  }

  const extracted = await extractTextFromPDF(resolved);
  let text = extracted;
  if (header_text && typeof header_text === 'string' && header_text.trim()) {
    text = `${header_text.trim()}\n\n${text}`;
  }

  let notice = '';
  if (text.length > MAX_TEXT_SIZE) {
    notice = `Warning: Extracted text (${text.length} bytes) exceeds MAX_TEXT_SIZE (${MAX_TEXT_SIZE}). Truncating.\n\n`;
    text = text.slice(0, MAX_TEXT_SIZE);
  }

  const result = await sendFax(to, text, 'txt', 'extracted.txt');
  const message = `Faxbot workflow initiated. PDF "${resolved}" extracted to ${extracted.length} characters. ${notice ? notice : ''}Fax job ID: ${result.id}`;
  return { message, jobId: result.id };
}

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
      case 'faxbot_pdf':
        return await handleFaxbotPdfTool(args);
      default:
        throw new McpError(ErrorCode.MethodNotFound, `Unknown tool: ${name}`);
    }
  });

  // Prompts
  server.setRequestHandler(ListPromptsRequestSchema, async () => ({ prompts: listPrompts() }));
  server.setRequestHandler(GetPromptRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    if (name === 'faxbot_pdf') {
      const { pdf_path, to, header_text } = (args || {});
      const instruction = `Plan: Extract text from PDF at ${pdf_path} and send as TXT fax to ${to}.\n` +
        `Action: Call the tool 'faxbot_pdf' with the same arguments to execute. Optional header_text: ${header_text || '(none)'}\n` +
        `Note: This prompt only describes the plan and does not send the fax itself.`;
      return {
        messages: [
          { role: 'user', content: { type: 'text', text: instruction } },
        ],
      };
    }
    throw new McpError(ErrorCode.MethodNotFound, `Unknown prompt: ${name}`);
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
