const { test } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const FaxbotClient = require('..');

const CONFLICT = 'Idempotency-Key already belongs to a different fax request.';

// A fake Faxbot that honors Idempotency-Key per API key, like the server contract.
// Each accepted job is one provider submission. `plan` scripts the next POST /fax replies:
// 'accept' (default), 'drop' (accept, then close without answering), 'uncertain' (accept, then
// answer 503) or an HTTP status (answer it without accepting).
async function startFake() {
  const state = { requests: [], faxStatus: 202, plan: [], jobs: [], ledger: new Map() };
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const raw = Buffer.concat(chunks);
    const body = raw.toString();
    state.requests.push({ method: req.method, path: req.url, key: req.headers['x-api-key'], operation: req.headers['idempotency-key'], body });
    const reply = (status, data) => {
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(data));
    };
    if (req.method === 'GET' && req.url === '/health') return reply(200, { status: 'ok' });
    if (req.method === 'GET' && req.url === '/plugins') return reply(200, { items: [{ id: 'phaxio', enabled: true }] });
    if (req.method === 'POST' && req.url === '/fax') {
      if (state.faxStatus !== 202) return reply(state.faxStatus, { detail: 'Invalid phone number' });
      const action = state.plan.length ? state.plan.shift() : 'accept';
      if (typeof action === 'number') return reply(action, { detail: `synthetic ${action}` });
      const form = await new Response(raw, { headers: { 'content-type': req.headers['content-type'] } }).formData();
      const digest = crypto.createHash('sha256').update(Buffer.from(await form.get('file').arrayBuffer())).digest('hex');
      const request = JSON.stringify([form.get('to'), digest, form.get('queue_only')]);
      const operation = req.headers['idempotency-key'];
      const scope = `${req.headers['x-api-key']}\n${operation}`;
      let entry = operation === undefined ? undefined : state.ledger.get(scope);
      if (entry && entry.request !== request) return reply(409, { detail: CONFLICT });
      if (!entry) {
        entry = { request, job: { id: `job-${state.jobs.length + 1}`, status: 'queued' } };
        state.jobs.push(entry.job); // one provider submission
        if (operation !== undefined) state.ledger.set(scope, entry);
      }
      if (action === 'drop') return req.socket.destroy(); // the job exists but its response is lost
      if (action === 'uncertain') return reply(503, { detail: 'Fax acceptance is uncertain; retry with the same Idempotency-Key.' });
      return reply(202, entry.job);
    }
    if (req.method === 'GET' && req.url === '/fax/missing') return reply(404, { detail: 'Fax job not found' });
    if (req.method === 'GET' && req.url.startsWith('/fax/')) return reply(200, { id: req.url.slice(5), status: 'SUCCESS' });
    if (req.method === 'PUT' && req.url === '/plugins/phaxio/config') return reply(200, { ok: true, changed: true });
    if (req.method === 'POST' && req.url === '/admin/plugins/http/install') return reply(200, { ok: true, id: JSON.parse(body).manifest.id });
    return reply(404, { detail: 'Not Found' });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    state,
    url: `http://127.0.0.1:${server.address().port}`,
    posts: () => state.requests.filter((r) => r.path === '/fax'),
    close: () => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); }),
  };
}

function writeDocument(name = 'letter.pdf', content = '%PDF-1.4 synthetic') {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'faxbot-sdk-')), name);
  fs.writeFileSync(file, content);
  return file;
}

async function withFake(run) {
  const fake = await startFake();
  try {
    await run(fake);
  } finally {
    await fake.close();
  }
}

