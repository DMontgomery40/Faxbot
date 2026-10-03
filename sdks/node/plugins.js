/**
 * Provider plugin management for the Faxbot Node SDK.
 */

class PluginManager {
  constructor(client) {
    this.client = client;
    this.enabled = false;
    this._checkPluginSupport();
  }

  _headers() {
    return this.client.apiKey ? { 'X-API-Key': this.client.apiKey } : {};
  }

  async _checkPluginSupport() {
    try {
      const res = await this.client._axios.get('/plugins', { headers: this._headers() });
      if (res.status === 200) this.enabled = true;
    } catch (_) {
      this.enabled = false;
    }
  }

  /** Installed plugins (GET /plugins answers { items: [...] }). */
  async listPlugins() {
    if (!this.enabled) return [];
    const res = await this.client._axios.get('/plugins', { headers: this._headers() });
    return Array.isArray(res.data) ? res.data : (res.data?.items || []);
  }

  /** { enabled, settings, role, _meta }; secrets are masked. */
  async getPluginConfig(pluginId) {
    const res = await this.client._axios.get(`/plugins/${pluginId}/config`, { headers: this._headers() });
    return res.data;
  }

  /**
   * Change only the given provider fields (PUT /plugins/{id}/config). Requires providers:write.
   * Pass expectedRevisionId from getPluginConfig(...)._meta.desired_revision_id to refuse a
   * write when someone else changed the configuration first.
   */
  async updatePluginConfig(pluginId, settings, { enabled, role, expectedRevisionId } = {}) {
    const patch = { settings, enabled, role, expected_revision_id: expectedRevisionId };
    const body = Object.fromEntries(Object.entries(patch).filter(([, value]) => value !== undefined && value !== null));
    const res = await this.client._axios.put(`/plugins/${pluginId}/config`, body, { headers: this._headers() });
    return res.data;
  }

  /** Install an HTTP provider manifest (POST /admin/plugins/http/install). Requires providers:install. */
  async installPlugin(manifest) {
    if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
      throw new TypeError('installPlugin takes an HTTP provider manifest (an object with an id).');
    }
    const res = await this.client._axios.post('/admin/plugins/http/install', { manifest }, { headers: this._headers() });
    return res.data;
  }
}

module.exports = PluginManager;
