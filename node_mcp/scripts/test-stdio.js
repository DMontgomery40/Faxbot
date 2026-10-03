#!/usr/bin/env node
// Manual smoke test: launch the stdio server and send one fax through it.
// Usage: FAX_API_URL=... API_KEY=... node scripts/test-stdio.js "+15551234567" /path/file.pdf|.txt
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';

const [to, filePath] = process.argv.slice(2);
if (!to || !filePath) {
  console.error('Usage: node scripts/test-stdio.js "+15551234567" /path/file.txt|.pdf');
  process.exit(1);
}
if (!fs.existsSync(filePath)) {
  console.error('File not found:', filePath);
  process.exit(1);
}

const server = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src', 'servers', 'stdio.js');
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [server],
  env: { ...process.env, FAX_API_URL: process.env.FAX_API_URL || 'http://localhost:8080', API_KEY: process.env.API_KEY || '' },
});
const client = new Client({ name: 'faxbot-test-stdio', version: '1.0.0' }, { versionNegotiation: { mode: 'auto' } });
try {
  await client.connect(transport);
  const result = await client.callTool({ name: 'send_fax', arguments: { to, filePath: path.resolve(filePath) } });
  console.log(JSON.stringify(result, null, 2));
} catch (err) {
  console.error('Test failed:', err);
  process.exitCode = 1;
} finally {
  await client.close();
}
