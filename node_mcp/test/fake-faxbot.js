// A local fake Faxbot API that records the X-API-Key of every request.
import { createServer } from 'node:http';

export const PDF = Buffer.from('%PDF-1.4\n% synthetic inbound fax\n');

export async function startFakeFaxbot() {
  const state = { requests: [], inboundEnvelope: false, jwks: { keys: [] } };
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
    state.requests.push({ method: req.method, path, key, body });
    if (req.method === 'POST' && path === '/fax') return reply(202, { id: `job-for-${key}`, status: 'queued' });
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
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}
