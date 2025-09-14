#!/usr/bin/env node
import { handleSendFaxTool } from '../src/tools/fax-tools.js';
import path from 'path';
import fs from 'fs';

async function main() {
  const to = process.argv[2];
  const filePath = process.argv[3];
  if (!to || !filePath) {
    console.error('Usage: node scripts/call-send-fax.js "+15551234567" /abs/path/file.pdf|.txt');
    process.exit(1);
  }
  const resolved = path.isAbsolute(filePath) ? filePath : path.resolve(process.cwd(), filePath);
  if (!fs.existsSync(resolved)) {
    console.error('File not found:', resolved);
    process.exit(1);
  }
  const result = await handleSendFaxTool({ to, filePath: resolved });
  console.log(JSON.stringify(result, null, 2));
}

main().catch((e) => { console.error('Failed:', e); process.exit(1); });