test('sendFax posts multipart with the key; getStatus and checkHealth use the documented paths', async () => {
  const fake = await startFake();
  const document = writeDocument();
  try {
    const client = new FaxbotClient(`${fake.url}/`, 'sdk-key');
    assert.deepEqual(await client.sendFax('+15551234567', document), { id: 'job-1', status: 'queued' });
    const post = fake.state.requests.find((r) => r.path === '/fax');
    assert.equal(post.key, 'sdk-key');
    assert.match(post.operation, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
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

test('a lost response is recovered with the same operation id', () => withFake(async (fake) => {
  fake.state.plan = ['drop'];
  const client = new FaxbotClient(fake.url, 'sdk-key', { retries: 2, retryBackoffMs: 0 });
  const job = await client.sendFax('+15551234567', writeDocument());
  const posts = fake.posts();
  assert.equal(posts.length, 2);
  assert.equal(posts[0].operation, posts[1].operation);
  // The retry reopened the file: a consumed stream would upload an empty document.
  assert.match(posts[1].body, /%PDF-1.4 synthetic/);
  assert.deepEqual(fake.state.jobs, [job]);
}));

test('by default a lost response is never sent again until the caller resumes', () => withFake(async (fake) => {
  // Older servers ignore the operation id, so the client never resends on its own.
  fake.state.plan = ['drop'];
  const document = writeDocument();
  const client = new FaxbotClient(fake.url, 'sdk-key');
  const error = await client.sendFax('+15551234567', document).then(() => assert.fail('expected an error'), (e) => e);
  assert.equal(error.uncertain, true);
  assert.deepEqual(fake.posts().map((post) => post.operation), [error.operationId]);
  assert.equal(fake.state.jobs.length, 1);
  assert.deepEqual(await client.resumeFax(error.operationId, '+15551234567', document), fake.state.jobs[0]);
  assert.equal(fake.state.jobs.length, 1);
}));

test('an unconfirmed fax is finished with its operation id', () => withFake(async (fake) => {
  fake.state.plan = ['uncertain', 'uncertain', 'uncertain'];
  const document = writeDocument();
  const client = new FaxbotClient(fake.url, 'sdk-key', { retries: 2, retryBackoffMs: 0 });
  const error = await client.sendFax('+15551234567', document).then(() => assert.fail('expected an error'), (e) => e);
  assert.equal(error.uncertain, true);
  assert.equal(error.status, 503);
  assert.ok(error instanceof FaxbotClient.FaxSendError);
  assert.deepEqual(fake.posts().map((post) => post.operation), [error.operationId, error.operationId, error.operationId]);
  assert.equal(error.message, `Faxbot did not confirm this fax, so call sendFax again with operationId=${error.operationId} to finish the same fax without sending it twice.`);
  assert.equal(fake.state.jobs.length, 1);
  assert.deepEqual(await client.resumeFax(error.operationId, '+15551234567', document), fake.state.jobs[0]);
  assert.equal(fake.state.jobs.length, 1);
}));

test('a send that never reached Faxbot is finished later', () => withFake(async (fake) => {
  const probe = http.createServer();
  await new Promise((resolve) => probe.listen(0, '127.0.0.1', resolve));
  const closed = `http://127.0.0.1:${probe.address().port}`;
  await new Promise((resolve) => probe.close(resolve));
  const document = writeDocument();
  const unreachable = new FaxbotClient(closed, 'sdk-key', { retries: 1, retryBackoffMs: 0 });
  const error = await unreachable.sendFax('+15551234567', document).then(() => assert.fail('expected an error'), (e) => e);
  assert.equal(error.uncertain, true);
  assert.equal(error.status, null);
  const client = new FaxbotClient(fake.url, 'sdk-key');
  const first = await client.sendFax('+15551234567', document, { operationId: error.operationId });
  assert.deepEqual(await client.resumeFax(error.operationId, '+15551234567', document), first);
  assert.equal(fake.state.jobs.length, 1);
}));

test('an operation id belongs to one fax', () => withFake(async (fake) => {
  const client = new FaxbotClient(fake.url, 'sdk-key', { retryBackoffMs: 0 });
  const operationId = FaxbotClient.newOperationId();
  const document = writeDocument();
  await client.sendFax('+15551234567', document, { operationId });
  for (const [to, file] of [['+15551234567', writeDocument('other.pdf', '%PDF-1.4 a different document')], ['+15559876543', document]]) {
    const error = await client.sendFax(to, file, { operationId }).then(() => assert.fail('expected a conflict'), (e) => e);
    assert.equal(error.message, `Conflict (409): ${CONFLICT}`);
    assert.deepEqual([error.status, error.uncertain, error.operationId], [409, false, operationId]);
  }
  assert.equal(fake.posts().length, 3, 'a conflict is never retried');
  assert.equal(fake.state.jobs.length, 1);
}));

test('each send without an operation id is a new fax', () => withFake(async (fake) => {
  const client = new FaxbotClient(fake.url, 'sdk-key');
  const document = writeDocument();
  const first = await client.sendFax('+15551234567', document);
  const second = await client.sendFax('+15551234567', document);
  assert.notEqual(first.id, second.id);
  assert.equal(fake.state.jobs.length, 2);
  const [a, b] = fake.posts().map((post) => post.operation);
  assert.notEqual(a, b);
}));

test('only unconfirmed answers are retried', () => withFake(async (fake) => {
  const client = new FaxbotClient(fake.url, 'sdk-key', { retries: 2, retryBackoffMs: 0 });
  const document = writeDocument();
  const expected = { 400: /Bad Request \(400\): synthetic 400/, 401: /Unauthorized \(401\)/, 404: /Not Found \(404\)/,
    408: /HTTP 408/, 409: /Conflict \(409\): synthetic 409/, 413: /Payload Too Large \(413\)/,
    415: /Unsupported Media Type \(415\)/, 429: /HTTP 429/, 500: /HTTP 500/ };
  for (const [status, message] of Object.entries(expected)) {
    const before = fake.posts().length;
    fake.state.plan = [Number(status)];
    const error = await client.sendFax('+15551234567', document).then(() => assert.fail('expected an error'), (e) => e);
    assert.match(error.message, message);
    assert.deepEqual([error.status, error.uncertain], [Number(status), false]);
    assert.equal(fake.posts().length - before, 1, `HTTP ${status} is never retried`);
  }
  for (const status of [502, 503, 504]) {
    const before = fake.posts().length;
    fake.state.plan = [status, status];
    await client.sendFax('+15551234567', document);
    const attempts = fake.posts().slice(before);
    assert.equal(attempts.length, 3);
    assert.equal(new Set(attempts.map((post) => post.operation)).size, 1);
  }
}));

test('invalid operation ids and retry settings are refused before any upload', () => withFake(async (fake) => {
  const client = new FaxbotClient(fake.url, 'sdk-key');
  const document = writeDocument();
  for (const bad of ['has space', '', 'x'.repeat(129), 'café', 42]) {
    await assert.rejects(client.sendFax('+15551234567', document, { operationId: bad }), /operationId must be/);
  }
  await assert.rejects(client.resumeFax('', '+15551234567', document), /operationId is required/);
  assert.throws(() => new FaxbotClient(fake.url, null, { retries: -1 }), /retries/);
  assert.equal(fake.posts().length, 0);
}));

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
