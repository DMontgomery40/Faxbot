// Faxbot REST client bound to one caller's API key.
// The key is sent as X-API-Key only when present; an empty header is never sent.
import { randomUUID } from 'node:crypto';

// Faxbot answers these when it could not confirm the fax; the same operation id is safe to send again.
const RETRYABLE_STATUSES = new Set([502, 503, 504]);
const OPERATION_ID = /^[\x21-\x7E]{1,128}$/;

export class FaxApiError extends Error {
  constructor(status, detail) {
    super(`Fax API error ${status}: ${detail}`);
    this.status = status;
  }
}

// No attempt of a send was confirmed: Faxbot may or may not have the fax.
export class FaxSubmissionUncertainError extends Error {
  constructor(operationId, options) {
    super(`Faxbot did not confirm this fax (operationId ${operationId}). Call send_fax again with operationId=${operationId} `
      + 'and the same number and document to finish this same fax without sending it twice.', options);
    this.operationId = operationId;
    this.uncertain = true;
  }
}

// A new operation id; a send without one is a new fax.
export function newOperationId() {
  return randomUUID();
}

export function checkOperationId(operationId) {
  if (typeof operationId !== 'string' || !OPERATION_ID.test(operationId)) {
    throw new Error('operationId must be 1 to 128 printable ASCII characters without spaces');
  }
  return operationId;
}

// GET /inbound returns a bare list; tolerate an { items: [...] } envelope too.
export function inboundItems(data) {
  const items = Array.isArray(data) ? data : data?.items;
  if (!Array.isArray(items)) throw new Error('Faxbot returned an unexpected inbound list.');
  return items.filter((item) => item && typeof item === 'object');
}

async function apiError(response) {
  const text = await response.text();
  let detail = text;
  try {
    detail = JSON.parse(text)?.detail || text;
  } catch {}
  return new FaxApiError(response.status, detail || response.statusText);
}

// retries: how many more times one sendFax call sends the same fax, with the same operation id,
// after a transport failure or HTTP 502/503/504; retryBackoffMs doubles after each retry. The
// default 0 never sends again on its own; the tool error names the operationId for an explicit resume.
export function createFaxClient({ baseUrl = 'http://localhost:8080', apiKey = '', retries = 0, retryBackoffMs = 500 } = {}) {
  const base = String(baseUrl).replace(/\/+$/, '');

  async function call(method, path, { expect, body, timeoutMs = 15000 } = {}) {
    const headers = apiKey ? { 'X-API-Key': apiKey } : {};
    const response = await fetch(base + path, { method, headers, body, signal: AbortSignal.timeout(timeoutMs) });
    if (response.status !== expect) throw await apiError(response);
    return response;
  }

  return {
    // Sends the fax as one operation. Without an operationId it is a new fax with a new id.
    async sendFax(to, buffer, fileName, fileType, { operationId } = {}) {
      const id = checkOperationId(operationId || newOperationId());
      const type = fileType === 'pdf' ? 'application/pdf' : 'text/plain';
      const headers = { 'Idempotency-Key': id, ...(apiKey ? { 'X-API-Key': apiKey } : {}) };
      for (let attempt = 0; ; attempt += 1) {
        if (attempt) await new Promise((resolve) => setTimeout(resolve, retryBackoffMs * 2 ** (attempt - 1)));
        // A fresh form and timeout each attempt: a fired timeout signal stays aborted.
        const form = new FormData();
        form.append('to', to);
        form.append('file', new Blob([buffer], { type }), fileName);
        let response;
        try {
          response = await fetch(`${base}/fax`, { method: 'POST', headers, body: form, signal: AbortSignal.timeout(60000) });
        } catch (error) {
          // No response: the request may or may not have reached Faxbot.
          if (attempt < retries) continue;
          throw new FaxSubmissionUncertainError(id, { cause: error });
        }
        if (RETRYABLE_STATUSES.has(response.status)) {
          await response.body?.cancel();
          if (attempt < retries) continue;
          throw new FaxSubmissionUncertainError(id);
        }
        if (response.status === 202) return response.json();
        const error = await apiError(response);
        if (response.status === 409) {
          error.message += ` operationId ${id} belongs to a different number or document; omit operationId to send a new fax.`;
        }
        error.operationId = id;
        throw error;
      }
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
