#!/usr/bin/env node
// Minimal WebSocket server for MCP-like tool access (dev-only baseline)
// Not a full MCP WebSocket transport; exposes simple JSON methods:
//   { method: 'list_tools' }
//   { method: 'call_tool', name: 'send_fax', arguments: { ... } }

import { WebSocketServer } from 'ws';
import { faxTools, handleSendFaxTool, handleGetFaxStatusTool, handleGetFaxTool, handleListInboundTool, handleGetInboundPdfTool } from '../tools/fax-tools.js';
import MCPToolRegistry from '../plugins/tool_registry.js';
import StatusPlugin from '../plugins/examples/status_plugin.js';

const port = parseInt(process.env.MCP_WS_PORT || '3004', 10);
const API_KEY = process.env.MCP_WS_API_KEY || process.env.API_KEY || '';

function listAllTools(registry) {
  const pluginTools = registry.getAllTools().map(t => ({
    name: `${t.pluginId}_${t.name}`,
    description: t.description || 'Plugin tool',
    inputSchema: t.parameters || { type: 'object' },
  }));
  return [...faxTools, ...pluginTools];
}

async function callTool(name, args, registry) {
  if (registry.has(name)) {
    return await registry.executeTool(name, args || {});
  }
  switch (name) {
    case 'send_fax':
      return (await handleSendFaxTool(args)).content?.[0]?.text || 'ok';
    case 'get_fax_status':
      return (await handleGetFaxStatusTool(args)).content?.[0]?.text || 'ok';
    case 'get_fax':
      return (await handleGetFaxTool(args)).content?.[0]?.text || 'ok';
    case 'list_inbound':
      return (await handleListInboundTool(args)).content?.[0]?.text || 'ok';
    case 'get_inbound_pdf':
      return (await handleGetInboundPdfTool(args)).content?.[0]?.text || 'ok';
    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

function authOk(req) {
  if (!API_KEY) return true; // if not configured, allow (dev)
  const url = new URL(req.url, 'http://localhost');
  const qk = url.searchParams.get('key');
  const hdr = req.headers['x-api-key'];
  return qk === API_KEY || hdr === API_KEY;
}

const wss = new WebSocketServer({ port });

// Prepare plugin tools
const registry = new MCPToolRegistry();
registry.registerPlugin(new StatusPlugin());

wss.on('connection', (ws, req) => {
  if (!authOk(req)) {
    ws.close(1008, 'Unauthorized');
    return;
  }
  ws.send(JSON.stringify({ event: 'hello', transport: 'websocket', server: 'faxbot-mcp', version: '2.0.0' }));
  ws.on('message', async (data) => {
    try {
      const msg = JSON.parse(data.toString());
      const id = msg.id || null;
      if (msg.method === 'list_tools') {
        ws.send(JSON.stringify({ id, result: { tools: listAllTools(registry) } }));
        return;
      }
      if (msg.method === 'call_tool') {
        const out = await callTool(msg.name, msg.arguments || {}, registry);
        ws.send(JSON.stringify({ id, result: out }));
        return;
      }
      ws.send(JSON.stringify({ id, error: { code: -32601, message: 'Method not found' } }));
    } catch (err) {
      ws.send(JSON.stringify({ error: { code: -32700, message: (err?.message || 'Parse error') } }));
    }
  });
});

console.log(`Faxbot MCP WS on ws://localhost:${port} (auth: ${API_KEY ? 'key' : 'disabled'})`);

