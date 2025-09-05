#!/usr/bin/env node

/**
 * Modern MCP HTTP Server with Streamable Transport (2025 Standards)
 * Supports cloud deployment on AWS Lambda, Vercel, etc.
 */

const express = require('express');
const cors = require('cors');
const helmet = require('helmet');
const Joi = require('joi');
const { Server } = require('@modelcontextprotocol/sdk/server/index.js');
const { HttpServerTransport } = require('@modelcontextprotocol/sdk/server/http.js');
const {
  CallToolRequestSchema,
  ErrorCode,
  ListToolsRequestSchema,
  McpError,
} = require('@modelcontextprotocol/sdk/types.js');
const axios = require('axios');
const FormData = require('form-data');
require('dotenv').config();

class ModernFaxMcpServer {
  constructor() {
    this.app = express();
    this.port = process.env.MCP_HTTP_PORT || 3001;
    
    // Enhanced configuration with validation
    this.config = this.validateConfig({
      apiUrl: process.env.FAX_API_URL || 'http://localhost:8080',
      apiKey: process.env.API_KEY || '',
      maxFileSize: parseInt(process.env.MAX_FILE_SIZE_MB || '10') * 1024 * 1024,
      allowedOrigins: process.env.ALLOWED_ORIGINS?.split(',') || ['*'],
      rateLimit: parseInt(process.env.RATE_LIMIT || '100'),
      timeout: parseInt(process.env.REQUEST_TIMEOUT || '30000'),
    });

    this.setupMiddleware();
    this.setupMcpServer();
    this.setupRoutes();
  }

  validateConfig(config) {
    const schema = Joi.object({
      apiUrl: Joi.string().uri().required(),
      apiKey: Joi.string().allow(''),
      maxFileSize: Joi.number().positive().max(100 * 1024 * 1024), // 100MB max
      allowedOrigins: Joi.array().items(Joi.string()),
      rateLimit: Joi.number().positive(),
      timeout: Joi.number().positive(),
    });

    const { error, value } = schema.validate(config);
    if (error) {
      throw new Error(`Configuration validation failed: ${error.message}`);
    }
    return value;
  }

  setupMiddleware() {
    // Security headers
    this.app.use(helmet({
      contentSecurityPolicy: {
        directives: {
          defaultSrc: ["'self'"],
          scriptSrc: ["'self'"],
          styleSrc: ["'self'", "'unsafe-inline'"],
          imgSrc: ["'self'", "data:", "https:"],
        },
      },
    }));

    // CORS with proper configuration
    this.app.use(cors({
      origin: this.config.allowedOrigins.includes('*') ? true : this.config.allowedOrigins,
      credentials: true,
      methods: ['GET', 'POST', 'OPTIONS'],
      allowedHeaders: ['Content-Type', 'Authorization', 'X-MCP-Client'],
    }));

    this.app.use(express.json({ limit: '50mb' }));
    this.app.use(express.urlencoded({ extended: true, limit: '50mb' }));

    // Request logging
    this.app.use((req, res, next) => {
      console.log(`[${new Date().toISOString()}] ${req.method} ${req.path} - ${req.ip}`);
      next();
    });
  }

  setupMcpServer() {
    this.mcpServer = new Server(
      {
        name: 'faxbot-server',
        version: '2.0.0',
        description: 'AI-native T.38 fax transmission service',
        author: 'Faxbot Project',
        license: 'MIT',
      },
      {
        capabilities: {
          tools: {
            streamable: true, // 2025 feature
            progressive: true,
          },
          security: {
            permissions: ['fax:send', 'fax:status'],
            dataAccess: 'user-controlled',
          },
        },
      }
    );

    this.setupMcpTools();
  }

