#!/usr/bin/env node
// Call the send_fax tool handler directly, without an MCP client.
// Usage: FAX_API_URL=... API_KEY=... node scripts/call-send-fax.js "+15551234567" /path/file.pdf|.txt
import fs from 'node:fs';
import path from 'node:path';
import { createFaxClient } from '../src/shared/fax-client.js';
import { faxToolDefinitions } from '../src/tools/fax-tools.js';

const [to, filePath] = process.argv.slice(2);
if (!to || !filePath) {
  console.error('Usage: node scripts/call-send-fax.js "+15551234567" /path/file.pdf|.txt');
  process.exit(1);
}
const resolved = path.resolve(filePath);
if (!fs.existsSync(resolved)) {
  console.error('File not found:', resolved);
  process.exit(1);
}
const client = createFaxClient({ baseUrl: process.env.FAX_API_URL || 'http://localhost:8080', apiKey: process.env.API_KEY || '' });
const sendFax = faxToolDefinitions(client, { localFiles: true }).find((tool) => tool.name === 'send_fax');
try {
  console.log(JSON.stringify(await sendFax.handler({ to, filePath: resolved }), null, 2));
} catch (err) {
  console.error('Failed:', err);
  process.exit(1);
}
