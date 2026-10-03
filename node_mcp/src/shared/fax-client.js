// Faxbot REST client bound to one caller's API key.
// The key is sent as X-API-Key only when present; an empty header is never sent.

export class FaxApiError extends Error {
  constructor(status, detail) {
    super(`Fax API error ${status}: ${detail}`);
    this.status = status;
  }
}

// GET /inbound returns a bare list; tolerate an { items: [...] } envelope too.
export function inboundItems(data) {
  const items = Array.isArray(data) ? data : data?.items;
  if (!Array.isArray(items)) throw new Error('Faxbot returned an unexpected inbound list.');
  return items.filter((item) => item && typeof item === 'object');
}

export function createFaxClient({ baseUrl = 'http://localhost:8080', apiKey = '' } = {}) {
  const base = String(baseUrl).replace(/\/+$/, '');

  async function call(method, path, { expect, body, timeoutMs = 15000 } = {}) {
    const headers = apiKey ? { 'X-API-Key': apiKey } : {};
    const response = await fetch(base + path, { method, headers, body, signal: AbortSignal.timeout(timeoutMs) });
    if (response.status !== expect) {
      const text = await response.text();
      let detail = text;
      try {
        detail = JSON.parse(text)?.detail || text;
      } catch {}
      throw new FaxApiError(response.status, detail || response.statusText);
    }
    return response;
  }

  return {
    async sendFax(to, buffer, fileName, fileType) {
      const form = new FormData();
      form.append('to', to);
      const type = fileType === 'pdf' ? 'application/pdf' : 'text/plain';
      form.append('file', new Blob([buffer], { type }), fileName);
      return (await call('POST', '/fax', { expect: 202, body: form, timeoutMs: 60000 })).json();
    },
    async getFaxStatus(jobId) {
      return (await call('GET', `/fax/${encodeURIComponent(jobId)}`, { expect: 200 })).json();
    },
    async listInbound() {
      return inboundItems(await (await call('GET', '/inbound', { expect: 200 })).json());
    },
    async getInbound(inboundId) {
      return (await call('GET', `/inbound/${encodeURIComponent(inboundId)}`, { expect: 200 })).json();
    },
    async downloadInboundPdf(inboundId) {
      const response = await call('GET', `/inbound/${encodeURIComponent(inboundId)}/pdf`, { expect: 200, timeoutMs: 30000 });
      return { buffer: Buffer.from(await response.arrayBuffer()), contentType: response.headers.get('content-type') || 'application/pdf' };
    },
  };
}
