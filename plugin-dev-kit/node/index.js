class PluginBase {
  constructor(deps = {}) {
    this.deps = deps;
    this.config = {};
    this._initialized = false;
  }
  manifest() { throw new Error('manifest() not implemented'); }
  validateConfig(_) { throw new Error('validateConfig() not implemented'); }
  async initialize(_) { throw new Error('initialize() not implemented'); }
  async shutdown() { throw new Error('shutdown() not implemented'); }
  async healthCheck() {
    const m = this.manifest();
    return { status: this._initialized ? 'healthy' : 'not_initialized', plugin: m.id, version: m.version };
  }
}

class FaxPlugin extends PluginBase {
  async sendFax(_, __, ___) { throw new Error('sendFax() not implemented'); }
  async getStatus(_) { throw new Error('getStatus() not implemented'); }
}

class StoragePlugin extends PluginBase {
  async put(_, __, ___) { throw new Error('put() not implemented'); }
  async get(_) { throw new Error('get() not implemented'); }
  async delete(_) { throw new Error('delete() not implemented'); }
  async exists(_) { throw new Error('exists() not implemented'); }
  async list(_) { throw new Error('list() not implemented'); }
}

class AuthPlugin extends PluginBase {
  async authenticate(_) { throw new Error('authenticate() not implemented'); }
  async validateToken(_) { throw new Error('validateToken() not implemented'); }
  async refreshToken(_) { throw new Error('refreshToken() not implemented'); }
}

module.exports = { PluginBase, FaxPlugin, StoragePlugin, AuthPlugin };

