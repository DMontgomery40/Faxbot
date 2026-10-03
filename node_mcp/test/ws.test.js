import { test } from 'node:test';
import assert from 'node:assert/strict';
import WebSocket from 'ws';
import { startWs } from '../src/servers/ws.js';
import { startFakeFaxbot } from './fake-faxbot.js';

function connect(url, headers) {
  return new Promise((resolve) => {
    const ws = new WebSocket(url, { headers });
    ws.on('open', () => resolve({ ws, status: 101 }));
    ws.on('unexpected-response', (_req, res) => resolve({ status: res.statusCode }));
    ws.on('error', () => {});
  });
}

async function serve(options) {
  const wss = startWs({ port: 0, ...options });
  await new Promise((resolve) => wss.on('listening', resolve));
  return { wss, url: `ws://127.0.0.1:${wss.address().port}` };
}

test('ws refuses every connection when no key is configured', async () => {
  const { wss, url } = await serve({ gateKey: '' });
  try {
    assert.equal((await connect(url, {})).status, 503);
    assert.equal((await connect(url, { 'X-API-Key': 'anything' })).status, 503);
  } finally {
    wss.close();
  }
});

test('ws accepts the configured key only from a header and forwards API_KEY', async () => {
  const fake = await startFakeFaxbot();
  const { wss, url } = await serve({ gateKey: 'ws-gate-key', baseUrl: fake.url, apiKey: 'ws-integration-key' });
  try {
    assert.equal((await connect(`${url}/?key=ws-gate-key`, {})).status, 400);
    assert.equal((await connect(url, { 'X-API-Key': 'wrong' })).status, 401);
    assert.equal((await connect(url, {})).status, 401);
    const { ws, status } = await connect(url, { Authorization: 'Bearer ws-gate-key' });
    assert.equal(status, 101);
    const reply = new Promise((resolve) => ws.once('message', (data) => resolve(JSON.parse(data.toString()))));
    ws.send(JSON.stringify({ id: 1, method: 'call_tool', name: 'get_fax_status', arguments: { jobId: 'ws-job' } }));
    const { result } = await reply;
    assert.equal(result.structuredContent.id, 'ws-job');
    ws.close();
  } finally {
    wss.close();
    await fake.close();
  }
  assert.deepEqual(fake.keys(), ['ws-integration-key']);
});
