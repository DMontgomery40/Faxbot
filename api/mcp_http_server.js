#!/usr/bin/env node

// MCP Streamable HTTP transport for Faxbot
// Implements the standard /mcp POST (requests), GET (SSE notifications), and DELETE (session end)

const express = require('express');
const helmet = require('helmet');
const cors = require('cors');
const morgan = require('morgan');
require('dotenv').config();

const { StreamableHTTPServerTransport } = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
const { isInitializeRequest } = require('@modelcontextprotocol/sdk/types.js');
const { FaxMcpServer } = require('./mcp_server.js');

const app = express();
app.use(helmet());
app.use(express.json({ limit: '10mb' }));
// Allow browser-based MCP clients to read session header
app.use(cors({
  origin: '*',
  exposedHeaders: ['Mcp-Session-Id'],
  allowedHeaders: ['Content-Type', 'mcp-session-id'],
}));
app.use(morgan('dev'));

// Health (optional convenience)
app.get('/health', (_req, res) => {
  res.json({ status: 'ok', transport: 'streamable-http', server: 'faxbot-mcp', version: '2.0.0' });
});

// Session store
const sessions = Object.create(null); // sessionId -> { transport, server }

// Helper to create a new Faxbot MCP server and connect it to a transport
async function initServerWithTransport(transport) {
  const fax = new FaxMcpServer();
  const server = fax.server; // underlying MCP server instance
  // Clean up when transport closes
  transport.onclose = () => {
    if (transport.sessionId && sessions[transport.sessionId]) {
      delete sessions[transport.sessionId];
    }
    try { server.close(); } catch (_) {}
  };
  await server.connect(transport);
  return server;
}

// POST /mcp - client->server JSON requests
app.post('/mcp', async (req, res) => {
  try {
    const sessionId = req.headers['mcp-session-id'];
    let session = sessionId ? sessions[sessionId] : undefined;

    if (!session) {
      // No session: only allow if this is an initialize request
      if (!isInitializeRequest(req.body)) {
        return res.status(400).json({
          jsonrpc: '2.0',
          error: { code: -32000, message: 'Bad Request: No valid session ID provided' },
          id: null,
        });
      }
      // Create transport with auto session ID
      const transport = new StreamableHTTPServerTransport({});
      const server = await initServerWithTransport(transport);
      // After connect, transport.sessionId should be set
      if (!transport.sessionId) {
        return res.status(500).json({ jsonrpc: '2.0', error: { code: -32603, message: 'Failed to initialize session' }, id: null });
      }
      sessions[transport.sessionId] = { transport, server };
      await transport.handleRequest(req, res, req.body);
      return;
    }

    // Existing session: forward request
    await session.transport.handleRequest(req, res, req.body);
  } catch (err) {
    console.error('MCP HTTP POST error:', err);
    if (!res.headersSent) {
      res.status(500).json({ jsonrpc: '2.0', error: { code: -32603, message: 'Internal server error' }, id: null });
    }
  }
});

// GET /mcp - SSE notifications
app.get('/mcp', async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  const session = sessionId ? sessions[sessionId] : undefined;
  if (!session) {
    res.status(400).send('Invalid or missing session ID');
    return;
  }
  await session.transport.handleRequest(req, res);
});

// DELETE /mcp - end session
app.delete('/mcp', async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  const session = sessionId ? sessions[sessionId] : undefined;
  if (!session) {
    res.status(400).send('Invalid or missing session ID');
    return;
  }
  await session.transport.handleRequest(req, res);
});

// Start server
const port = parseInt(process.env.MCP_HTTP_PORT || '3001', 10);
app.listen(port, () => {
  console.log(`Faxbot MCP HTTP (streamable) on http://localhost:${port}`);
});
