#!/usr/bin/env node
// Offline smoke check for the Node MCP package (no Faxbot server needed):
// syntax-checks every .js file under scripts/ and src/, then imports every module under src/.
import { readdirSync } from 'fs';
import path from 'path';
import { spawnSync } from 'child_process';
import { fileURLToPath, pathToFileURL } from 'url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function jsFiles(dir) {
  return readdirSync(path.join(root, dir), { recursive: true })
    .filter((name) => name.endsWith('.js'))
    .map((name) => path.join(root, dir, name))
    .sort();
}

const scripts = jsFiles('scripts');
const modules = jsFiles('src');
const failures = [];

for (const file of [...scripts, ...modules]) {
  const result = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' });
  if (result.status !== 0) failures.push(`syntax ${path.relative(root, file)}\n${result.stderr || result.error}`);
}

// Servers start only when run directly; keep an ephemeral port as a safety net.
process.env.MCP_WS_PORT = '0';

for (const file of modules) {
  try {
    await import(pathToFileURL(file).href);
  } catch (err) {
    failures.push(`import ${path.relative(root, file)}\n${(err && err.stack) || err}`);
  }
}

if (failures.length > 0) {
  console.error(`check-imports: ${failures.length} problem(s)\n\n${failures.join('\n\n')}`);
  process.exit(1);
}
console.log(`check-imports: ${scripts.length + modules.length} files parse, ${modules.length} modules import`);
// Imported servers keep handles open; exit explicitly.
process.exit(0);
