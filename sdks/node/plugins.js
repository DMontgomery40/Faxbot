/**
 * Plugin management extension for existing Faxbot Node SDK
 */

class PluginManager {
  constructor(client) {
    this.client = client;
    this.enabled = false;
    this._checkPluginSupport();
  }

  async _checkPluginSupport() {
    try {
      const headers = this.client.apiKey ? { 'X-API-Key': this.client.apiKey } : {};
      const res = await this.client._axios.get('/plugins', { headers });
      if (res.status === 200) this.enabled = true;
    } catch (_) {
      this.enabled = false;
    }
  }

  async listPlugins() {
    if (!this.enabled) return [];
    const headers = this.client.apiKey ? { 'X-API-Key': this.client.apiKey } : {};
    const res = await this.client._axios.get('/plugins', { headers });
    return res.data;
  }

  async getPluginConfig(pluginId) {
    const headers = this.client.apiKey ? { 'X-API-Key': this.client.apiKey } : {};
    const res = await this.client._axios.get(`/plugins/${pluginId}/config`, { headers });
    return res.data;
  }

  async updatePluginConfig(pluginId, config) {
    const headers = this.client.apiKey ? { 'X-API-Key': this.client.apiKey } : {};
    const res = await this.client._axios.put(`/plugins/${pluginId}/config`, config, { headers });
    return res.data;
  }

  async installPlugin(pluginId) {
    const headers = this.client.apiKey ? { 'X-API-Key': this.client.apiKey } : {};
    const res = await this.client._axios.post('/plugins/install', { plugin_id: pluginId }, { headers });
    return res.data;
  }
}

module.exports = PluginManager;

