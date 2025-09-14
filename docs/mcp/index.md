---
layout: default
title: MCP Integration
nav_order: 30
has_children: true
permalink: /mcp/
---

# MCP Integration

Faxbot provides MCP servers in Node and Python with identical tools:
- Tools: `send_fax`, `get_fax_status`
- Transports:
  - stdio (local desktop assistants)
  - HTTP (Node streamable HTTP)
  - SSE + OAuth2 (Node and Python)

Choose a server:
- [Python MCP](/Faxbot/mcp/python/)
- [Node MCP](/Faxbot/mcp/node/)
- [Transports](/Faxbot/mcp/transports/)

Limits and file handling
- stdio: use `filePath` to avoid base64 limits
- HTTP/SSE: JSON limit is ~16 MB for Node; REST API raw file limit is 10 MB
- Allowed types: PDF, TXT

