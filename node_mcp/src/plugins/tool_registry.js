/**
 * MCP Tool Registry - aggregates tools from registered plugins
 */

class MCPToolRegistry {
  constructor() {
    this.tools = new Map(); // fullName -> { ..toolDef, pluginId, execute }
    this.plugins = new Map(); // pluginId -> plugin instance
  }

  registerPlugin(plugin) {
    const m = plugin.manifest();
    const pid = m.id;
    this.plugins.set(pid, plugin);
    const tools = plugin.getMCPTools();
    for (const t of tools) {
      const fullName = `${pid}_${t.name}`;
      this.tools.set(fullName, {
        ...t,
        pluginId: pid,
        execute: async (params) => plugin.executeMCPTool(t.name, params),
      });
    }
  }

  has(toolName) { return this.tools.has(toolName); }
  getAll() { return Array.from(this.tools.entries()); }
  getAllTools() { return Array.from(this.tools.values()); }

  async executeTool(toolName, params) {
    const entry = this.tools.get(toolName);
    if (!entry) throw new Error(`Tool not found: ${toolName}`);
    const plugin = this.plugins.get(entry.pluginId);
    const short = toolName.replace(`${entry.pluginId}_`, '');
    if (!plugin.validateMCPParams(short, params)) {
      throw new Error(`Invalid params for ${toolName}`);
    }
    return await entry.execute(params);
  }
}

export default MCPToolRegistry;

