---
layout: default
title: AI Integration
nav_order: 4
has_children: true
permalink: /ai-integration/
---

<div class="home-hero">
  <img src="{{ site.baseurl }}/docs/assets/images/faxbot_full_logo.png" alt="Faxbot logo" />
</div>

# AI Assistant Integration

Faxbot integrates with AI assistants through the Model Context Protocol (MCP), allowing you to send faxes using natural language commands like "fax this document to my doctor."

## What is MCP?

MCP (Model Context Protocol) is a standard for connecting AI assistants to external tools and data sources. Faxbot provides MCP servers that expose fax-sending capabilities to AI assistants.

## Available Integrations

- **Claude Desktop**: Use stdio transport for local desktop integration
- **Cursor**: Built-in MCP support with stdio transport  
- **Web Applications**: HTTP transport for cloud-based AI assistants
- **Enterprise**: SSE + OAuth2 transport for secure, authenticated access

## Transport Options

| Transport | Best For | Authentication | Complexity |
|-----------|----------|----------------|------------|
| **stdio** | Desktop AI (Claude, Cursor) | API key | Low |
| **HTTP** | Web apps, cloud AI | API key | Medium |
| **SSE+OAuth** | Enterprise, HIPAA environments | JWT/OAuth2 | High |

## Language Options

Faxbot provides MCP servers in both Node.js and Python with identical functionality. Choose based on your environment preferences.
