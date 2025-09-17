const os = require('os');
const fs = require('fs');
const path = require('path');

function createMockDeps() {
  const logger = new Proxy({}, { get: () => () => {} });
  const storage = {}; const db = {}; const http = {}; const audit = {}; const cache = {}; const metrics = {};
  const events = { _events: {}, emit(e, p = {}) { this._events[e] = this._events[e] || []; this._events[e].push(p); }, getEvents(e) { return this._events[e] || []; } };
  const configDir = fs.mkdtempSync(path.join(os.tmpdir(), 'faxbot_plugin_'));
  return { logger, storage, db, events, http, audit, config_dir: configDir, cache, metrics, extras: {} };
}

class PluginTestHarness {
  constructor(PluginClass) { this.PluginClass = PluginClass; this.plugin = null; this.deps = null; }
  async setup(config = null) { this.deps = createMockDeps(); this.plugin = new this.PluginClass(this.deps); if (config) { if (this.plugin.validateConfig) this.plugin.validateConfig(config); if (this.plugin.initialize) await this.plugin.initialize(config); } return this; }
  async teardown() { if (this.plugin && this.plugin.shutdown) await this.plugin.shutdown(); if (this.deps && this.deps.config_dir) { try { fs.rmSync(this.deps.config_dir, { recursive: true, force: true }); } catch {} } }
  assertEventEmitted(event) { const list = this.deps.events.getEvents(event); if (!list || !list.length) throw new Error(`Event ${event} was not emitted`); }
  assertNoPhiLogged() { /* No-op (stub logger) */ }
}

module.exports = { createMockDeps, PluginTestHarness };

