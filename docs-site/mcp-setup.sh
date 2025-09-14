#!/bin/bash

# MCP Setup Script for Faxbot
# This script helps set up the MCP server for AI integration

set -e

echo "🔧 Setting up Faxbot MCP Server..."

# Check if .env exists
if [ ! -f .env ]; then
    echo "❌ .env file not found!"
    echo "Please create .env file with your configuration first."
    echo "See README.md for required environment variables."
    exit 1
fi

echo "📦 Installing Node MCP dependencies..."
pushd node_mcp >/dev/null 2>&1 || true
npm install || true
popd >/dev/null 2>&1 || true


echo "✅ MCP setup complete!"
echo ""
echo "🚀 Usage Options:"
echo ""
echo "1. Start MCP stdio server (RECOMMENDED, supports filePath):"
echo "   cd node_mcp && npm run stdio"
echo ""
echo "2. Start with Docker (node_mcp profile)"
echo "   docker compose --profile mcp up -d"
echo ""
echo "📋 MCP Configuration for Claude/Cursor:"
echo ""
echo "Add this to your MCP configuration:"
echo '{'
echo '  "mcpServers": {'
echo '    "faxbot": {'
echo '      "command": "node",'
echo '      "args": ["src/servers/stdio.js"],'
echo '      "cwd": "'$(pwd)/node_mcp'",'
echo '      "env": { "FAX_API_URL": "http://localhost:8080", "API_KEY": "" }'
echo '    }'
echo '  }'
echo '}'
echo ""
echo "🎯 Available MCP Tools:"
echo "- send_fax: Send a fax (stdio supports filePath; HTTP/SSE require base64)"
echo "- get_fax_status: Check fax job status"
echo "(Tools: send_fax, get_fax_status)"
echo ""
echo "🗣️  Voice Assistant Example:"
echo '"Call send_fax with { to: "+1234567890", filePath: "/Users/me/Documents/letter.pdf" }"'
echo ""
echo "📎 File types: only PDF and TXT are accepted. Convert images to PDF first (e.g., macOS: sips -s format pdf in.png --out out.pdf)."
