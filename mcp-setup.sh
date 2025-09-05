#!/bin/bash

# MCP Setup Script for Open Fax by Codex
# This script helps set up the MCP server for AI integration

set -e

echo "🔧 Setting up Open Fax MCP Server..."

# Check if .env exists
if [ ! -f .env ]; then
    echo "❌ .env file not found!"
    echo "Please create .env file with your configuration first."
    echo "See README.md for required environment variables."
    exit 1
fi

# Install Node.js dependencies
echo "📦 Installing Node.js dependencies..."
cd api
npm install
cd ..

echo "✅ MCP setup complete!"
echo ""
echo "🚀 Usage Options:"
echo ""
echo "1. Start MCP server standalone:"
echo "   cd api && npm run start:mcp"
echo ""
echo "2. Start with Docker (recommended):"
echo "   docker-compose --profile mcp up -d"
echo ""
echo "3. Development mode:"
echo "   cd api && npm run dev:mcp"
echo ""
echo "📋 MCP Configuration for Claude/Cursor:"
echo ""
echo "Add this to your MCP configuration:"
echo '{'
echo '  "mcpServers": {'
echo '    "open-fax": {'
echo '      "command": "node",'
echo '      "args": ["mcp_server.js"],'
echo '      "cwd": "'$(pwd)/api'"'
echo '    }'
echo '  }'
echo '}'
echo ""
echo "🎯 Available MCP Tools:"
echo "- send_fax: Send a fax with base64 content"
echo "- get_fax_status: Check fax job status"
echo ""
echo "🗣️  Voice Assistant Example:"
echo '"Hey Claude, send a fax of my insurance card to Dr. Smith at +1234567890"'
