#!/usr/bin/env node

// MCP SSE transport for Faxbot with OAuth2 (JWT) Bearer authentication.
// Requires valid JWTs issued by an OIDC provider and verified via JWKS.

require('dotenv').config();
const express = require('express');
const helmet = require('helmet');
const cors = require('cors');
const morgan = require('morgan');
const { jwtVerify, createRemoteJWKSet } = require('jose');

const { SSEServerTransport } = require('@modelcontextprotocol/sdk/server/sse.js');
const { FaxMcpServer } = require('./mcp_server.js');

const app = express();
app.use(helmet());
app.use(express.json({ limit: '10mb' }));
app.use(cors({ origin: '*', exposedHeaders: ['Mcp-Session-Id'] }));
app.use(morgan('dev'));

// Health route
app.get('/health', (_req, res) => {
  res.json({ status: 'ok', transport: 'sse', server: 'faxbot-mcp', version: '2.0.0' });
});

// OAuth2 / JWT verification
const issuer = (process.env.OAUTH_ISSUER || '').replace(/\/$/, '');
const audience = process.env.OAUTH_AUDIENCE || '';
const jwksUrl = process.env.OAUTH_JWKS_URL || (issuer ? `${issuer}/.well-known/jwks.json` : '');

if (!issuer || !audience || !jwksUrl) {
  console.warn('[mcp-sse] OAuth2 env not fully configured. Set OAUTH_ISSUER, OAUTH_AUDIENCE, and optionally OAUTH_JWKS_URL.');
}

let JWKS; // initialized lazily on first request

async function authenticate(req, res, next) {
  try {
    const auth = req.headers['authorization'] || '';
    const [scheme, token] = auth.split(' ');
    if (scheme !== 'Bearer' || !token) {
      return res.status(401).json({ error: 'Unauthorized' });
    }
    if (!JWKS) {
      JWKS = createRemoteJWKSet(new URL(jwksUrl));
    }
    const { payload } = await jwtVerify(token, JWKS, {
      issuer,
      audience,
    });
    req.user = payload;
    return next();
  } catch (err) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
}

// In-memory session store: sessionId -> { transport, server }
const sessions = Object.create(null);

async function initServerWithTransport(transport) {
  const fax = new FaxMcpServer();
  const server = fax.server;
  transport.onclose = () => {
    if (transport.sessionId && sessions[transport.sessionId]) {
      delete sessions[transport.sessionId];
    }
    try { server.close(); } catch (_) {}
  };
  await server.connect(transport);
  return server;
}

// GET /sse - establish SSE session
app.get('/sse', authenticate, async (req, res) => {
  try {
    const existingId = req.query.sessionId || req.headers['mcp-session-id'];
    if (existingId && sessions[existingId]) {
      return sessions[existingId].transport.handleRequest(req, res);
    }
    const transport = new SSEServerTransport({});
    const server = await initServerWithTransport(transport);
    if (!transport.sessionId) {
      return res.status(500).json({ error: 'Failed to initialize session' });
    }
    sessions[transport.sessionId] = { transport, server };
    return transport.handleRequest(req, res);
  } catch (err) {
    console.error('SSE /sse error:', err);
    if (!res.headersSent) {
      res.status(500).json({ error: 'Internal server error' });
    }
  }
});

// POST /messages - deliver client messages into a session
app.post('/messages', authenticate, async (req, res) => {
  try {
    const sessionId = req.query.sessionId || req.headers['mcp-session-id'] || req.body.sessionId;
    if (!sessionId || !sessions[sessionId]) {
      return res.status(400).json({ error: 'Invalid or missing sessionId' });
    }
    const session = sessions[sessionId];
    return session.transport.handlePostMessage(req, res);
  } catch (err) {
    console.error('SSE POST /messages error:', err);
    if (!res.headersSent) {
      res.status(500).json({ error: 'Internal server error' });
    }
  }
});

// DELETE /messages - close a session
app.delete('/messages', authenticate, async (req, res) => {
  try {
    const sessionId = req.query.sessionId || req.headers['mcp-session-id'];
    if (!sessionId || !sessions[sessionId]) {
      return res.status(400).json({ error: 'Invalid or missing sessionId' });
    }
    const session = sessions[sessionId];
    await session.transport.handleClose();
    delete sessions[sessionId];
    res.status(204).end();
  } catch (err) {
    console.error('SSE DELETE /messages error:', err);
    if (!res.headersSent) {
      res.status(500).json({ error: 'Internal server error' });
    }
  }
});

const port = parseInt(process.env.PORT || '3002', 10);
app.listen(port, () => {
  console.log(`Faxbot MCP SSE (OAuth2) on http://localhost:${port}`);
});

