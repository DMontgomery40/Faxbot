import axios from 'axios';
import FormData from 'form-data';

const BASE_URL = process.env.FAX_API_URL || 'http://localhost:8080';
const API_KEY = process.env.API_KEY || '';

function buildHeaders(extra = {}) {
  const headers = { ...extra };
  if (API_KEY) headers['X-API-Key'] = API_KEY;
  return headers;
}

export async function sendFax(to, contentOrBuffer, type = 'txt', filename) {
  if (!to || typeof to !== 'string') {
    throw new Error('Missing recipient number');
  }
  const isBuffer = Buffer.isBuffer(contentOrBuffer);
  if (!isBuffer && typeof contentOrBuffer !== 'string') {
    throw new Error('Content must be a string or Buffer');
  }
  const normalizedTo = to.replace(/\s/g, '');
  const form = new FormData();
  form.append('to', normalizedTo);

  const name = filename || (type === 'pdf' ? 'document.pdf' : 'document.txt');
  const buffer = isBuffer
    ? contentOrBuffer
    : Buffer.from(contentOrBuffer, 'utf8');
  form.append('file', buffer, {
    filename: name,
    contentType: type === 'pdf' ? 'application/pdf' : 'text/plain',
  });

  const headers = buildHeaders(form.getHeaders());
  const resp = await axios.post(`${BASE_URL}/fax`, form, { headers, timeout: 30000 });
  if (!resp.data) throw new Error('Invalid response from Fax API');
  return resp.data; // expecting { id, status, ... }
}

export async function getFaxStatus(jobId) {
  if (!jobId) throw new Error('jobId required');
  const headers = buildHeaders();
  const resp = await axios.get(`${BASE_URL}/fax/${jobId}`, { headers, timeout: 10000 });
  if (!resp.data) throw new Error('Invalid response from Fax API');
  return resp.data;
}

export default { sendFax, getFaxStatus };
