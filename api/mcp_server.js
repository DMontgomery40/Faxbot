#!/usr/bin/env node

const { Server } = require('@modelcontextprotocol/sdk/server/index.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const {
  CallToolRequestSchema,
  ErrorCode,
  ListToolsRequestSchema,
  McpError,
} = require('@modelcontextprotocol/sdk/types.js');
const axios = require('axios');
const FormData = require('form-data');

class FaxMcpServer {
  constructor() {
    this.server = new Server(
      {
        name: 'faxbot-server',
        version: '2.0.0',
      },
      {
        capabilities: {
          tools: {},
        },
      }
    );

    // Configuration from environment
    this.apiUrl = process.env.FAX_API_URL || 'http://localhost:8080';
    this.apiKey = process.env.API_KEY || '';

    this.setupTools();
  }

  setupTools() {
    // Register available tools
    this.server.setRequestHandler(ListToolsRequestSchema, async () => {
      return {
        tools: [
          {
            name: 'send_fax',
            description: 'Send a fax to a recipient using either Phaxio cloud service or T.38 protocol via Asterisk. Supports PDF and TXT files.',
            inputSchema: {
              type: 'object',
              properties: {
                to: {
                  type: 'string',
                  description: 'The fax number to send to (e.g., "+1234567890" or "555-1234")'
                },
                fileContent: {
                  type: 'string',
                  description: 'Base64 encoded file content (PDF or plain text)'
                },
                fileName: {
                  type: 'string',
                  description: 'Name of the file being sent (e.g., "document.pdf")'
                },
                fileType: {
                  type: 'string',
                  enum: ['pdf', 'txt'],
                  description: 'Type of file being sent (pdf or txt)'
                }
              },
              required: ['to', 'fileContent', 'fileName']
            }
          },
          {
            name: 'get_fax_status',
            description: 'Check the status of a previously sent fax job',
            inputSchema: {
              type: 'object',
              properties: {
                jobId: {
                  type: 'string',
                  description: 'The job ID returned from send_fax'
                }
              },
              required: ['jobId']
            }
          }
        ],
      };
    });

    // Handle tool calls
    this.server.setRequestHandler(CallToolRequestSchema, async (request) => {
      try {
        const { name, arguments: args } = request.params;

        switch (name) {
          case 'send_fax':
            return await this.handleSendFax(args);
          case 'get_fax_status':
            return await this.handleGetFaxStatus(args);
          default:
            throw new McpError(
              ErrorCode.MethodNotFound,
              `Unknown tool: ${name}`
            );
        }
      } catch (error) {
        console.error('MCP tool call error:', {
          tool: request.params.name,
          error: error instanceof Error ? error.message : error
        });

        if (error instanceof McpError) {
          throw error;
        }

        throw new McpError(
          ErrorCode.InternalError,
          `Tool execution failed: ${error instanceof Error ? error.message : 'Unknown error'}`
        );
      }
    });
  }

