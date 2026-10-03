// A local fake Faxbot API that records the X-API-Key of every request.
// POST /fax honors Idempotency-Key per API key like the server contract; each accepted job is one
// provider submission in state.jobs. state.plan scripts the next POST /fax replies: 'accept' (default),
// 'drop' (accept, then close without answering), 'uncertain' (accept, then answer 503) or an HTTP
// status (answer it without accepting).
import { createHash } from 'node:crypto';
import { createServer } from 'node:http';

export const PDF = Buffer.from('%PDF-1.4\n% synthetic inbound fax\n');
export const CONFLICT = 'Idempotency-Key already belongs to a different fax request.';

async function acceptFax(state, req, body, reply) {
  const action = state.plan.length ? state.plan.shift() : 'accept';
  if (typeof action === 'number') return reply(action, { detail: `synthetic ${action}` });
  const form = await new Response(body, { headers: { 'content-type': req.headers['content-type'] } }).formData();
  const digest = createHash('sha256').update(Buffer.from(await form.get('file').arrayBuffer())).digest('hex');
  const request = JSON.stringify([form.get('to'), digest, form.get('queue_only')]);
  const key = req.headers['x-api-key'];
  const operation = req.headers['idempotency-key'];
  const scope = `${key}\n${operation}`;
  let entry = operation === undefined ? undefined : state.ledger.get(scope);
  if (entry && entry.request !== request) return reply(409, { detail: CONFLICT });
  if (!entry) {
    const number = 1 + state.jobs.filter((job) => job.id.endsWith(`-for-${key}`)).length;
    entry = { request, job: { id: `job-${number}-for-${key}`, status: 'queued' } };
    state.jobs.push(entry.job); // one provider submission
    if (operation !== undefined) state.ledger.set(scope, entry);
  }
  if (action === 'drop') return req.socket.destroy(); // the job exists but its response is lost
  if (action === 'uncertain') return reply(503, { detail: 'Fax acceptance is uncertain; retry with the same key.' });
  return reply(202, entry.job);
}

export async function startFakeFaxbot() {
  const state = { requests: [], inboundEnvelope: false, jwks: { keys: [] }, plan: [], jobs: [], ledger: new Map() };
  const server = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    const path = new URL(req.url, 'http://localhost').pathname;
    const reply = (status, data, type = 'application/json') => {
      res.writeHead(status, { 'Content-Type': type });
      res.end(Buffer.isBuffer(data) ? data : JSON.stringify(data));
    };
    if (path === '/.well-known/jwks.json') return reply(200, state.jwks);
    const key = req.headers['x-api-key'];
    state.requests.push({ method: req.method, path, key, body, operation: req.headers['idempotency-key'] });
    if (req.method === 'POST' && path === '/fax') return acceptFax(state, req, body, reply);
    // Inbound ids are hex like outbound ids; /fax/a1b2c3 is unknown, so get_fax must fall back to /inbound.
    if (path === '/fax/missing' || path === '/fax/a1b2c3' || path === '/inbound/missing') return reply(404, { detail: 'Not found' });
    let match = path.match(/^\/fax\/([^/]+)$/);
    if (match) {
      return reply(200, { id: decodeURIComponent(match[1]), to: '+15551230000', status: 'SUCCESS', pages: 1, backend: 'sip',
        error: null, created_at: '2026-10-03T10:00:00', updated_at: '2026-10-03T10:01:00' });
    }
    if (path === '/inbound') {
      const items = [{ id: 'a1b2c3', fr: '+15550001111', to: '+15550002222', status: 'received', backend: 'sip', pages: 2, received_at: '2026-10-03T09:00:00' }];
      return reply(200, state.inboundEnvelope ? { items } : items);
    }
    if (/^\/inbound\/[^/]+\/pdf$/.test(path)) return reply(200, PDF, 'application/pdf');
    match = path.match(/^\/inbound\/([^/]+)$/);
    if (match) return reply(200, { id: match[1], fr: '+15550001111', to: '+15550002222', status: 'received', backend: 'sip', pages: 2 });
    return reply(404, { detail: 'Not Found' });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return {
    state,
    url: `http://127.0.0.1:${server.address().port}`,
    keys: () => state.requests.map((request) => request.key),
    posts: () => state.requests.filter((request) => request.path === '/fax'),
    close: () => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); }),
  };
}
