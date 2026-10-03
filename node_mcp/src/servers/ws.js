#!/usr/bin/env node
// Development WebSocket bridge to the Faxbot tools (not an MCP transport).
// Messages: { id, method: 'list_tools' } and { id, method: 'call_tool', name, arguments }.
//
// A connection must present MCP_WS_API_KEY (or API_KEY when that is unset) in an
// X-API-Key or "Authorization: Bearer" header. Without a configured key every
// connection is refused; keys in the URL are refused. Tool calls use API_KEY as
// one integration identity, like the stdio server.
import { timingSafeEqual } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { WebSocketServer } from 'ws';
import * as z from 'zod/v4';
import { createFaxClient } from '../shared/fax-client.js';
import { faxToolDefinitions } from '../tools/fax-tools.js';

function sameSecret(presented, expected) {
  const a = Buffer.from(presented);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

export function authorizeUpgrade(req, gateKey) {
  const url = new URL(req.url || '/', 'http://localhost');
  if (url.searchParams.has('key')) return { ok: false, code: 400, message: 'Send the key in a header, not the URL' };
  if (!gateKey) return { ok: false, code: 503, message: 'WebSocket access is not configured' };
  const authorization = String(req.headers.authorization || '');
  const presented = (/^bearer /i.test(authorization) ? authorization.slice(7).trim() : '') || String(req.headers['x-api-key'] || '');
  return presented && sameSecret(presented, gateKey) ? { ok: true } : { ok: false, code: 401, message: 'Unauthorized' };
}

export function startWs({
  port = parseInt(process.env.MCP_WS_PORT || '3004', 10),
  gateKey = process.env.MCP_WS_API_KEY || process.env.API_KEY || '',
  baseUrl = process.env.FAX_API_URL || 'http://localhost:8080',
  apiKey = process.env.API_KEY || '',
} = {}) {
  const tools = new Map(faxToolDefinitions(createFaxClient({ baseUrl, apiKey }), { localFiles: false }).map((tool) => [tool.name, tool]));
  const wss = new WebSocketServer({
    port,
    verifyClient: (info, done) => {
      const decision = authorizeUpgrade(info.req, gateKey);
      done(decision.ok, decision.code, decision.message);
    },
  });
  wss.on('connection', (ws) => {
    ws.on('message', async (data) => {
      let id = null;
      try {
        const msg = JSON.parse(data.toString());
        id = msg.id ?? null;
        if (msg.method === 'list_tools') {
          const list = [...tools.values()].map(({ name, config }) => ({ name, description: config.description, inputSchema: z.toJSONSchema(config.inputSchema) }));
          ws.send(JSON.stringify({ id, result: { tools: list } }));
          return;
        }
        if (msg.method === 'call_tool') {
          const tool = tools.get(msg.name);
          if (!tool) throw new Error(`Unknown tool: ${msg.name}`);
          ws.send(JSON.stringify({ id, result: await tool.handler(tool.config.inputSchema.parse(msg.arguments || {})) }));
          return;
        }
        ws.send(JSON.stringify({ id, error: { code: -32601, message: 'Method not found' } }));
      } catch (err) {
        ws.send(JSON.stringify({ id, error: { code: -32000, message: err?.message || 'Error' } }));
      }
    });
  });
  if (!gateKey) console.error('Faxbot MCP WS: set MCP_WS_API_KEY or API_KEY; all connections are refused until then.');
  return wss;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const wss = startWs();
  wss.on('listening', () => console.error(`Faxbot MCP WS on ws://localhost:${wss.address().port}`));
}