  async handleSendFax(args) {
    try {
      // Validate inputs
      if (!args.to || !args.fileContent || !args.fileName) {
        throw new McpError(
          ErrorCode.InvalidParams,
          'Missing required parameters: to, fileContent, fileName'
        );
      }

      // Determine file type from extension if not provided
      let fileType = args.fileType;
      if (!fileType) {
        const ext = args.fileName.split('.').pop()?.toLowerCase();
        if (ext === 'pdf') {
          fileType = 'pdf';
        } else if (ext === 'txt') {
          fileType = 'txt';
        } else {
          throw new McpError(
            ErrorCode.InvalidParams,
            'Could not determine file type. Please specify fileType as "pdf" or "txt"'
          );
        }
      }

      if (!['pdf', 'txt'].includes(fileType)) {
        throw new McpError(
          ErrorCode.InvalidParams,
          'fileType must be either "pdf" or "txt"'
        );
      }

      // Validate phone number format
      const phoneRegex = /^[\+]?[\d\s\-\(\)]{7,20}$/;
      if (!phoneRegex.test(args.to.replace(/\s/g, ''))) {
        throw new McpError(
          ErrorCode.InvalidParams,
          'Invalid recipient number format'
        );
      }

      // Decode base64 content
      let fileBuffer;
      try {
        fileBuffer = Buffer.from(args.fileContent, 'base64');
      } catch (error) {
        throw new McpError(
          ErrorCode.InvalidParams,
          'Invalid base64 encoded file content'
        );
      }

      if (fileBuffer.length === 0) {
        throw new McpError(
          ErrorCode.InvalidParams,
          'File content is empty'
        );
      }

      // Create form data for the API request
      const formData = new FormData();
      formData.append('to', args.to.replace(/\s/g, ''));
      formData.append('file', fileBuffer, {
        filename: args.fileName,
        contentType: fileType === 'pdf' ? 'application/pdf' : 'text/plain'
      });

      // Make request to the FastAPI backend
      const headers = {
        ...formData.getHeaders()
      };

      if (this.apiKey) {
        headers['X-API-Key'] = this.apiKey;
      }

      const response = await axios.post(`${this.apiUrl}/fax`, formData, {
        headers,
        timeout: 30000
      });

      if (response.data) {
        console.log('MCP fax job queued successfully:', {
          jobId: response.data.id,
          recipient: args.to,
          fileType: fileType
        });

        return {
          content: [
            {
              type: 'text',
              text: `Fax queued successfully!\n\nJob ID: ${response.data.id}\nRecipient: ${args.to}\nFile: ${args.fileName} (${fileType})\nStatus: ${response.data.status}\n\nUse get_fax_status with job ID "${response.data.id}" to check progress.`
            }
          ]
        };
      } else {
        throw new McpError(
          ErrorCode.InternalError,
          'Invalid response from fax API'
        );
      }

    } catch (error) {
      if (error instanceof McpError) {
        throw error;
      }

      if (error.response) {
        const statusCode = error.response.status;
        const errorMessage = error.response.data?.detail || error.response.statusText;
        
        if (statusCode === 401) {
          throw new McpError(
            ErrorCode.InvalidParams,
            'Invalid API key or authentication failed'
          );
        } else if (statusCode === 413) {
          throw new McpError(
            ErrorCode.InvalidParams,
            'File too large - exceeds maximum size limit'
          );
        } else if (statusCode === 415) {
          throw new McpError(
            ErrorCode.InvalidParams,
            'Unsupported file type - only PDF and TXT are allowed'
          );
        }

        throw new McpError(
          ErrorCode.InternalError,
          `Fax API error (${statusCode}): ${errorMessage}`
        );
      }

      console.error('MCP send_fax error:', {
        error: error instanceof Error ? error.message : error,
        args
      });

      throw new McpError(
        ErrorCode.InternalError,
        `Send fax failed: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  async handleGetFaxStatus(args) {
    try {
      if (!args.jobId) {
        throw new McpError(
          ErrorCode.InvalidParams,
          'Job ID is required'
        );
      }

      const headers = {};
      if (this.apiKey) {
        headers['X-API-Key'] = this.apiKey;
      }

      const response = await axios.get(`${this.apiUrl}/fax/${args.jobId}`, {
        headers,
        timeout: 10000
      });

      if (!response.data) {
        throw new McpError(
          ErrorCode.InternalError,
          'Invalid response from fax API'
        );
      }

      const job = response.data;
      const statusText = this.formatJobStatus(job);

      console.log('MCP fax status retrieved:', {
        jobId: args.jobId,
        status: job.status
      });

      return {
        content: [
          {
            type: 'text',
            text: statusText
          }
        ]
      };

    } catch (error) {
      if (error instanceof McpError) {
        throw error;
      }

      if (error.response) {
        const statusCode = error.response.status;
        
        if (statusCode === 404) {
          throw new McpError(
            ErrorCode.InvalidParams,
            `Fax job not found: ${args.jobId}`
          );
        } else if (statusCode === 401) {
          throw new McpError(
            ErrorCode.InvalidParams,
            'Invalid API key or authentication failed'
          );
        }

        throw new McpError(
          ErrorCode.InternalError,
          `Fax API error (${statusCode}): ${error.response.statusText}`
        );
      }

      console.error('MCP get_fax_status error:', {
        error: error instanceof Error ? error.message : error,
        jobId: args.jobId
      });

      throw new McpError(
        ErrorCode.InternalError,
        `Get fax status failed: ${error instanceof Error ? error.message : 'Unknown error'}`
      );
    }
  }

  formatJobStatus(job) {
    let statusText = `Fax Job Status\n\n`;
    statusText += `Job ID: ${job.id}\n`;
    statusText += `Status: ${job.status}\n`;
    statusText += `Recipient: ${job.to}\n`;
    
    if (job.pages) {
      statusText += `Pages: ${job.pages}\n`;
    }
    
    statusText += `Created: ${new Date(job.created_at).toLocaleString()}\n`;
    statusText += `Updated: ${new Date(job.updated_at).toLocaleString()}\n`;
    
    if (job.error) {
      statusText += `Error: ${job.error}\n`;
    }

    // Add status-specific information
    switch (job.status) {
      case 'queued':
        statusText += `\nThe fax is queued and waiting to be processed by Asterisk.`;
        break;
      case 'in_progress':
        statusText += `\nThe fax is currently being sent via T.38 protocol through Asterisk.`;
        break;
      case 'SUCCESS':
        statusText += `\nThe fax was sent successfully via T.38!`;
        break;
      case 'FAILED':
      case 'failed':
        statusText += `\nThe fax transmission failed. Check the error message above for details.`;
        break;
      case 'disabled':
        statusText += `\nFax transmission is disabled (test mode).`;
        break;
    }

    return statusText;
  }

  async start() {
    const transport = new StdioServerTransport();
    
    console.log('Starting Faxbot MCP server...');
    console.log(`API URL: ${this.apiUrl}`);
    console.log(`API Key: ${this.apiKey ? 'configured' : 'not configured'}`);
    
    await this.server.connect(transport);
    
    console.log('Faxbot MCP server started successfully');
  }

  async stop() {
    console.log('Stopping MCP server');
    await this.server.close();
    console.log('MCP server stopped');
  }
}

// Start the server
async function main() {
  const server = new FaxMcpServer();
  
  // Handle graceful shutdown
  process.on('SIGINT', async () => {
    console.log('\nReceived SIGINT, shutting down gracefully...');
    await server.stop();
    process.exit(0);
  });

  process.on('SIGTERM', async () => {
    console.log('\nReceived SIGTERM, shutting down gracefully...');
    await server.stop();
    process.exit(0);
  });

  try {
    await server.start();
  } catch (error) {
    console.error('Failed to start MCP server:', error);
    process.exit(1);
  }
}

if (require.main === module) {
  main();
}

module.exports = { FaxMcpServer };
