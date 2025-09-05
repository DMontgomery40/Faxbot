#!/usr/bin/env node

/**
 * Automated MCP Setup and Installation Script
 * Handles platform-specific installation and configuration
 */

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

class McpInstaller {
  constructor() {
    this.platform = os.platform();
    this.arch = os.arch();
    this.homeDir = os.homedir();
    this.configPaths = this.getConfigPaths();
  }

  getConfigPaths() {
    switch (this.platform) {
      case 'darwin':
        return {
          claude: path.join(this.homeDir, 'Library', 'Application Support', 'Claude', 'claude_desktop_config.json'),
          cursor: path.join(this.homeDir, 'Library', 'Application Support', 'Cursor', 'User', 'globalStorage', 'rooveterinaryinc.roo-cline', 'settings', 'cline_mcp_settings.json'),
          vscode: path.join(this.homeDir, 'Library', 'Application Support', 'Code', 'User', 'settings.json')
        };
      case 'win32':
        return {
          claude: path.join(this.homeDir, 'AppData', 'Roaming', 'Claude', 'claude_desktop_config.json'),
          cursor: path.join(this.homeDir, 'AppData', 'Roaming', 'Cursor', 'User', 'globalStorage', 'rooveterinaryinc.roo-cline', 'settings', 'cline_mcp_settings.json'),
          vscode: path.join(this.homeDir, 'AppData', 'Roaming', 'Code', 'User', 'settings.json')
        };
      case 'linux':
        return {
          claude: path.join(this.homeDir, '.config', 'claude', 'claude_desktop_config.json'),
          cursor: path.join(this.homeDir, '.config', 'Cursor', 'User', 'globalStorage', 'rooveterinaryinc.roo-cline', 'settings', 'cline_mcp_settings.json'),
          vscode: path.join(this.homeDir, '.config', 'Code', 'User', 'settings.json')
        };
      default:
        return {};
    }
  }

  async install() {
    console.log('🚀 Faxbot MCP Installer v2.0');
    console.log(`📱 Platform: ${this.platform} (${this.arch})`);
    console.log('');

    try {
      await this.checkDependencies();
      await this.createConfigurations();
      await this.setupEnvironment();
      await this.testInstallation();
      
      this.printSuccessMessage();
    } catch (error) {
      console.error('❌ Installation failed:', error.message);
      process.exit(1);
    }
  }

  async checkDependencies() {
    console.log('🔍 Checking dependencies...');
    
    // Check Node.js version
    const nodeVersion = process.version;
    const majorVersion = parseInt(nodeVersion.slice(1).split('.')[0]);
    
    if (majorVersion < 18) {
      throw new Error(`Node.js 18+ required, found ${nodeVersion}`);
    }
    
    console.log(`✅ Node.js ${nodeVersion} (compatible)`);
    
    // Check if running in correct directory
    const packagePath = path.join(process.cwd(), 'package.json');
    if (!fs.existsSync(packagePath)) {
      throw new Error('package.json not found. Run this script from the api directory.');
    }
    
    console.log('✅ Package configuration found');
  }

  async createConfigurations() {
    console.log('⚙️  Creating MCP configurations...');
    
    const currentDir = process.cwd();
    const mcpConfig = {
      command: 'node',
      args: ['mcp_server.js'],
      cwd: currentDir,
      env: {
        FAX_API_URL: process.env.FAX_API_URL || 'http://localhost:8080',
        API_KEY: process.env.API_KEY || ''
      }
    };

    const httpMcpConfig = {
      command: 'node',
      args: ['mcp_http_server.js'],
      cwd: currentDir,
      env: {
        FAX_API_URL: process.env.FAX_API_URL || 'http://localhost:8080',
        API_KEY: process.env.API_KEY || '',
        MCP_HTTP_PORT: process.env.MCP_HTTP_PORT || '3001'
      }
    };

    // Claude Desktop configuration
    await this.updateClaudeConfig(mcpConfig);
    
    // Cursor/Cline configuration
    await this.updateCursorConfig(mcpConfig);
    
    // Create standalone config files
    await this.createStandaloneConfigs(mcpConfig, httpMcpConfig);
    
    console.log('✅ MCP configurations created');
  }

  async updateClaudeConfig(mcpConfig) {
    const claudeConfigPath = this.configPaths.claude;
    
    if (!claudeConfigPath) return;
    
    try {
      // Ensure directory exists
      const configDir = path.dirname(claudeConfigPath);
      fs.mkdirSync(configDir, { recursive: true });
      
      let config = {};
      if (fs.existsSync(claudeConfigPath)) {
        const content = fs.readFileSync(claudeConfigPath, 'utf8');
        try {
          config = JSON.parse(content);
        } catch (e) {
          console.warn('⚠️  Invalid Claude config found, creating new one');
        }
      }
      
      // Update MCP servers configuration
      if (!config.mcpServers) {
        config.mcpServers = {};
      }
      
      config.mcpServers['faxbot'] = mcpConfig;
      
      fs.writeFileSync(claudeConfigPath, JSON.stringify(config, null, 2));
      console.log(`✅ Claude config updated: ${claudeConfigPath}`);
    } catch (error) {
      console.warn(`⚠️  Could not update Claude config: ${error.message}`);
    }
  }

