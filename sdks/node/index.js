/**
 * Faxbot API Client SDK for Node.js.
 *
 * Provides a FaxbotClient class to send faxes and retrieve fax status from a Faxbot API server.
 *
 * Usage example:
 *   const FaxbotClient = require('faxbot');
 *   const client = new FaxbotClient('http://localhost:8080', 'YOUR_API_KEY');
 *   const operationId = FaxbotClient.newOperationId(); // save this before sending
 *   client.sendFax('+15551234567', '/path/to/document.pdf', { operationId })
 *     .then(job => {
 *         console.log(`Fax queued with ID: ${job.id}, initial status: ${job.status}`);
 *         return client.getStatus(job.id);
 *     })
 *     .then(statusInfo => {
 *         console.log(`Fax status: ${statusInfo.status}`);
 *     })
 *     .catch(err => {
 *         // err.uncertain: Faxbot may or may not have the fax; resumeFax(err.operationId, ...) finishes it.
 *         console.error('Fax operation failed:', err.message);
 *     });
 */
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const axios = require('axios');
const FormData = require('form-data');
const PluginManager = require('./plugins');

// Faxbot answers these when it could not confirm the fax; the same operation id is safe to send again.
const RETRYABLE_STATUSES = new Set([502, 503, 504]);
const OPERATION_ID = /^[\x21-\x7E]{1,128}$/;

