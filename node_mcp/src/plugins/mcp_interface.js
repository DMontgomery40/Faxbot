/**
 * MCP Plugin Interface - Node-side plugin base for MCP tool exposure.
 * This is independent from backend plugins and does not touch PHI directly.
 */

class MCPPlugin {
  manifest() {
    return { id: 'plugin', name: 'MCP Plugin', version: '1.0.0', capabilities: ['mcp'] };
  }

  /**
   * Return list of tools: [{ name, description, parameters: JSONSchema }]
   */
  getMCPTools() { return []; }

  /**
   * Execute tool by name with params. Should return a plain object result.
   */
  async executeMCPTool(_toolName, _params) { throw new Error('Not implemented'); }

  validateMCPParams(toolName, params) {
    const tools = this.getMCPTools();
    const tool = tools.find(t => t.name === toolName);
    if (!tool) return false;
    const req = (tool.parameters && tool.parameters.required) || [];
    for (const k of req) {
      if (!(k in (params || {}))) return false;
    }
    return true;
  }
}

export { MCPPlugin };

