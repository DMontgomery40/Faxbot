const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {reachableFiles} = require('./console_reachable.cjs');

test('only used value imports preserve a screen, including through a helper', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'faxbot-ledger-'));
  try {
    for (const name of ['Home', 'Unused', 'Orphan', 'Types']) {
      fs.writeFileSync(path.join(root, name + '.tsx'), `export default function ${name}() { return null; }`);
    }
    fs.writeFileSync(path.join(root, 'helper.ts'), "import Home from './Home'; export const home = () => <never>Home;");
    const entry = path.join(root, 'main.tsx');
    fs.writeFileSync(entry, `import {home} from './helper';
      import Unused from './Unused'; import type Types from './Types';
      const typed: Types | null = null; home();`);
    const names = () => reachableFiles(root).map(file => path.basename(file));
    assert.deepEqual(names(), ['Home.tsx', 'helper.ts', 'main.tsx']);
    // Leaving the import behind when the control is removed cannot pass coverage.
    fs.writeFileSync(entry, "import {home} from './helper';");
    assert.deepEqual(names(), ['main.tsx']);
  } finally { fs.rmSync(root, {recursive: true}); }
});
