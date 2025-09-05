#!/usr/bin/env node

// HTTP transport for Faxbot MCP tools
// Exposes health, capabilities, and tool invocation over HTTP

const express = require('express');
const helmet = require('helmet');
const cors = require('cors');
const morgan = require('morgan');
const Joi = require('joi');
require('dotenv').config();

const { FaxMcpServer } = require('./mcp_server.js');
const { McpError, ErrorCode } = require('@modelcontextprotocol/sdk/types.js');

const app = express();
app.use(helmet());
app.use(cors());
app.use(express.json({ limit: '10mb' }));
app.use(morgan('dev'));

// Instantiate the MCP logic holder (but do not start stdio transport)
const mcp = new FaxMcpServer();

// Tool definitions (keep in sync with mcp_server.js)
function listTools() {
  return {
    tools: [
      {
        name: 'send_fax',
        description:
          'Send a fax to a recipient using T.38 protocol via Asterisk or cloud provider. Supports PDF and TXT files.',
        inputSchema: {
          type: 'object',
          properties: {
            to: {
              type: 'string',
              description:
                'The fax number to send to (e.g., "+1234567890" or "555-1234")',
            },
            fileContent: {
              type: 'string',
              description: 'Base64 encoded file content (PDF or plain text)',
            },
            fileName: {
              type: 'string',
              description: 'Name of the file being sent (e.g., "document.pdf")',
            },
            fileType: {
              type: 'string',
              enum: ['pdf', 'txt'],
              description: 'Type of file being sent (pdf or txt)',
            },
          },
          required: ['to', 'fileContent', 'fileName'],
        },
      },
      {
        name: 'get_fax_status',
        description: 'Check the status of a previously sent fax job',
        inputSchema: {
          type: 'object',
          properties: {
            jobId: {
              type: 'string',
              description: 'The job ID returned from send_fax',
            },
          },
          required: ['jobId'],
        },
      },
    ],
  };
}

// Health endpoint
app.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    transport: 'http',
    server: 'faxbot-mcp',
    version: '2.0.0',
    apiUrl: mcp.apiUrl,
  });
});

// Capabilities (tools) endpoint
app.get('/mcp/capabilities', (req, res) => {
  res.json(listTools());
});

// Optional: list tools alias
app.get('/mcp/tools', (req, res) => {
  res.json(listTools());
});

// Tool invocation endpoint
const callSchema = Joi.object({
  name: Joi.string().required(),
  arguments: Joi.object().default({}),
});

app.post('/mcp/call', async (req, res) => {
  const { error, value } = callSchema.validate(req.body || {});
  if (error) {
    return res.status(400).json({
      error: { code: 'InvalidParams', message: error.message },
    });
  }

  const { name, arguments: args } = value;

  try {
    switch (name) {
      case 'send_fax': {
        const result = await mcp.handleSendFax(args);
        return res.json(result);
      }
      case 'get_fax_status': {
        const result = await mcp.handleGetFaxStatus(args);
        return res.json(result);
      }
      default:
        throw new McpError(ErrorCode.MethodNotFound, `Unknown tool: ${name}`);
    }
  } catch (err) {
    if (err instanceof McpError) {
      const code = err.code || ErrorCode.InternalError;
      const status =
        code === ErrorCode.InvalidParams
          ? 400
          : code === ErrorCode.MethodNotFound
          ? 404
          : 500;
      return res.status(status).json({ error: { code, message: err.message } });
    }

    console.error('HTTP MCP call error:', err);
    return res
      .status(500)
      .json({ error: { code: 'InternalError', message: 'Unexpected error' } });
  }
});

// Start server
const port = parseInt(process.env.MCP_HTTP_PORT || '3001', 10);
app.listen(port, () => {
  console.log(`Faxbot MCP HTTP server listening on http://localhost:${port}`);
  console.log(`Upstream API: ${mcp.apiUrl}`);
});