/** An error from sendFax. `uncertain` is true when no attempt was confirmed and the fax can be finished. */
class FaxSendError extends Error {
  constructor(message, { operationId, status = null, uncertain = false, cause } = {}) {
    super(message, cause ? { cause } : undefined);
    this.name = 'FaxSendError';
    this.operationId = operationId;
    this.status = status;
    this.uncertain = uncertain;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function sendFailure(error, operationId) {
  const fail = (message) => new FaxSendError(message, { operationId, status: error.response?.status ?? null, cause: error });
  if (!error.response) return fail(`Fax send error: ${error.message}`);
  const status = error.response.status;
  let errMsg = '';
  if (error.response.data) {
    if (typeof error.response.data === 'object' && error.response.data.detail) {
      errMsg = error.response.data.detail;
    } else if (typeof error.response.data === 'string') {
      errMsg = error.response.data;
    }
  }
  if (status === 400) return fail(`Bad Request (400): ${errMsg || 'Invalid fax parameters or phone number.'}`);
  if (status === 401) return fail('Unauthorized (401): API key is invalid or missing.');
  if (status === 409) return fail(`Conflict (409): ${errMsg || 'Idempotency-Key already belongs to a different fax request.'}`);
  if (status === 415) return fail(`Unsupported Media Type (415): ${errMsg || 'File type not allowed. Only PDF or TXT can be sent.'}`);
  if (status === 413) return fail(`Payload Too Large (413): ${errMsg || 'File size exceeds the allowed limit.'}`);
  if (status === 404) return fail(`Not Found (404): ${errMsg || 'The Faxbot API endpoint was not found (check baseUrl).'}`);
  return fail(`Fax send failed (HTTP ${status}): ${errMsg || error.response.statusText}`);
}

class FaxbotClient {
  /**
   * Create a new FaxbotClient.
   * @param {string} [baseUrl="http://localhost:8080"] - Base URL of the Faxbot API.
   * @param {string|null} [apiKey=null] - API key for authentication (optional).
   * @param {Object} [options]
   * @param {number} [options.retries=2] - How many more times sendFax sends the same fax, with the same
   *   operation id, after a connection error, a timeout or HTTP 502/503/504. Use 0 against a Faxbot
   *   server that does not support Idempotency-Key.
   * @param {number} [options.retryBackoffMs=500] - Wait before the first retry; each later retry waits twice as long.
   */
  constructor(baseUrl = 'http://localhost:8080', apiKey = null, { retries = 2, retryBackoffMs = 500 } = {}) {
    if (!Number.isInteger(retries) || retries < 0) throw new Error('retries must be a whole number, 0 or more');
    if (typeof retryBackoffMs !== 'number' || !(retryBackoffMs >= 0)) throw new Error('retryBackoffMs must be 0 or more');
    // Remove trailing slash from baseUrl if present for consistency
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey;
    this.retries = retries;
    this.retryBackoffMs = retryBackoffMs;
    // Preconfigure an Axios instance for convenience
    this._axios = axios.create({
      baseURL: this.baseUrl,
      timeout: 30000, // 30 seconds default timeout
    });
    // Expose plugin manager (auto-detects server plugin support)
    this.plugins = new PluginManager(this);
  }

  /**
   * A new operation id for one fax. Save it before sending so an unconfirmed send can be finished.
   * @returns {string}
   */
  static newOperationId() {
    return crypto.randomUUID();
  }

  /**
   * Send a fax via the Faxbot API.
   *
   * Every call is one fax operation, identified by `operationId` and sent as the Idempotency-Key
   * header. Without an `operationId` the call is a new fax and gets a new id, even for a document sent
   * before. After a connection error, a timeout or HTTP 502/503/504 the client sends the same fax again
   * with the same id, up to `retries` more times; Faxbot returns the original job instead of sending it twice.
   *
   * @param {string} to - The destination fax number (E.164 format like "+15551234567" is recommended).
   * @param {string} filePath - Path to the PDF or text file to send as fax.
   * @param {Object} [options]
   * @param {string} [options.operationId] - The id of an earlier send to finish (see resumeFax). Omit it for a new fax.
   * @returns {Promise<Object>} - Resolves to the server's fax job object, unchanged.
   * @throws {FaxSendError} - With `operationId`, `status` (HTTP status or null) and `uncertain`
   *   (true when no attempt was confirmed; send again with that operationId to finish the same fax).
   */
  async sendFax(to, filePath, { operationId } = {}) {
    if (!to) {
      throw new Error("Destination fax number 'to' is required");
    }
    if (!filePath) {
      throw new Error("filePath is required and must point to a .pdf or .txt file");
    }
    if (!fs.existsSync(filePath)) {
      throw new Error(`File not found: ${filePath}`);
    }

    // Determine MIME type
    const ext = path.extname(filePath).toLowerCase();
    let contentType;
    if (ext === '.pdf') {
      contentType = 'application/pdf';
    } else if (ext === '.txt') {
      contentType = 'text/plain';
    } else {
      throw new Error(`Unsupported file type '${ext}'. Only .pdf or .txt files are allowed.`);
    }

    // The id exists before any upload, so an unconfirmed send can always be finished with it.
    const id = operationId ?? FaxbotClient.newOperationId();
    if (typeof id !== 'string' || !OPERATION_ID.test(id)) {
      throw new Error('operationId must be 1 to 128 printable ASCII characters without spaces');
    }

    for (let attempt = 0; ; attempt += 1) {
      if (attempt) await sleep(this.retryBackoffMs * 2 ** (attempt - 1));
      // Rebuild the form and reopen the file every attempt: a consumed stream would upload an empty body.
      const form = new FormData();
      form.append('to', to);
      const fileStream = fs.createReadStream(filePath);
      form.append('file', fileStream, {
        filename: path.basename(filePath),
        contentType: contentType,
      });
      const headers = { ...form.getHeaders(), 'Idempotency-Key': id };
      if (this.apiKey) {
        headers['X-API-Key'] = this.apiKey;
      }
      try {
        const response = await this._axios.post('/fax', form, { headers });
        return response.data;
      } catch (error) {
        // No response means the request may or may not have reached Faxbot.
        const retryable = error.response ? RETRYABLE_STATUSES.has(error.response.status) : Boolean(error.request);
        if (!retryable) throw sendFailure(error, id);
        if (attempt < this.retries) continue;
        throw new FaxSendError(
          `Faxbot did not confirm this fax, so call sendFax again with operationId '${id}' to finish the same fax without sending it twice.`,
          { operationId: id, status: error.response?.status ?? null, uncertain: true, cause: error });
      } finally {
        fileStream.destroy();
      }
    }
  }

  /**
   * Finish a fax whose send was not confirmed, without sending it twice.
   * Pass the operationId from the error (or the one you saved before sending) with the same number
   * and document. Faxbot returns the original job if it already accepted the fax, or accepts it now.
   * A different number or document fails with status 409.
   * @param {string} operationId
   * @param {string} to
   * @param {string} filePath
   * @returns {Promise<Object>}
   */
  async resumeFax(operationId, to, filePath) {
    if (!operationId) {
      throw new Error('operationId is required to resume a fax');
    }
    return this.sendFax(to, filePath, { operationId });
  }

  /**
   * Get the status of a sent fax job.
   * @param {string} jobId - The ID of the fax job to retrieve.
   * @returns {Promise<Object>} - Resolves to the fax job status object.
   * @throws {Error} - If jobId is missing or the API call fails (404 if not found, etc.).
   */
  async getStatus(jobId) {
    if (!jobId) {
      throw new Error('jobId is required to retrieve fax status');
    }
    try {
      const headers = this.apiKey ? { 'X-API-Key': this.apiKey } : {};
      const response = await this._axios.get(`/fax/${jobId}`, { headers });
      return response.data;
    } catch (error) {
      if (error.response) {
        const status = error.response.status;
        let errMsg = '';
        if (error.response.data && typeof error.response.data === 'object' && error.response.data.detail) {
          errMsg = error.response.data.detail;
        }
        if (status === 404) {
          throw new Error(`Fax job not found (404): Job ID ${jobId} does not exist.`);
        } else if (status === 401) {
          throw new Error('Unauthorized (401): API key is invalid or missing for status check.');
        } else {
          throw new Error(`Failed to get fax status (HTTP ${status}): ${errMsg || error.response.statusText}`);
        }
      } else if (error.request) {
        throw new Error('Failed to get fax status: No response from server.');
      } else {
        throw new Error(`Error getting fax status: ${error.message}`);
      }
    }
  }

  /**
   * Check the health/status of the Faxbot API server.
   * @returns {Promise<boolean>} - Resolves to true if the server is healthy (status "ok"), otherwise false.
   * @throws {Error} - If the server is unreachable or returns an unexpected response.
   */
  async checkHealth() {
    try {
      const response = await this._axios.get('/health');
      if (response.status === 200) {
        const data = response.data;
        if (data && typeof data === 'object' && data.status === 'ok') {
          return true;
        }
        return true;
      }
      return false;
    } catch (error) {
      if (error.response) {
        throw new Error(`Health check failed (HTTP ${error.response.status})`);
      }
      throw new Error(`Health check failed: ${error.message}`);
    }
  }
}

// Export the FaxbotClient class as the module's default export
module.exports = FaxbotClient;
module.exports.FaxSendError = FaxSendError;
