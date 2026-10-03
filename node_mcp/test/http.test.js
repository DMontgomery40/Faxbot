import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { SignJWT, exportJWK } from 'jose';
import { Client, StreamableHTTPClientTransport } from '@modelcontextprotocol/client';
import { createHttpServer } from '../src/servers/http.js';
import { startFakeFaxbot } from './fake-faxbot.js';

const TOOLS = ['get_fax', 'get_fax_status', 'get_inbound_pdf', 'list_inbound', 'send_fax'];
const PDF_B64 = Buffer.from('%PDF-1.4 outbound').toString('base64').replace(/^(.{8})/, '$1\n'); // line-wrapped base64 is accepted

async function listen(options) {
  const server = createHttpServer({ allowedHosts: [], allowedOrigins: [], oauthIssuer: '', ...options });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { server, url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((resolve) => server.close(resolve)) };
}

async function exercise(url, headers, mode, caller) {
  const client = new Client({ name: `http-${caller}`, version: '1.0.0' }, { versionNegotiation: { mode } });
  await client.connect(new StreamableHTTPClientTransport(new URL(`${url}/mcp`), { requestInit: { headers } }));
  try {
    const { tools } = await client.listTools();
    return {
      version: client.getNegotiatedProtocolVersion(),
      tools,
      sent: await client.callTool({ name: 'send_fax', arguments: { to: '+15551234567', fileContent: PDF_B64, fileName: 'letter.pdf' } }),
      status: await client.callTool({ name: 'get_fax_status', arguments: { jobId: `status-${caller}` } }),
      details: await client.callTool({ name: 'get_fax', arguments: { id: 'a1b2c3' } }),
      inbound: await client.callTool({ name: 'list_inbound', arguments: { limit: 5 } }),
      pdf: await client.callTool({ name: 'get_inbound_pdf', arguments: { inboundId: 'a1b2c3', asBase64: true } }),
      resource: await client.readResource({ uri: 'faxbot://inbound/a1b2c3/pdf' }),
    };
  } finally {
    await client.close();
  }
}

test('each HTTP caller key is forwarded and API_KEY never is', async () => {
  const fake = await startFakeFaxbot();
  process.env.API_KEY = 'environment-key-must-not-be-used';
  const mcp = await listen({ baseUrl: fake.url });
  try {
    const [alice, bob] = await Promise.all([
      exercise(mcp.url, { 'X-API-Key': 'alice-key' }, 'auto', 'alice'),
      exercise(mcp.url, { Authorization: 'Bearer bob-key' }, 'legacy', 'bob'),
    ]);
    assert.equal(alice.version, '2026-07-28');
    assert.equal(bob.version, '2025-11-25');
    for (const [caller, key, result] of [['alice', 'alice-key', alice], ['bob', 'bob-key', bob]]) {
      assert.deepEqual(result.tools.map((tool) => tool.name).sort(), TOOLS);
      const send = result.tools.find((tool) => tool.name === 'send_fax');
      assert.deepEqual(send.inputSchema.required, ['to', 'fileContent', 'fileName']);
      assert.equal(send.inputSchema.properties.filePath, undefined, 'network callers cannot read server files');
      assert.deepEqual(result.sent.structuredContent, { id: `job-for-${key}`, status: 'queued' });
      assert.equal(result.status.structuredContent.id, `status-${caller}`);
      assert.equal(result.details.structuredContent.direction, 'inbound');
      assert.equal(result.details.structuredContent.fr, '+15550001111');
      assert.equal(result.inbound.structuredContent.items[0].id, 'a1b2c3');
      assert.ok(Buffer.from(result.pdf.content[0].resource.blob, 'base64').toString().startsWith('%PDF'));
      assert.ok(Buffer.from(result.resource.contents[0].blob, 'base64').toString().startsWith('%PDF'));
    }
  } finally {
    delete process.env.API_KEY;
    await mcp.close();
    await fake.close();
  }
  const statusKeys = Object.fromEntries(fake.state.requests.filter((r) => r.path.startsWith('/fax/status-')).map((r) => [r.path, r.key]));
  assert.deepEqual(statusKeys, { '/fax/status-alice': 'alice-key', '/fax/status-bob': 'bob-key' });
  assert.deepEqual([...new Set(fake.keys())].sort(), ['alice-key', 'bob-key']);
});