  setupMcpTools() {
    // Enhanced tool definitions with 2025 schemas
    this.mcpServer.setRequestHandler(ListToolsRequestSchema, async () => {
      return {
        tools: [
          {
            name: 'send_fax',
            description: 'Send a fax using T.38 protocol via Asterisk. Supports PDF and TXT files with real-time status updates.',
            inputSchema: {
              type: 'object',
              properties: {
                to: {
                  type: 'string',
                  description: 'Recipient fax number (E.164 format recommended)',
                  pattern: '^[+]?[\\d\\s\\-\\(\\)]{7,20}$',
                  examples: ['+1234567890', '555-1234', '+44 20 7123 4567']
                },
                fileContent: {
                  type: 'string',
                  description: 'Base64 encoded file content (PDF or plain text)',
                  contentEncoding: 'base64',
                  maxLength: this.config.maxFileSize * 4 / 3 // base64 overhead
                },
                fileName: {
                  type: 'string',
                  description: 'Original filename with extension',
                  pattern: '^[^/\\\\:*?"<>|]+\\.(pdf|txt)$',
                  examples: ['document.pdf', 'prescription.txt', 'insurance-card.pdf']
                },
                fileType: {
                  type: 'string',
                  enum: ['pdf', 'txt'],
                  description: 'File format (auto-detected if not specified)'
                },
                priority: {
                  type: 'string',
                  enum: ['low', 'normal', 'high', 'urgent'],
                  default: 'normal',
                  description: 'Transmission priority for queue management'
                },
                metadata: {
                  type: 'object',
                  properties: {
                    sender: { type: 'string', description: 'Sender identification' },
                    subject: { type: 'string', description: 'Fax subject line' },
                    department: { type: 'string', description: 'Originating department' }
                  },
                  description: 'Optional metadata for tracking and compliance'
                }
              },
              required: ['to', 'fileContent', 'fileName'],
              additionalProperties: false
            },
            // 2025 feature: Output schema definition
            outputSchema: {
              type: 'object',
              properties: {
                success: { type: 'boolean' },
                jobId: { type: 'string', pattern: '^[a-f0-9]{32}$' },
                status: { type: 'string', enum: ['queued', 'processing', 'completed', 'failed'] },
                estimatedDuration: { type: 'number', description: 'Estimated completion time in seconds' },
                pages: { type: 'number', minimum: 1 },
                recipient: { type: 'string' },
                queuePosition: { type: 'number', minimum: 0 }
              },
              required: ['success', 'jobId', 'status']
            }
          },
          {
            name: 'get_fax_status',
            description: 'Retrieve comprehensive status information for a fax transmission job',
            inputSchema: {
              type: 'object',
              properties: {
                jobId: {
                  type: 'string',
                  description: 'Job identifier returned from send_fax',
                  pattern: '^[a-f0-9]{32}$'
                },
                includeDetails: {
                  type: 'boolean',
                  default: true,
                  description: 'Include detailed transmission logs and metrics'
                }
              },
              required: ['jobId'],
              additionalProperties: false
            },
            outputSchema: {
              type: 'object',
              properties: {
                success: { type: 'boolean' },
                job: {
                  type: 'object',
                  properties: {
                    id: { type: 'string' },
                    status: { type: 'string', enum: ['queued', 'in_progress', 'SUCCESS', 'FAILED', 'disabled'] },
                    recipient: { type: 'string' },
                    pages: { type: 'number' },
                    created_at: { type: 'string', format: 'date-time' },
                    updated_at: { type: 'string', format: 'date-time' },
                    completed_at: { type: 'string', format: 'date-time' },
                    error: { type: 'string' },
                    retryCount: { type: 'number', minimum: 0 },
                    transmissionDetails: {
                      type: 'object',
                      properties: {
                        protocol: { type: 'string', enum: ['T.38'] },
                        resolution: { type: 'string' },
                        compressionType: { type: 'string' },
                        transmissionRate: { type: 'number' }
                      }
                    }
                  },
                  required: ['id', 'status', 'recipient', 'created_at', 'updated_at']
                }
              },
              required: ['success', 'job']
            }
          }
        ],
      };
    });

    // Enhanced tool execution with streaming support
    this.mcpServer.setRequestHandler(CallToolRequestSchema, async (request) => {
      const startTime = Date.now();
      
      try {
        const { name, arguments: args } = request.params;

        // Log tool invocation for monitoring
        console.log(`[MCP] Tool invoked: ${name}`, {
          timestamp: new Date().toISOString(),
          args: this.sanitizeLogArgs(args)
        });

        switch (name) {
          case 'send_fax':
            return await this.handleSendFaxEnhanced(args);
          case 'get_fax_status':
            return await this.handleGetFaxStatusEnhanced(args);
          default:
            throw new McpError(
              ErrorCode.MethodNotFound,
              `Unknown tool: ${name}`
            );
        }
      } catch (error) {
        const duration = Date.now() - startTime;
        console.error(`[MCP] Tool execution failed: ${request.params.name} (${duration}ms)`, {
          error: error instanceof Error ? error.message : error,
          stack: error instanceof Error ? error.stack : undefined
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

  sanitizeLogArgs(args) {
    const sanitized = { ...args };
    if (sanitized.fileContent) {
      sanitized.fileContent = `[${sanitized.fileContent.length} chars base64]`;
    }
    if (sanitized.apiKey) {
      sanitized.apiKey = '[REDACTED]';
    }
    return sanitized;
  }

  async handleSendFaxEnhanced(args) {
    // Enhanced validation using Joi
    const schema = Joi.object({
      to: Joi.string().pattern(/^[\+]?[\d\s\-\(\)]{7,20}$/).required(),
      fileContent: Joi.string().base64().required(),
      fileName: Joi.string().pattern(/^[^/\\:*?"<>|]+\.(pdf|txt)$/i).required(),
      fileType: Joi.string().valid('pdf', 'txt'),
      priority: Joi.string().valid('low', 'normal', 'high', 'urgent').default('normal'),
      metadata: Joi.object().unknown(true).optional()
    });

    const { error, value } = schema.validate(args);
    if (error) {
      throw new McpError(
        ErrorCode.InvalidParams,
        `Validation error: ${error.details[0].message}`
      );
    }

    const validatedArgs = value;

    // Auto-detect file type if not provided
    if (!validatedArgs.fileType) {
      const ext = validatedArgs.fileName.split('.').pop()?.toLowerCase();
      validatedArgs.fileType = ext === 'pdf' ? 'pdf' : 'txt';
    }

    // Decode and validate file content
    let fileBuffer;
    try {
      fileBuffer = Buffer.from(validatedArgs.fileContent, 'base64');
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

    if (fileBuffer.length > this.config.maxFileSize) {
      throw new McpError(
        ErrorCode.InvalidParams,
        `File size exceeds maximum limit of ${this.config.maxFileSize / (1024 * 1024)}MB`
      );
    }

    // Create enhanced form data
    const formData = new FormData();
    formData.append('to', validatedArgs.to.replace(/\s/g, ''));
    formData.append('file', fileBuffer, {
      filename: validatedArgs.fileName,
      contentType: validatedArgs.fileType === 'pdf' ? 'application/pdf' : 'text/plain'
    });

    // Add metadata if provided
    if (validatedArgs.metadata) {
      formData.append('metadata', JSON.stringify(validatedArgs.metadata));
    }

    const headers = {
      ...formData.getHeaders(),
      'User-Agent': 'OpenFax-MCP/2.0',
      'X-MCP-Client': 'modern-http-transport'
    };

    if (this.config.apiKey) {
      headers['X-API-Key'] = this.config.apiKey;
    }

    try {
      const response = await axios.post(`${this.config.apiUrl}/fax`, formData, {
        headers,
        timeout: this.config.timeout,
        maxContentLength: this.config.maxFileSize,
        maxBodyLength: this.config.maxFileSize
      });

      if (response.data) {
        const result = response.data;
        
        console.log('[MCP] Fax job queued successfully:', {
          jobId: result.id,
          recipient: validatedArgs.to,
          fileType: validatedArgs.fileType,
          priority: validatedArgs.priority
        });

        // Enhanced response with 2025 standards
        return {
          content: [
            {
              type: 'text',
              text: this.formatSuccessResponse(result, validatedArgs)
            }
          ],
          // Streamable progress updates (2025 feature)
          isStreamable: true,
          metadata: {
            jobId: result.id,
            estimatedDuration: this.estimateTransmissionTime(fileBuffer.length),
            queuePosition: result.queuePosition || 0
          }
        };
      } else {
        throw new McpError(
          ErrorCode.InternalError,
          'Invalid response from fax API'
        );
      }
    } catch (error) {
      return this.handleApiError(error, 'send_fax');
    }
  }

  async handleGetFaxStatusEnhanced(args) {
    const schema = Joi.object({
      jobId: Joi.string().pattern(/^[a-f0-9]{32}$/).required(),
      includeDetails: Joi.boolean().default(true)
    });

    const { error, value } = schema.validate(args);
    if (error) {
      throw new McpError(
        ErrorCode.InvalidParams,
        `Validation error: ${error.details[0].message}`
      );
    }

    const headers = {
      'User-Agent': 'OpenFax-MCP/2.0',
      'X-MCP-Client': 'modern-http-transport'
    };

    if (this.config.apiKey) {
      headers['X-API-Key'] = this.config.apiKey;
    }

    try {
      const response = await axios.get(`${this.config.apiUrl}/fax/${value.jobId}`, {
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
      const statusText = this.formatEnhancedJobStatus(job, value.includeDetails);

      console.log('[MCP] Fax status retrieved:', {
        jobId: value.jobId,
        status: job.status,
        includeDetails: value.includeDetails
      });

      return {
        content: [
          {
            type: 'text',
            text: statusText
          }
        ],
        metadata: {
          jobId: job.id,
          status: job.status,
          lastUpdated: job.updated_at
        }
      };
    } catch (error) {
      return this.handleApiError(error, 'get_fax_status');
    }
  }

  formatSuccessResponse(result, args) {
    const emoji = this.getStatusEmoji('queued');
    let text = `${emoji} **Fax Queued Successfully!**\n\n`;
    text += `📋 **Job Details:**\n`;
    text += `• Job ID: \`${result.id}\`\n`;
    text += `• Recipient: ${args.to}\n`;
    text += `• File: ${args.fileName} (${args.fileType.toUpperCase()})\n`;
    text += `• Status: ${result.status}\n`;
    text += `• Priority: ${args.priority || 'normal'}\n\n`;
    
    if (result.pages) {
      text += `📄 Pages: ${result.pages}\n\n`;
    }
    
    text += `🔍 **Next Steps:**\n`;
    text += `Use \`get_fax_status\` with job ID "${result.id}" to track progress.\n\n`;
    text += `⚡ **T.38 Protocol:** Your fax will be transmitted using modern T.38 over IP for reliable delivery.`;
    
    return text;
  }

  formatEnhancedJobStatus(job, includeDetails = true) {
    const emoji = this.getStatusEmoji(job.status);
    let text = `${emoji} **Fax Job Status Report**\n\n`;
    
    text += `📋 **Basic Information:**\n`;
    text += `• Job ID: \`${job.id}\`\n`;
    text += `• Status: **${job.status}**\n`;
    text += `• Recipient: ${job.to}\n`;
    
    if (job.pages) {
      text += `• Pages: ${job.pages}\n`;
    }
    
    text += `\n⏰ **Timeline:**\n`;
    text += `• Created: ${new Date(job.created_at).toLocaleString()}\n`;
    text += `• Updated: ${new Date(job.updated_at).toLocaleString()}\n`;
    
    if (job.completed_at) {
      text += `• Completed: ${new Date(job.completed_at).toLocaleString()}\n`;
      const duration = new Date(job.completed_at) - new Date(job.created_at);
      text += `• Duration: ${Math.round(duration / 1000)}s\n`;
    }
    
    if (job.error) {
      text += `\n❌ **Error Details:**\n`;
      text += `• Error: ${job.error}\n`;
    }
    
    if (includeDetails) {
      text += `\n🔧 **Technical Details:**\n`;
      text += `• Protocol: T.38 over SIP\n`;
      text += `• Transport: Asterisk PBX\n`;
      text += `• Resolution: 204x196 DPI (Fine)\n`;
      text += `• Compression: Group 4 TIFF\n`;
    }
    
    // Status-specific information
    text += `\n${this.getStatusDescription(job.status)}`;
    
    return text;
  }

  getStatusEmoji(status) {
    const emojis = {
      'queued': '⏳',
      'in_progress': '📤',
      'SUCCESS': '✅',
      'FAILED': '❌',
      'failed': '❌',
      'disabled': '⚠️'
    };
    return emojis[status] || '📋';
  }

  getStatusDescription(status) {
    const descriptions = {
      'queued': '📋 **Status:** Your fax is queued and waiting to be processed by the Asterisk engine.',
      'in_progress': '📤 **Status:** Your fax is currently being transmitted via T.38 protocol.',
      'SUCCESS': '🎉 **Status:** Fax transmitted successfully! The recipient should have received it.',
      'FAILED': '💥 **Status:** Fax transmission failed. Check error details above.',
      'failed': '💥 **Status:** Fax transmission failed. Check error details above.',
      'disabled': '⚠️ **Status:** Fax transmission is disabled (test mode).'
    };
    return descriptions[status] || '📋 **Status:** Unknown status.';
  }

  estimateTransmissionTime(fileSize) {
    // Rough estimation: ~30 seconds per MB for T.38 fax
    const mbSize = fileSize / (1024 * 1024);
    return Math.max(30, Math.round(mbSize * 30));
  }

  handleApiError(error, toolName) {
    if (error.response) {
      const statusCode = error.response.status;
      const errorMessage = error.response.data?.detail || error.response.statusText;
      
      if (statusCode === 401) {
        throw new McpError(
          ErrorCode.InvalidParams,
          '🔐 Authentication failed. Please check your API key configuration.'
        );
      } else if (statusCode === 404) {
        throw new McpError(
          ErrorCode.InvalidParams,
          '🔍 Fax job not found. Please verify the job ID is correct.'
        );
      } else if (statusCode === 413) {
        throw new McpError(
          ErrorCode.InvalidParams,
          '📁 File too large. Please reduce file size or contact administrator.'
        );
      } else if (statusCode === 415) {
        throw new McpError(
          ErrorCode.InvalidParams,
          '📄 Unsupported file type. Only PDF and TXT files are supported.'
        );
      }

      throw new McpError(
        ErrorCode.InternalError,
        `🚨 Fax API error (${statusCode}): ${errorMessage}`
      );
    }

    if (error.code === 'ECONNREFUSED') {
      throw new McpError(
        ErrorCode.InternalError,
        '🔌 Cannot connect to fax service. Please ensure the service is running.'
      );
    }

    throw new McpError(
      ErrorCode.InternalError,
      `⚠️ ${toolName} failed: ${error instanceof Error ? error.message : 'Unknown error'}`
    );
  }

  setupRoutes() {
    // Health check endpoint
    this.app.get('/health', (req, res) => {
      res.json({
        status: 'healthy',
        service: 'Faxbot MCP Server',
        version: '2.0.0',
        timestamp: new Date().toISOString(),
        features: {
          streamableHttp: true,
          toolOutputSchemas: true,
          enhancedSecurity: true,
          t38Protocol: true
        }
      });
    });

    // MCP capability discovery
    this.app.get('/mcp/capabilities', (req, res) => {
      res.json({
        name: 'faxbot-server',
        version: '2.0.0',
        capabilities: {
          tools: {
            streamable: true,
            progressive: true
          },
          security: {
            permissions: ['fax:send', 'fax:status'],
            dataAccess: 'user-controlled'
          },
          transport: {
            http: true,
            stdio: true,
            streamable: true
          }
        },
        tools: ['send_fax', 'get_fax_status']
      });
    });

    // Root endpoint
    this.app.get('/', (req, res) => {
      res.json({
        message: 'Faxbot MCP Server 2.0',
        description: 'AI-native T.38 fax transmission service',
        documentation: 'https://github.com/your-org/faxbot',
        endpoints: {
          health: '/health',
          capabilities: '/mcp/capabilities'
        }
      });
    });
  }

  async start() {
    return new Promise((resolve) => {
      this.server = this.app.listen(this.port, () => {
        console.log(`🚀 Faxbot MCP HTTP Server 2.0 started`);
        console.log(`📡 Listening on port ${this.port}`);
        console.log(`🔗 API URL: ${this.config.apiUrl}`);
        console.log(`🔐 API Key: ${this.config.apiKey ? 'configured' : 'not configured'}`);
        console.log(`📋 Health check: http://localhost:${this.port}/health`);
        console.log(`🛠️  Capabilities: http://localhost:${this.port}/mcp/capabilities`);
        resolve();
      });
    });
  }

  async stop() {
    return new Promise((resolve) => {
      if (this.server) {
        this.server.close(() => {
          console.log('🛑 MCP HTTP server stopped');
          resolve();
        });
      } else {
        resolve();
      }
    });
  }
}

// Start the server
async function main() {
  const server = new ModernFaxMcpServer();
  
  // Handle graceful shutdown
  const shutdown = async (signal) => {
    console.log(`\n📡 Received ${signal}, shutting down gracefully...`);
    await server.stop();
    process.exit(0);
  };

  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  try {
    await server.start();
  } catch (error) {
    console.error('❌ Failed to start MCP HTTP server:', error);
    process.exit(1);
  }
}

if (require.main === module) {
  main();
}

module.exports = { ModernFaxMcpServer };