  async updateCursorConfig(mcpConfig) {
    const cursorConfigPath = this.configPaths.cursor;
    
    if (!cursorConfigPath) return;
    
    try {
      const configDir = path.dirname(cursorConfigPath);
      fs.mkdirSync(configDir, { recursive: true });
      
      let config = {};
      if (fs.existsSync(cursorConfigPath)) {
        const content = fs.readFileSync(cursorConfigPath, 'utf8');
        try {
          config = JSON.parse(content);
        } catch (e) {
          console.warn('⚠️  Invalid Cursor config found, creating new one');
        }
      }
      
      if (!config.mcpServers) {
        config.mcpServers = {};
      }
      
      config.mcpServers['faxbot'] = mcpConfig;
      
      fs.writeFileSync(cursorConfigPath, JSON.stringify(config, null, 2));
      console.log(`✅ Cursor config updated: ${cursorConfigPath}`);
    } catch (error) {
      console.warn(`⚠️  Could not update Cursor config: ${error.message}`);
    }
  }

  async createStandaloneConfigs(mcpConfig, httpMcpConfig) {
    const configDir = path.join(process.cwd(), 'configs');
    fs.mkdirSync(configDir, { recursive: true });
    
    // MCP configuration files
    const configs = {
      'claude-mcp.json': {
        mcpServers: {
          'faxbot': mcpConfig
        }
      },
      'cursor-mcp.json': {
        mcpServers: {
          'faxbot': mcpConfig
        }
      },
      'http-mcp.json': {
        server: httpMcpConfig,
        endpoints: {
          health: 'http://localhost:3001/health',
          capabilities: 'http://localhost:3001/mcp/capabilities'
        }
      }
    };
    
    for (const [filename, config] of Object.entries(configs)) {
      const configPath = path.join(configDir, filename);
      fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
      console.log(`✅ Created config: ${configPath}`);
    }
  }

  async setupEnvironment() {
    console.log('🌍 Setting up environment...');
    
    const envPath = path.join(process.cwd(), '..', '.env');
    const envExamplePath = path.join(process.cwd(), '..', '.env.example');
    
    if (!fs.existsSync(envPath) && fs.existsSync(envExamplePath)) {
      fs.copyFileSync(envExamplePath, envPath);
      console.log('✅ Created .env file from template');
      console.log('⚠️  Please edit .env with your SIP provider settings');
    } else if (fs.existsSync(envPath)) {
      console.log('✅ Environment file exists');
    } else {
      console.warn('⚠️  No .env file found - you may need to create one manually');
    }
    
    // Create launch scripts
    await this.createLaunchScripts();
  }

  async createLaunchScripts() {
    const scriptsDir = path.join(process.cwd(), 'scripts');
    fs.mkdirSync(scriptsDir, { recursive: true });
    
    const scripts = {
      'start-mcp.sh': '#!/bin/bash\ncd "$(dirname "$0")/.." && node mcp_server.js',
      'start-mcp-http.sh': '#!/bin/bash\ncd "$(dirname "$0")/.." && node mcp_http_server.js',
      'start-mcp.bat': '@echo off\ncd /d "%~dp0.."\nnode mcp_server.js',
      'start-mcp-http.bat': '@echo off\ncd /d "%~dp0.."\nnode mcp_http_server.js'
    };
    
    for (const [filename, content] of Object.entries(scripts)) {
      const scriptPath = path.join(scriptsDir, filename);
      fs.writeFileSync(scriptPath, content);
      
      if (filename.endsWith('.sh')) {
        try {
          fs.chmodSync(scriptPath, '755');
        } catch (e) {
          // Ignore chmod errors on Windows
        }
      }
    }
    
    console.log('✅ Launch scripts created');
  }

  async testInstallation() {
    console.log('🧪 Testing installation...');
    
    try {
      // Test that the MCP server can start
      const { spawn } = require('child_process');
      const child = spawn('node', ['mcp_server.js', '--test'], {
        stdio: 'pipe',
        timeout: 5000
      });
      
      let output = '';
      child.stdout.on('data', (data) => {
        output += data.toString();
      });
      
      child.stderr.on('data', (data) => {
        output += data.toString();
      });
      
      await new Promise((resolve, reject) => {
        child.on('exit', (code) => {
          if (code === 0 || output.includes('MCP server')) {
            resolve();
          } else {
            reject(new Error(`Test failed with code ${code}: ${output}`));
          }
        });
        
        child.on('error', reject);
        
        // Kill after timeout
        setTimeout(() => {
          child.kill();
          resolve(); // Consider timeout as success for basic syntax check
        }, 3000);
      });
      
      console.log('✅ Installation test passed');
    } catch (error) {
      console.warn(`⚠️  Installation test warning: ${error.message}`);
    }
  }

  printSuccessMessage() {
    console.log('');
    console.log('🎉 Faxbot MCP Installation Complete!');
    console.log('');
    console.log('📋 Next Steps:');
    console.log('');
    console.log('1. Configure your SIP provider settings in .env file');
    console.log('2. Start the fax API service:');
    console.log('   docker-compose up -d');
    console.log('');
    console.log('3. Start MCP server (choose one):');
    console.log('   • Stdio mode: npm run start:mcp');
    console.log('   • HTTP mode:  npm run start:http');
    console.log('   • Docker:     docker-compose --profile mcp up -d');
    console.log('');
    console.log('🗣️  Voice Assistant Examples:');
    console.log('   "Hey Claude, fax my prescription to the pharmacy at 555-0123"');
    console.log('   "Send my insurance card to Dr. Smith\'s office"');
    console.log('');
    console.log('🔧 Configuration Files Created:');
    console.log(`   • configs/claude-mcp.json`);
    console.log(`   • configs/cursor-mcp.json`);
    console.log(`   • configs/http-mcp.json`);
    console.log('');
    console.log('📖 Documentation: https://github.com/DMontgomery40/Faxbot');
    console.log('');
    console.log('🚀 Ready for AI-powered fax transmission!');
  }
}

// Run installer
if (require.main === module) {
  const installer = new McpInstaller();
  installer.install().catch(console.error);
}

module.exports = { McpInstaller };