test('HTTP requests without a caller key or from an unlisted origin never reach Faxbot', async () => {
  const fake = await startFakeFaxbot();
  const mcp = await listen({ baseUrl: fake.url });
  const body = JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'get_fax_status', arguments: { jobId: 'x' } } });
  try {
    for (const headers of [{}, { 'X-API-Key': '' }, { Authorization: 'Bearer ' }]) {
      const response = await fetch(`${mcp.url}/mcp`, { method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body });
      assert.equal(response.status, 401);
      assert.equal(response.headers.get('www-authenticate'), 'Bearer');
    }
    const browser = await fetch(`${mcp.url}/mcp`, { method: 'POST', headers: { 'X-API-Key': 'k', Origin: 'https://evil.invalid' }, body });
    assert.equal(browser.status, 403);
    assert.equal((await (await fetch(`${mcp.url}/health`)).json()).status, 'ok');
  } finally {
    await mcp.close();
    await fake.close();
  }
  assert.deepEqual(fake.state.requests, []);
});

test('OAuth subjects map to their stored Faxbot key; unmapped subjects are refused', async () => {
  const fake = await startFakeFaxbot();
  const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  fake.state.jwks = { keys: [{ ...(await exportJWK(publicKey)), kid: 'kid-1', alg: 'RS256', use: 'sig' }] };
  const keysFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'faxbot-mcp-')), 'subject-keys.json');
  fs.writeFileSync(keysFile, JSON.stringify({ 'alice@example.test': 'alice-faxbot-key' }));
  const token = (subject) => new SignJWT({}).setProtectedHeader({ alg: 'RS256', kid: 'kid-1' }).setSubject(subject)
    .setIssuer(fake.url).setAudience('faxbot-mcp').setExpirationTime('5m').sign(privateKey);
  const mcp = await listen({ baseUrl: fake.url, oauthIssuer: fake.url, oauthAudience: 'faxbot-mcp', subjectKeysFile: keysFile,
    resourceUrl: 'https://mcp.example.test/mcp' });
  try {
    const result = await exercise(mcp.url, { Authorization: `Bearer ${await token('alice@example.test')}`, 'X-API-Key': 'ignored' }, 'auto', 'alice');
    assert.equal(result.status.structuredContent.id, 'status-alice');
    const unmapped = await fetch(`${mcp.url}/mcp`, { method: 'POST', headers: { Authorization: `Bearer ${await token('mallory@example.test')}` }, body: '{}' });
    assert.equal(unmapped.status, 403);
    const anonymous = await fetch(`${mcp.url}/mcp`, { method: 'POST', headers: { 'X-API-Key': 'raw-key' }, body: '{}' });
    assert.equal(anonymous.status, 401);
    assert.match(anonymous.headers.get('www-authenticate'), /resource_metadata="https:\/\/mcp\.example\.test\/mcp\/\.well-known\/oauth-protected-resource"/);
    // A client follows the advertised URL; serve it at that path behind this origin.
    const advertised = anonymous.headers.get('www-authenticate').match(/resource_metadata="([^"]+)"/)[1];
    const metadata = await fetch(`${mcp.url}${new URL(advertised).pathname}`);
    assert.equal(metadata.status, 200);
    assert.deepEqual((await metadata.json()).authorization_servers, [fake.url]);
  } finally {
    await mcp.close();
    await fake.close();
  }
  assert.deepEqual([...new Set(fake.keys())], ['alice-faxbot-key']);
});
