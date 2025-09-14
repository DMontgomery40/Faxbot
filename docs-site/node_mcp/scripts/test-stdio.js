#!/usr/bin/env node
import { spawn } from 'child_process';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import fs from 'fs';
import path from 'path';

async function main() {
  const to = process.argv[2];
  const filePath = process.argv[3];
  if (!to || !filePath) {
    console.error('Usage: node scripts/test-stdio.js "+15551234567" /abs/path/file.txt|.pdf');
    process.exit(1);
  }
  if (!fs.existsSync(filePath)) {
    console.error('File not found:', filePath);
    process.exit(1);
  }

  // Start server process
  const serverProc = spawn('node', [path.join('src', 'servers', 'stdio.js')], {
    stdio: ['pipe', 'pipe', 'inherit'],
    env: { ...process.env, FAX_API_URL: process.env.FAX_API_URL || 'http://localhost:8080', API_KEY: process.env.API_KEY || '' },
    cwd: path.join(process.cwd()),
  });

  const transport = new StdioClientTransport({
    input: serverProc.stdout,
    output: serverProc.stdin,
  });
  const client = new Client(transport);
  await client.connect();

  // Call tool
  const call = await client.callTool({ name: 'send_fax', arguments: { to, filePath } });
  console.log(JSON.stringify(call, null, 2));

  serverProc.kill();
}

main().catch((err) => {
  console.error('Test failed:', err);
  process.exit(1);
});

