/**
 * Example MCP plugin: status tools
 */
import { MCPPlugin } from '../mcp_interface.js';
import { getFaxStatus as apiGetFaxStatus } from '../../shared/fax-client.js';

class StatusPlugin extends MCPPlugin {
  manifest() {
    return { id: 'status', name: 'Status Tools', version: '1.0.0', capabilities: ['mcp'] };
  }

  getMCPTools() {
    return [
      {
        name: 'check_fax_status',
        description: 'Check the status of a fax job by jobId',
        parameters: {
          type: 'object',
          properties: { jobId: { type: 'string', description: 'Fax job ID' } },
          required: ['jobId']
        },
      },
    ];
  }

  async executeMCPTool(toolName, params) {
    switch (toolName) {
      case 'check_fax_status':
        return await apiGetFaxStatus(params.jobId);
      default:
        throw new Error(`Unknown tool: ${toolName}`);
    }
  }
}

export default StatusPlugin;

