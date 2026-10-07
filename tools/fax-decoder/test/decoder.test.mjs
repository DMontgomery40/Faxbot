// The browser decoder on fixtures the Python encoder wrote: node --test tools/fax-decoder/test
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { decodeFiles, rsCorrect, crc16, readTiff, readPage, DecodeError } from '../decoder.js';

const folder = join(dirname(fileURLToPath(import.meta.url)), 'fixtures');
const expected = JSON.parse(readFileSync(join(folder, 'expected.json'), 'utf8'));
const file = (name) => ({ bytes: new Uint8Array(readFileSync(join(folder, name))), type: '' });

test('Reed-Solomon repairs the shared vectors exactly as the Python encoder made them', () => {
  for (const vector of expected.rs) {
    assert.deepEqual(rsCorrect(vector.damaged, vector.parity, vector.erasures), vector.codeword);
  }
});

test('CRC-16 matches binascii.crc_hqx with 0xFFFF', () => {
  assert.equal(crc16(new TextEncoder().encode('123456789')), 0x29b1);
});

for (const name of expected.files.filter((item) => item !== 'encrypted.tiff')) {
  test(`decodes ${name} and checks its fingerprint`, async () => {
    const result = await decodeFiles([file(name)]);
    assert.equal(result.sha256, expected.sha256);
    assert.equal(result.name, expected.name);
    assert.equal(result.contentType, 'text/plain');
    assert.equal(result.pagesRead, result.pagesExpected);
  });
}

test('an encrypted payload needs the shared key and decodes with it', async () => {
  await assert.rejects(decodeFiles([file('encrypted.tiff')]), (error) => error instanceof DecodeError
    && error.message === 'The document is encrypted; enter the shared key.');
  await assert.rejects(decodeFiles([file('encrypted.tiff')], { secrets: ['not the key at all'] }),
    /does not match/);
  const result = await decodeFiles([file('encrypted.tiff')], { secrets: [expected.secret] });
  assert.equal(result.sha256, expected.sha256);
});

test('a page with lines blanked out still decodes', async () => {
  const [page] = readTiff(file('grid.tiff').bytes);
  const damaged = { ...page, gray: page.gray.slice() };
  const start = Math.floor(page.height * 0.6);
  damaged.gray.fill(255, start * page.width, (start + 6) * page.width);
  assert.ok(readPage(damaged));
});

test('a file without payload pages says so', async () => {
  const blank = new Uint8Array(readFileSync(join(folder, 'grid.tiff')));
  const [page] = readTiff(blank);
  page.gray.fill(255);
  assert.equal(readPage(page), null);
});

test('a forged header that claims a huge stream is refused before anything is allocated', async () => {
  const { decodeHeaderForTest } = await import('../decoder.js');
  const header = new Uint8Array(32);
  header.set([0x46, 0x58, 0x50, 1, 1, 32]);
  const view = new DataView(header.buffer);
  view.setUint16(8, 1); view.setUint32(14, 0x7fffffff); view.setUint32(18, 0x7fffffff); header[30] = 15;
  assert.equal(decodeHeaderForTest(header), null);
  view.setUint32(18, 2000); view.setUint32(14, 9); // the real size of a 2,000-byte container at parity 32
  assert.notEqual(decodeHeaderForTest(header), null);
});
