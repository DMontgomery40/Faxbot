const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const FaxbotClient = require('..');

async function startFake() {
  const state = { requests: [], faxStatus: 202 };
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks).toString();
    state.requests.push({ method: req.method, path: req.url, key: req.headers['x-api-key'], body });
    const reply = (status, data) => {
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(data));
    };
    if (req.method === 'GET' && req.url === '/health') return reply(200, { status: 'ok' });
    if (req.method === 'GET' && req.url === '/plugins') return reply(200, { items: [{ id: 'phaxio', enabled: true }] });
    if (req.method === 'POST' && req.url === '/fax') return reply(state.faxStatus, state.faxStatus === 202 ? { id: 'job-1', status: 'queued' } : { detail: 'Invalid phone number' });
    if (req.method === 'GET' && req.url === '/fax/missing') return reply(404, { detail: 'Fax job not found' });
    if (req.method === 'GET' && req.url.startsWith('/fax/')) return reply(200, { id: req.url.slice(5), status: 'SUCCESS' });
    if (req.method === 'PUT' && req.url === '/plugins/phaxio/config') return reply(200, { ok: true, changed: true });
    if (req.method === 'POST' && req.url === '/admin/plugins/http/install') return reply(200, { ok: true, id: JSON.parse(body).manifest.id });
    return reply(404, { detail: 'Not Found' });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { state, url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((resolve) => server.close(resolve)) };
}

test('sendFax posts multipart with the key; getStatus and checkHealth use the documented paths', async () => {
  const fake = await startFake();
  const document = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'faxbot-sdk-')), 'letter.pdf');
  fs.writeFileSync(document, '%PDF-1.4 synthetic');
  try {
    const client = new FaxbotClient(`${fake.url}/`, 'sdk-key');
    assert.deepEqual(await client.sendFax('+15551234567', document), { id: 'job-1', status: 'queued' });
    const post = fake.state.requests.find((r) => r.path === '/fax');
    assert.equal(post.key, 'sdk-key');
    assert.match(post.body, /name="to"\r\n\r\n\+15551234567/);
    assert.match(post.body, /%PDF-1.4 synthetic/);
    assert.deepEqual(await client.getStatus('job-1'), { id: 'job-1', status: 'SUCCESS' });
    await assert.rejects(client.getStatus('missing'), /404/);
    assert.equal(await client.checkHealth(), true);
    assert.equal(fake.state.requests.find((r) => r.path === '/health').key, undefined);
    fake.state.faxStatus = 400;
    await assert.rejects(client.sendFax('+1', document), /Bad Request \(400\): Invalid phone number/);
  } finally {
    await fake.close();
  }
});

test('plugins use live paths and the typed config patch', async () => {
  const fake = await startFake();
  try {
    const client = new FaxbotClient(fake.url, 'admin-key');
    await new Promise((resolve) => setTimeout(resolve, 50)); // constructor probes GET /plugins
    assert.deepEqual(await client.plugins.listPlugins(), [{ id: 'phaxio', enabled: true }]);
    await client.plugins.updatePluginConfig('phaxio', { api_key: 'new' }, { enabled: true, expectedRevisionId: 'rev-1' });
    const put = fake.state.requests.find((r) => r.method === 'PUT');
    assert.deepEqual(JSON.parse(put.body), { settings: { api_key: 'new' }, enabled: true, expected_revision_id: 'rev-1' });
    const installed = await client.plugins.installPlugin({ id: 'acme-fax', name: 'Acme' });
    assert.equal(installed.id, 'acme-fax');
    const install = fake.state.requests.find((r) => r.path === '/admin/plugins/http/install');
    assert.equal(install.key, 'admin-key');
    assert.deepEqual(JSON.parse(install.body), { manifest: { id: 'acme-fax', name: 'Acme' } });
    await assert.rejects(client.plugins.installPlugin('acme-fax'), TypeError);
    assert.ok(!fake.state.requests.some((r) => r.path === '/plugins/install'));
  } finally {
    await fake.close();
  }
});
