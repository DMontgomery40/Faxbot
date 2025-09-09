#!/usr/bin/env node
import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import morgan from 'morgan';
import dotenv from 'dotenv';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { isInitializeRequest } from '@modelcontextprotocol/sdk/types.js';
import { buildServer } from './stdio.js';

dotenv.config();

const app = express();
app.use(helmet());
app.use(express.json({ limit: '16mb' }));
const corsOrigin = process.env.MCP_HTTP_CORS_ORIGIN || '*';
app.use(
  cors({ origin: corsOrigin, exposedHeaders: ['Mcp-Session-Id'], allowedHeaders: ['Content-Type', 'mcp-session-id', 'authorization', 'x-api-key'] })
);
app.use(morgan('dev'));

app.get('/health', (_req, res) => {
  res.json({ status: 'ok', transport: 'streamable-http', server: 'faxbot-mcp', version: '2.0.0' });
});

const sessions = Object.create(null); // sessionId -> { transport, server }
const MCP_HTTP_API_KEY = process.env.MCP_HTTP_API_KEY || '';

function checkAuth(req, res, next) {
  if (!MCP_HTTP_API_KEY) {
    // Explicitly require configuration for network-exposed server
    return res.status(401).json({ error: 'Unauthorized: MCP_HTTP_API_KEY not configured' });
  }
  const hdr = (req.headers['authorization'] || '').trim();
  const x = (req.headers['x-api-key'] || '').toString().trim();
  let ok = false;
  if (hdr && hdr.startsWith('Bearer ')) {
    const token = hdr.slice(7).trim();
    ok = token === MCP_HTTP_API_KEY;
  } else if (x) {
    ok = x === MCP_HTTP_API_KEY;
  }
  if (!ok) return res.status(401).json({ error: 'Unauthorized' });
  return next();
}

async function initServerWithTransport(transport) {
  const server = buildServer();
  transport.onclose = () => {
    if (transport.sessionId && sessions[transport.sessionId]) delete sessions[transport.sessionId];
    try {
      server.close();
    } catch (_) {}
  };
  await server.connect(transport);
  return server;
}

app.post('/mcp', checkAuth, async (req, res) => {
  try {
    const sessionId = req.headers['mcp-session-id'];
    const session = sessionId ? sessions[sessionId] : undefined;
    if (!session) {
      if (!isInitializeRequest(req.body)) {
        return res.status(400).json({
          jsonrpc: '2.0',
          error: { code: -32000, message: 'Bad Request: No valid session ID provided' },
          id: null,
        });
      }
      const transport = new StreamableHTTPServerTransport({});
      const server = await initServerWithTransport(transport);
      // Process initialize request; the transport will assign sessionId and set header
      await transport.handleRequest(req, res, req.body);
      if (transport.sessionId) {
        sessions[transport.sessionId] = { transport, server };
      }
      return;
    }
    await session.transport.handleRequest(req, res, req.body);
  } catch (err) {
    console.error('MCP HTTP POST error:', err);
    if (!res.headersSent) res.status(500).json({ jsonrpc: '2.0', error: { code: -32603, message: 'Internal server error' }, id: null });
  }
});

app.get('/mcp', checkAuth, async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  const session = sessionId ? sessions[sessionId] : undefined;
  if (!session) return res.status(400).send('Invalid or missing session ID');
  await session.transport.handleRequest(req, res);
});

app.delete('/mcp', checkAuth, async (req, res) => {
  const sessionId = req.headers['mcp-session-id'];
  const session = sessionId ? sessions[sessionId] : undefined;
  if (!session) return res.status(400).send('Invalid or missing session ID');
  await session.transport.handleRequest(req, res);
});

export async function start() {
  const port = parseInt(process.env.MCP_HTTP_PORT || '3001', 10);
  app.listen(port, () => {
    console.log(`Faxbot MCP HTTP (streamable) on http://localhost:${port}`);
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  start().catch((err) => {
    console.error('Failed to start HTTP server:', err);
    process.exit(1);
  });
}
