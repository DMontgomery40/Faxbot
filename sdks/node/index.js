/**
 * Faxbot API Client SDK for Node.js.
 *
 * Provides a FaxbotClient class to send faxes and retrieve fax status from a Faxbot API server.
 *
 * Usage example:
 *   const FaxbotClient = require('faxbot');
 *   const client = new FaxbotClient('http://localhost:8080', 'YOUR_API_KEY');
 *   client.sendFax('+15551234567', '/path/to/document.pdf')
 *     .then(job => {
 *         console.log(`Fax queued with ID: ${job.id}, initial status: ${job.status}`);
 *         return client.getStatus(job.id);
 *     })
 *     .then(statusInfo => {
 *         console.log(`Fax status: ${statusInfo.status}`);
 *     })
 *     .catch(err => {
 *         console.error('Fax operation failed:', err.message);
 *     });
 */
const fs = require('fs');
const path = require('path');
const axios = require('axios');
const FormData = require('form-data');
const PluginManager = require('./plugins');

class FaxbotClient {
  /**
   * Create a new FaxbotClient.
   * @param {string} [baseUrl="http://localhost:8080"] - Base URL of the Faxbot API.
   * @param {string|null} [apiKey=null] - API key for authentication (optional).
   */
  constructor(baseUrl = 'http://localhost:8080', apiKey = null) {
    // Remove trailing slash from baseUrl if present for consistency
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey;
    // Preconfigure an Axios instance for convenience
    this._axios = axios.create({
      baseURL: this.baseUrl,
      timeout: 30000, // 30 seconds default timeout
    });
    // Expose plugin manager (auto-detects server plugin support)
    this.plugins = new PluginManager(this);
  }

  /**
   * Send a fax via the Faxbot API.
   * @param {string} to - The destination fax number (E.164 format like "+15551234567" is recommended).
   * @param {string} filePath - Path to the PDF or text file to send as fax.
   * @returns {Promise<Object>} - Resolves to the fax job info object (with id, status, etc.).
   * @throws {Error} - If inputs are invalid or the API call fails.
   */
  async sendFax(to, filePath) {
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

    // Prepare form data
    const form = new FormData();
    form.append('to', to);
    const fileStream = fs.createReadStream(filePath);
    form.append('file', fileStream, {
      filename: path.basename(filePath),
      contentType: contentType,
    });

    // Headers
    let headers = form.getHeaders();
    if (this.apiKey) {
      headers['X-API-Key'] = this.apiKey;
    }

    try {
      const response = await this._axios.post('/fax', form, { headers });
      return response.data;
    } catch (error) {
      if (error.response) {
        const status = error.response.status;
        let errMsg = '';
        if (error.response.data) {
          if (typeof error.response.data === 'object' && error.response.data.detail) {
            errMsg = error.response.data.detail;
          } else if (typeof error.response.data === 'string') {
            errMsg = error.response.data;
          }
        }
        if (status === 400) {
          throw new Error(`Bad Request (400): ${errMsg || 'Invalid fax parameters or phone number.'}`);
        } else if (status === 401) {
          throw new Error(`Unauthorized (401): API key is invalid or missing.`);
        } else if (status === 415) {
          throw new Error(`Unsupported Media Type (415): ${errMsg || 'File type not allowed. Only PDF or TXT can be sent.'}`);
        } else if (status === 413) {
          throw new Error(`Payload Too Large (413): ${errMsg || 'File size exceeds the allowed limit.'}`);
        } else if (status === 404) {
          throw new Error(`Not Found (404): ${errMsg || 'The Faxbot API endpoint was not found (check baseUrl).'}`);
        } else {
          throw new Error(`Fax send failed (HTTP ${status}): ${errMsg || error.response.statusText}`);
        }
      } else if (error.request) {
        throw new Error('Fax send failed: No response from server. Please check the server URL and network connection.');
      } else {
        throw new Error(`Fax send error: ${error.message}`);
      }
    }
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
