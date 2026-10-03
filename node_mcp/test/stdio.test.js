import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';
import { startFakeFaxbot } from './fake-faxbot.js';

const STDIO = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src', 'servers', 'stdio.js');
const TOOLS = ['get_fax', 'get_fax_status', 'get_inbound_pdf', 'list_inbound', 'send_fax'];

test('stdio lists the documented tools and uses its single configured key', async () => {
  const fake = await startFakeFaxbot();
  const document = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'faxbot-mcp-')), 'note.txt');
  fs.writeFileSync(document, 'hello fax');
  const client = new Client({ name: 'stdio-test', version: '1.0.0' }, { versionNegotiation: { mode: 'auto' } });
  try {
    await client.connect(new StdioClientTransport({
      command: process.execPath, args: [STDIO],
      env: { PATH: process.env.PATH, FAX_API_URL: fake.url, API_KEY: 'stdio-integration-key' },
    }));
    assert.equal(client.getNegotiatedProtocolVersion(), '2026-07-28');
    const { tools } = await client.listTools();
    assert.deepEqual(tools.map((tool) => tool.name).sort(), TOOLS);
    const send = tools.find((tool) => tool.name === 'send_fax');
    assert.ok(send.inputSchema.properties.filePath && send.inputSchema.properties.fileUrl);
    assert.deepEqual(send.outputSchema.required, ['id', 'status', 'operationId']);
    assert.match(send.inputSchema.properties.operationId.description, /same number and document/);
    assert.deepEqual(send.inputSchema.required, ['to']);
    assert.equal(send.annotations.idempotentHint, false);

    const sent = await client.callTool({ name: 'send_fax', arguments: { to: '+15551234567', filePath: document } });
    assert.deepEqual(sent.structuredContent, { id: 'job-1-for-stdio-integration-key', status: 'queued', operationId: fake.posts()[0].operation });
    const bare = await client.callTool({ name: 'list_inbound', arguments: {} });
    fake.state.inboundEnvelope = true;
    const wrapped = await client.callTool({ name: 'list_inbound', arguments: {} });
    assert.deepEqual(bare.structuredContent, wrapped.structuredContent);
    assert.equal(bare.structuredContent.items[0].fr, '+15550001111');
    const missing = await client.callTool({ name: 'get_fax_status', arguments: { jobId: 'missing' } });
    assert.equal(missing.isError, true);
    assert.match(missing.content[0].text, /404/);
  } finally {
    await client.close();
    await fake.close();
  }
  assert.ok(fake.keys().length >= 4);
  assert.ok(fake.keys().every((key) => key === 'stdio-integration-key'));
  assert.ok(fake.state.requests[0].body.includes('hello fax'));
});

test('stdio stdout carries only JSON-RPC messages', async () => {
  const fake = await startFakeFaxbot();
  const child = spawn(process.execPath, [STDIO], { env: { PATH: process.env.PATH, FAX_API_URL: fake.url, API_KEY: 'k' } });
  let stdout = '';
  child.stdout.on('data', (chunk) => { stdout += chunk; });
  const lines = () => stdout.split('\n').filter(Boolean);
  const waitFor = async (count) => {
    for (let i = 0; i < 200 && lines().length < count; i += 1) await new Promise((resolve) => setTimeout(resolve, 25));
  };
  const send = (message) => child.stdin.write(`${JSON.stringify(message)}\n`);
  try {
    send({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'raw', version: '1' } } });
    await waitFor(1);
    send({ jsonrpc: '2.0', method: 'notifications/initialized' });
    send({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
    send({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'get_fax_status', arguments: { jobId: 'missing' } } });
    await waitFor(3);
  } finally {
    child.stdin.end();
    await new Promise((resolve) => child.on('close', resolve));
    await fake.close();
  }
  const replies = lines().map((line) => JSON.parse(line));
  assert.ok(replies.every((reply) => reply.jsonrpc === '2.0'));
  assert.deepEqual(replies.map((reply) => reply.id), [1, 2, 3]);
  assert.deepEqual(replies[1].result.tools.map((tool) => tool.name).sort(), TOOLS);
  assert.equal(replies[2].result.isError, true);
});
