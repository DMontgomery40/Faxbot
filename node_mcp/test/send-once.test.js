import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createServer } from 'node:http';
import { createFaxClient } from '../src/shared/fax-client.js';
import { faxToolDefinitions } from '../src/tools/fax-tools.js';
import { CONFLICT, startFakeFaxbot } from './fake-faxbot.js';

const SEND = { to: '+15551234567', fileContent: Buffer.from('%PDF-1.4 once').toString('base64'), fileName: 'once.pdf' };
const OTHER_DOCUMENT = Buffer.from('%PDF-1.4 a different document').toString('base64');

// The send_fax handler in process, with retries that do not wait.
function sendFax(baseUrl, { localFiles = true } = {}) {
  const client = createFaxClient({ baseUrl, apiKey: 'mcp-key', retries: 2, retryBackoffMs: 0 });
  return faxToolDefinitions(client, { localFiles }).find((tool) => tool.name === 'send_fax').handler;
}

async function withFake(run) {
  const fake = await startFakeFaxbot();
  try {
    await run(fake);
  } finally {
    await fake.close();
  }
}

const failure = (promise) => promise.then(() => assert.fail('expected an error'), (error) => error);

async function listen(handler) {
  const server = createServer(handler);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return { url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((resolve) => server.close(resolve)) };
}

test('a lost response is recovered with the same operation id', () => withFake(async (fake) => {
  fake.state.plan = ['drop'];
  const sent = await sendFax(fake.url)(SEND);
  const posts = fake.posts();
  assert.deepEqual(posts.map((post) => post.operation), [sent.structuredContent.operationId, sent.structuredContent.operationId]);
  assert.ok(posts[1].body.includes('%PDF-1.4 once'));
  assert.deepEqual(fake.state.jobs.map((job) => job.id), [sent.structuredContent.id]);
}));

test('an unconfirmed fax is finished with its operation id', () => withFake(async (fake) => {
  fake.state.plan = ['uncertain', 'uncertain', 'uncertain'];
  const send = sendFax(fake.url);
  const error = await failure(send(SEND));
  const operation = error.operationId;
  assert.deepEqual(fake.posts().map((post) => post.operation), [operation, operation, operation]);
  assert.equal(error.message, `Faxbot did not confirm this fax (operationId ${operation}). Call send_fax again with `
    + `operationId=${operation} and the same number and document to finish this same fax without sending it twice.`);
  assert.equal(fake.state.jobs.length, 1);
  const finished = await send({ ...SEND, operationId: operation });
  assert.deepEqual(finished.structuredContent, { id: fake.state.jobs[0].id, status: 'queued', operationId: operation });
  assert.equal(fake.state.jobs.length, 1);
}));

test('a send that never reached Faxbot is finished later', () => withFake(async (fake) => {
  const probe = await listen(() => {});
  await probe.close();
  const error = await failure(sendFax(probe.url)(SEND));
  assert.equal(error.uncertain, true);
  assert.match(error.message, new RegExp(`operationId=${error.operationId} and the same number and document`));
  const send = sendFax(fake.url);
  const first = await send({ ...SEND, operationId: error.operationId });
  const again = await send({ ...SEND, operationId: error.operationId });
  assert.deepEqual(first.structuredContent, again.structuredContent);
  assert.equal(first.structuredContent.operationId, error.operationId);
  assert.equal(fake.state.jobs.length, 1);
}));

test('an operation id belongs to one fax', () => withFake(async (fake) => {
  const send = sendFax(fake.url);
  const { operationId } = (await send(SEND)).structuredContent;
  for (const change of [{ fileContent: OTHER_DOCUMENT }, { to: '+15559876543' }]) {
    const error = await failure(send({ ...SEND, ...change, operationId }));
    assert.equal(error.status, 409);
    assert.equal(error.message, `Fax API error 409: ${CONFLICT} operationId ${operationId} belongs to a different `
      + 'number or document; omit operationId to send a new fax.');
  }
  assert.equal(fake.posts().length, 3, 'a conflict is never retried');
  assert.equal(fake.state.jobs.length, 1);
}));

test('each send without an operation id is a new fax', () => withFake(async (fake) => {
  const send = sendFax(fake.url, { localFiles: false });
  const first = await send(SEND);
  const second = await send(SEND);
  assert.notEqual(first.structuredContent.id, second.structuredContent.id);
  assert.notEqual(first.structuredContent.operationId, second.structuredContent.operationId);
  assert.equal(fake.state.jobs.length, 2);
}));

test('only unconfirmed answers are retried', () => withFake(async (fake) => {
  const send = sendFax(fake.url);
  for (const status of [400, 401, 404, 409, 413, 429, 500]) {
    const before = fake.posts().length;
    fake.state.plan = [status];
    const error = await failure(send(SEND));
    assert.ok(error.message.startsWith(`Fax API error ${status}: synthetic ${status}`), error.message);
    assert.equal(fake.posts().length - before, 1, `HTTP ${status} is never retried`);
  }
  assert.equal(fake.state.jobs.length, 0);
}));

test('gateway failures are retried with the same document read once', () => withFake(async (fake) => {
  const document = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'faxbot-mcp-')), 'letter.pdf');
  fs.writeFileSync(document, '%PDF-1.4 local');
  let downloads = 0;
  const source = await listen((req, res) => {
    downloads += 1;
    res.writeHead(200, { 'Content-Type': 'application/pdf' });
    res.end('%PDF-1.4 remote');
  });
  try {
    const send = sendFax(fake.url);
    fake.state.plan = [502, 504];
    const local = await send({ to: '+15551234567', filePath: document });
    fake.state.plan = [503];
    const remote = await send({ to: '+15551234567', fileUrl: `${source.url}/remote.pdf` });
    const posts = fake.posts();
    assert.deepEqual(posts.map((post) => post.operation), [
      local.structuredContent.operationId, local.structuredContent.operationId, local.structuredContent.operationId,
      remote.structuredContent.operationId, remote.structuredContent.operationId]);
    assert.ok(posts.slice(0, 3).every((post) => post.body.includes('%PDF-1.4 local')));
    assert.ok(posts.slice(3).every((post) => post.body.includes('%PDF-1.4 remote')));
    assert.equal(downloads, 1, 'a retry resends the bytes already read');
    assert.equal(fake.state.jobs.length, 2);
  } finally {
    await source.close();
  }
}));

test('an invalid operation id is refused before any upload', () => withFake(async (fake) => {
  const send = sendFax(fake.url);
  for (const operationId of ['has space', 'x'.repeat(129), 'café']) {
    await assert.rejects(send({ ...SEND, operationId }), /operationId must be 1 to 128 printable ASCII characters/);
  }
  assert.equal(fake.posts().length, 0);
}));
