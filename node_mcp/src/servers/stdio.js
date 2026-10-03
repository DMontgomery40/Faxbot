#!/usr/bin/env node
// Faxbot MCP over stdio for local assistants. One integration identity: every
// tool call uses API_KEY from the environment. Stdout carries only JSON-RPC;
// diagnostics go to stderr.
import { pathToFileURL } from 'node:url';
import { serveStdio } from '@modelcontextprotocol/server/stdio';
import { buildServer } from '../tools/fax-tools.js';

export function startStdio() {
  const options = { baseUrl: process.env.FAX_API_URL || 'http://localhost:8080', apiKey: process.env.API_KEY || '', localFiles: true };
  return serveStdio(() => buildServer(options));
}

export { buildServer };

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    startStdio();
  } catch (err) {
    console.error('Failed to start the Faxbot stdio MCP server:', err);
    process.exit(1);
  }
}
