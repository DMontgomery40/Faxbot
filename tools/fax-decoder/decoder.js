// Faxbot payload decoder (formats 1 and 2, experimental). Runs entirely on this device: nothing is uploaded.
//
// The same format as api/app/codec (pages.py, runs.py, stream.py, container.py, rs.py, t4.py):
// find the ladder pattern, read the page header, read every scan line on its own (grid cells,
// picture cells or run-coded lines), repair the interleaved Reed-Solomon stream, unpack the
// container (deflate or zstd, optional AES-256-GCM with a shared key) and check the SHA-256.
// Works in browsers and in Node 18+ (tests): WebCrypto for SHA-256, PBKDF2 and AES-GCM;
// DecompressionStream or node:zlib for deflate; the vendored fzstd (MIT) for zstd.
import { decompress as zstdDecompress } from './vendor/fzstd.mjs';
import { CAPACITY_TABLES } from './capacity-tables.js';

export class DecodeError extends Error {}

// ---------------------------------------------------------------- GF(256) and Reed-Solomon
const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);
{
  let x = 1;
  for (let i = 0; i < 255; i += 1) {
    EXP[i] = x; LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (let i = 255; i < 512; i += 1) EXP[i] = EXP[i - 255];
}
const gmul = (a, b) => (a === 0 || b === 0 ? 0 : EXP[LOG[a] + LOG[b]]);
const gdiv = (a, b) => (a === 0 ? 0 : EXP[(LOG[a] + 255 - LOG[b]) % 255]);
const gpow = (a, n) => EXP[(((LOG[a] * n) % 255) + 255) % 255];
const ginv = (a) => EXP[255 - LOG[a]];
const polyScale = (p, x) => p.map((v) => gmul(v, x));
function polyAdd(p, q) {
  const size = Math.max(p.length, q.length);
  const r = new Array(size).fill(0);
  p.forEach((v, i) => { r[i + size - p.length] = v; });
  q.forEach((v, i) => { r[i + size - q.length] ^= v; });
  return r;
}
function polyMul(p, q) {
  const r = new Array(p.length + q.length - 1).fill(0);
  for (let j = 0; j < q.length; j += 1) for (let i = 0; i < p.length; i += 1) r[i + j] ^= gmul(p[i], q[j]);
  return r;
}
function polyEval(p, x) {
  let y = p[0];
  for (let i = 1; i < p.length; i += 1) y = gmul(y, x) ^ p[i];
  return y;
}
function polyDiv(dividend, divisor) {
  const out = dividend.slice();
  for (let i = 0; i < dividend.length - (divisor.length - 1); i += 1) {
    const c = out[i];
    if (c) for (let j = 1; j < divisor.length; j += 1) if (divisor[j]) out[i + j] ^= gmul(divisor[j], c);
  }
  const sep = out.length - (divisor.length - 1);
  return [out.slice(0, sep), out.slice(sep)];
}
const syndromes = (word, parity) => [0, ...Array.from({ length: parity }, (_, i) => polyEval(word, EXP[i]))];

export function rsCorrect(codeword, parity, erasures = []) {
  let word = codeword.slice();
  const erased = [...new Set(erasures)].sort((a, b) => a - b);
  if (erased.length > parity) throw new DecodeError('too many erasures');
  for (const position of erased) word[position] = 0;
  const synd = syndromes(word, parity);
  if (Math.max(...synd) === 0) return word;
  const n = word.length;
  const forney = synd.slice(1);
  for (const position of erased) {
    const x = gpow(2, n - 1 - position);
    for (let j = 0; j < forney.length - 1; j += 1) forney[j] = gmul(forney[j], x) ^ forney[j + 1];
  }
  let locator = [1];
  let previous = [1];
  for (let i = 0; i < parity - erased.length; i += 1) {
    let delta = forney[i];
    for (let j = 1; j < locator.length; j += 1) delta ^= gmul(locator[locator.length - 1 - j], forney[i - j]);
    previous = [...previous, 0];
    if (delta) {
      if (previous.length > locator.length) {
        const next = polyScale(previous, delta);
        previous = polyScale(locator, ginv(delta));
        locator = next;
      }
      locator = polyAdd(locator, polyScale(previous, delta));
    }
  }
  while (locator.length && locator[0] === 0) locator.shift();
  const errors = locator.length - 1;
  if (errors * 2 + erased.length > parity) throw new DecodeError('too many errors');
  const reversed = locator.slice().reverse();
  const found = [];
  if (errors > 0) {
    for (let i = 0; i < n; i += 1) if (polyEval(reversed, gpow(2, i)) === 0) found.push(n - 1 - i);
    if (found.length !== errors) throw new DecodeError('could not locate the errors');
  }
  const positions = [...erased, ...found];
  const coefficients = positions.map((p) => n - 1 - p);
  let errata = [1];
  for (const c of coefficients) errata = polyMul(errata, polyAdd([1], [gpow(2, c), 0]));
  const [, remainder] = polyDiv(polyMul(synd.slice().reverse(), errata), [1, ...new Array(errata.length).fill(0)]);
  const evaluator = remainder.slice().reverse();
  const roots = coefficients.map((c) => gpow(2, c));
  roots.forEach((root, i) => {
    const inverse = ginv(root);
    let derivative = 1;
    roots.forEach((other, j) => { if (j !== i) derivative = gmul(derivative, 1 ^ gmul(inverse, other)); });
    if (derivative === 0) throw new DecodeError('could not find an error magnitude');
    const y = gmul(root, polyEval(evaluator.slice().reverse(), inverse));
    word[positions[i]] ^= gdiv(y, derivative);
  });
  if (Math.max(...syndromes(word, parity)) !== 0) throw new DecodeError('could not correct the codeword');
  return word;
}

// ---------------------------------------------------------------- CRC-16/CCITT-FALSE
const CRC_TABLE = new Uint16Array(256);
for (let i = 0; i < 256; i += 1) {
  let c = i << 8;
  for (let k = 0; k < 8; k += 1) c = (c & 0x8000) ? ((c << 1) ^ 0x1021) & 0xffff : (c << 1) & 0xffff;
  CRC_TABLE[i] = c;
}
export function crc16(bytes, init = 0xffff) {
  let crc = init;
  for (const b of bytes) crc = ((crc << 8) & 0xffff) ^ CRC_TABLE[((crc >> 8) ^ b) & 0xff];
  return crc;
}

// ---------------------------------------------------------------- T.4 / T.6 code tables
const WHITE_TERM = '00110101 000111 0111 1000 1011 1100 1110 1111 10011 10100 00111 01000 001000 000011 110100 110101 101010 101011 0100111 0001100 0001000 0010111 0000011 0000100 0101000 0101011 0010011 0100100 0011000 00000010 00000011 00011010 00011011 00010010 00010011 00010100 00010101 00010110 00010111 00101000 00101001 00101010 00101011 00101100 00101101 00000100 00000101 00001010 00001011 01010010 01010011 01010100 01010101 00100100 00100101 01011000 01011001 01011010 01011011 01001010 01001011 00110010 00110011 00110100'.split(' ');
const WHITE_MAKEUP = '11011 10010 010111 0110111 00110110 00110111 01100100 01100101 01101000 01100111 011001100 011001101 011010010 011010011 011010100 011010101 011010110 011010111 011011000 011011001 011011010 011011011 010011000 010011001 010011010 011000 010011011'.split(' ');
const BLACK_TERM = '0000110111 010 11 10 011 0011 0010 00011 000101 000100 0000100 0000101 0000111 00000100 00000111 000011000 0000010111 0000011000 0000001000 00001100111 00001101000 00001101100 00000110111 00000101000 00000010111 00000011000 000011001010 000011001011 000011001100 000011001101 000001101000 000001101001 000001101010 000001101011 000011010010 000011010011 000011010100 000011010101 000011010110 000011010111 000001101100 000001101101 000011011010 000011011011 000001010100 000001010101 000001010110 000001010111 000001100100 000001100101 000001010010 000001010011 000000100100 000000110111 000000111000 000000100111 000000101000 000001011000 000001011001 000000101011 000000101100 000001011010 000001100110 000001100111'.split(' ');
const BLACK_MAKEUP = '0000001111 000011001000 000011001001 000001011011 000000110011 000000110100 000000110101 0000001101100 0000001101101 0000001001010 0000001001011 0000001001100 0000001001101 0000001110010 0000001110011 0000001110100 0000001110101 0000001110110 0000001110111 0000001010010 0000001010011 0000001010100 0000001010101 0000001011010 0000001011011 0000001100100 0000001100101'.split(' ');
const EXTENDED = '00000001000 00000001100 00000001101 000000010010 000000010011 000000010100 000000010101 000000010110 000000010111 000000011100 000000011101 000000011110 000000011111'.split(' ');

function codeTable(term, makeup) {
  const table = new Map();
  term.forEach((code, run) => table.set(code, run));
  makeup.forEach((code, i) => table.set(code, 64 * (i + 1)));
  EXTENDED.forEach((code, i) => table.set(code, 1792 + 64 * i));
  return table;
}
const TABLES = [codeTable(WHITE_TERM, WHITE_MAKEUP), codeTable(BLACK_TERM, BLACK_MAKEUP)];
const MODES = new Map([['0001', 'P'], ['001', 'H'], ['1', 0], ['011', 1], ['000011', 2], ['0000011', 3],
  ['010', -1], ['000010', -2], ['0000010', -3]]);

class BitReader {
  constructor(bytes, reverse = false) {
    this.bytes = bytes; this.pos = 0; this.length = bytes.length * 8; this.reverse = reverse;
  }

  bit(index) {
    if (index >= this.length) return 0;
    const byte = this.bytes[index >> 3];
    return this.reverse ? (byte >> (index & 7)) & 1 : (byte >> (7 - (index & 7))) & 1;
  }

  peek(n) {
    let s = '';
    for (let i = 0; i < n; i += 1) s += this.bit(this.pos + i);
    return s;
  }
}

function readCode(reader, table) {
  let code = '';
  for (let i = 0; i < 14; i += 1) {
    code += reader.bit(reader.pos + i);
    if (code.length >= 2 && table.has(code)) {
      reader.pos += code.length;
      return table.get(code);
    }
  }
  throw new DecodeError('invalid code');
}

function readRun(reader, colour) {
  let total = 0;
  for (;;) {
    const value = readCode(reader, TABLES[colour]);
    total += value;
    if (value < 64) return total;
  }
}

function decode1d(reader, width) {
  const changes = [];
  let x = 0;
  let colour = 0;
  while (x < width) {
    x += readRun(reader, colour);
    if (x > width) throw new DecodeError('line too long');
    if (x < width) changes.push(x);
    colour ^= 1;
  }
  return changes;
}

function nextChange(list, after) {
  let i = 0;
  while (i < list.length && list[i] <= after) i += 1;
  return i;
}

function decode2d(reader, reference, width) {
  const changes = [];
  let a0 = -1;
  let colour = 0;
  const ref = [...reference, width, width];
  while (a0 < width) {
    let mode;
    let code = '';
    for (let i = 0; i < 7 && mode === undefined; i += 1) {
      code += reader.bit(reader.pos + i);
      if (MODES.has(code)) mode = MODES.get(code);
    }
    if (mode === undefined) throw new DecodeError('invalid mode');
    reader.pos += code.length;
    let j = nextChange(ref, a0);
    while (j < reference.length && ((j % 2 === 0) !== (colour === 0))) j += 1;
    const b1 = j < ref.length ? ref[j] : width;
    const b2 = j + 1 < ref.length ? ref[j + 1] : width;
    if (mode === 'P') {
      a0 = b2;
    } else if (mode === 'H') {
      const first = readRun(reader, colour);
      const second = readRun(reader, colour ^ 1);
      const start = a0 < 0 ? 0 : a0;
      changes.push(start + first, start + first + second);
      a0 = start + first + second;
    } else {
      const a1 = b1 + mode;
      if (a1 < 0 || a1 > width) throw new DecodeError('vertical position outside the line');
      changes.push(a1);
      a0 = a1;
      colour ^= 1;
    }
  }
  const cleaned = [];
  for (const c of changes) {
    if (c >= width) break;
    if (cleaned.length && cleaned[cleaned.length - 1] === c) cleaned.pop(); else cleaned.push(c);
  }
  return cleaned;
}

function skipEol(reader) {
  // Fill bits (zeros) then 000000000001, when present.
  let zeros = 0;
  while (reader.bit(reader.pos + zeros) === 0 && reader.pos + zeros < reader.length) zeros += 1;
  if (zeros >= 11 && reader.bit(reader.pos + zeros) === 1) {
    reader.pos += zeros + 1;
    return true;
  }
  return false;
}

// Decode CCITT data to rows of changing elements. k < 0: Group 4; k = 0: Group 3 1-D; k > 0: Group 3 2-D.
export function decodeCcitt(bytes, width, rows, k, { byteAlign = false, reverse = false } = {}) {
  const reader = new BitReader(bytes, reverse);
  const lines = [];
  let reference = [];
  const limit = rows || 20000;
  while (lines.length < limit && reader.pos < reader.length) {
    try {
      if (k < 0) {
        if (reader.peek(24) === '000000000001000000000001') break;
        if (byteAlign) reader.pos = Math.ceil(reader.pos / 8) * 8;
        const line = decode2d(reader, reference, width);
        lines.push(line); reference = line;
      } else {
        // Fill bits before an EOL are zeros, so skipping zeros up to the EOL's one handles byte alignment too.
        skipEol(reader);
        if (reader.pos >= reader.length || reader.peek(12) === '000000000001') break;
        let twoD = false;
        if (k > 0) { twoD = reader.bit(reader.pos) === 0; reader.pos += 1; }
        const line = twoD ? decode2d(reader, reference, width) : decode1d(reader, width);
        lines.push(line); reference = line;
      }
    } catch (error) {
      if (k < 0) break;
      // A damaged line: copy the previous one and look for the next EOL.
      lines.push(reference);
      let found = false;
      while (reader.pos < reader.length && !found) {
        reader.pos += 1;
        if (reader.peek(12) === '000000000001') found = true;
      }
    }
  }
  return lines;
}

function linesToGray(lines, width, whiteIsZero = true) {
  const gray = new Uint8Array(width * lines.length);
  lines.forEach((changes, y) => {
    let colour = 0; // CCITT white first
    let x = 0;
    const row = y * width;
    for (const c of [...changes, width]) {
      const value = (colour === 0) === whiteIsZero ? 255 : 0;
      gray.fill(value, row + x, row + c);
      x = c; colour ^= 1;
    }
  });
  return gray;
}

// ---------------------------------------------------------------- images in: TIFF, PDF, browser images
function packedToGray(bytes, width, height, oneIsWhite) {
  const stride = Math.ceil(width / 8);
  const gray = new Uint8Array(width * height);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const bit = (bytes[y * stride + (x >> 3)] >> (7 - (x & 7))) & 1;
      gray[y * width + x] = (bit === 1) === oneIsWhite ? 255 : 0;
    }
  }
  return gray;
}

function packBits(data, expected) {
  const out = new Uint8Array(expected);
  let i = 0; let o = 0;
  while (i < data.length && o < expected) {
    const n = data[i] > 127 ? data[i] - 256 : data[i];
    i += 1;
    if (n >= 0) { out.set(data.subarray(i, i + n + 1), o); i += n + 1; o += n + 1; } else if (n !== -128) {
      out.fill(data[i], o, o + 1 - n); i += 1; o += 1 - n;
    }
  }
  return out;
}

export function readTiff(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const little = bytes[0] === 0x49;
  const u16 = (o) => view.getUint16(o, little);
  const u32 = (o) => view.getUint32(o, little);
  if (u16(2) !== 42) throw new DecodeError('This is not a TIFF file.');
  const pages = [];
  let ifd = u32(4);
  while (ifd && pages.length < 200) {
    const count = u16(ifd);
    const tags = new Map();
    for (let i = 0; i < count; i += 1) {
      const e = ifd + 2 + i * 12;
      const tag = u16(e); const type = u16(e + 2); const n = u32(e + 4);
      const size = { 1: 1, 3: 2, 4: 4, 5: 8 }[type] || 1;
      const at = n * size > 4 ? u32(e + 8) : e + 8;
      const values = [];
      for (let j = 0; j < Math.min(n, 100000); j += 1) {
        if (type === 3) values.push(u16(at + j * 2));
        else if (type === 4) values.push(u32(at + j * 4));
        else if (type === 5) values.push(u32(at + j * 8) / (u32(at + j * 8 + 4) || 1));
        else values.push(bytes[at + j]);
      }
      tags.set(tag, values);
    }
    const get = (t, d) => (tags.has(t) ? tags.get(t)[0] : d);
    const width = get(256); const height = get(257);
    const compression = get(259, 1); const photometric = get(262, 0); const fill = get(266, 1);
    const offsets = tags.get(273) || []; const counts = tags.get(279) || [];
    const rowsPerStrip = get(278, height);
    const options = compression === 3 ? get(292, 0) : get(293, 0);
    const dpi = [get(282, 204), get(283, 196)];
    const gray = new Uint8Array(width * height);
    let row = 0;
    offsets.forEach((offset, s) => {
      const data = bytes.subarray(offset, offset + counts[s]);
      const rows = Math.min(rowsPerStrip, height - row);
      let part;
      if (compression === 3 || compression === 4 || compression === 2) {
        const k = compression === 4 ? -1 : (options & 1 ? 1 : 0);
        const lines = decodeCcitt(data, width, rows, k, { byteAlign: Boolean(options & 4), reverse: fill === 2 });
        while (lines.length < rows) lines.push([]);
        part = linesToGray(lines, width, photometric === 0);
      } else if (compression === 1 || compression === 32773) {
        const raw = compression === 1 ? data : packBits(data, Math.ceil(width / 8) * rows);
        part = packedToGray(raw, width, rows, photometric === 1);
      } else {
        throw new DecodeError('This TIFF uses a compression the decoder does not read.');
      }
      gray.set(part.subarray(0, width * rows), row * width);
      row += rows;
    });
    pages.push({ width, height, gray, dpi });
    ifd = u32(ifd + 2 + count * 12);
  }
  return pages;
}

async function inflate(data) {
  if (typeof DecompressionStream !== 'undefined') {
    const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate'));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }
  const zlib = await import('node:zlib');
  return new Uint8Array(zlib.inflateSync(data));
}

const latin1 = (bytes) => { let s = ''; for (let i = 0; i < bytes.length; i += 8192) s += String.fromCharCode(...bytes.subarray(i, i + 8192)); return s; };

export async function readPdf(bytes) {
  const text = latin1(bytes);
  const objects = new Map();
  const objectPattern = /(\d+)\s+(\d+)\s+obj\b/g;
  let match;
  while ((match = objectPattern.exec(text))) objects.set(Number(match[1]), match.index + match[0].length);
  const number = (value) => {
    const ref = /^\s*(\d+)\s+\d+\s+R/.exec(value);
    if (!ref) return Number(value);
    const at = objects.get(Number(ref[1]));
    return Number(/^\s*(\d+)/.exec(text.slice(at, at + 40))[1]);
  };
  const pages = [];
  for (const start of objects.values()) {
    const head = text.slice(start, start + 2000);
    const dictEnd = head.indexOf('stream');
    const objectEnd = head.indexOf('endobj');
    if (dictEnd < 0 || (objectEnd >= 0 && objectEnd < dictEnd)) continue;
    const dict = head.slice(0, dictEnd);
    if (!/\/Subtype\s*\/Image/.test(dict)) continue;
    const field = (name) => { const m = new RegExp(`/${name}\\s*([^/>]+)`).exec(dict); return m ? m[1].trim() : null; };
    const width = number(field('Width'));
    const height = number(field('Height'));
    const length = number(field('Length'));
    let dataStart = start + dictEnd + 6;
    if (text[dataStart] === '\r') dataStart += 1;
    if (text[dataStart] === '\n') dataStart += 1;
    let data = bytes.subarray(dataStart, dataStart + length);
    const filters = ((/\/Filter\s*(\[[^\]]*\]|\/\w+)/.exec(dict) || [, ''])[1].match(/\/\w+/g)) || [];
    const invert = /\/Decode\s*\[\s*1\s+0\s*\]/.test(dict);
    let gray = null;
    for (const filter of filters) {
      if (filter === '/FlateDecode') {
        data = await inflate(data);
      } else if (filter === '/CCITTFaxDecode') {
        const parms = (/\/DecodeParms\s*\[?\s*<<([\s\S]*?)>>/.exec(dict) || [, ''])[1];
        const param = (name, d) => { const m = new RegExp(`/${name}\\s*(-?\\d+|true|false)`).exec(parms); return m ? m[1] : d; };
        const k = Number(param('K', '0'));
        const columns = Number(param('Columns', String(width)));
        const rows = Number(param('Rows', String(height)));
        const lines = decodeCcitt(data, columns, rows, k, { byteAlign: param('EncodedByteAlign', 'false') === 'true' });
        while (lines.length < height) lines.push([]);
        // CCITT white runs are white on the page; /Decode [1 0] inverts them.
        gray = linesToGray(lines.slice(0, height), columns, !invert);
      } else if (filter === '/DCTDecode') {
        gray = (await imageToGray(new Blob([data], { type: 'image/jpeg' }))).gray;
      } else {
        throw new DecodeError(`This PDF uses ${filter.slice(1)}, which the decoder does not read.`);
      }
    }
    if (!gray) {
      const bits = number(field('BitsPerComponent') || '8');
      gray = bits === 1 ? packedToGray(data, width, height, !invert) : Uint8Array.from(data.subarray(0, width * height));
    }
    pages.push({ width, height, gray });
  }
  if (!pages.length) throw new DecodeError('This PDF has no fax images.');
  return pages;
}

async function imageToGray(blob) {
  if (typeof createImageBitmap === 'undefined') throw new DecodeError('Open picture files in a browser.');
  const bitmap = await createImageBitmap(blob);
  const canvas = typeof OffscreenCanvas !== 'undefined' ? new OffscreenCanvas(bitmap.width, bitmap.height)
    : Object.assign(document.createElement('canvas'), { width: bitmap.width, height: bitmap.height });
  const context = canvas.getContext('2d');
  context.drawImage(bitmap, 0, 0);
  const rgba = context.getImageData(0, 0, bitmap.width, bitmap.height).data;
  const gray = new Uint8Array(bitmap.width * bitmap.height);
  for (let i = 0; i < gray.length; i += 1) gray[i] = (rgba[i * 4] * 299 + rgba[i * 4 + 1] * 587 + rgba[i * 4 + 2] * 114) / 1000;
  return { width: bitmap.width, height: bitmap.height, gray };
}

export async function readFile(bytes, type = '') {
  if (bytes[0] === 0x25 && bytes[1] === 0x50 && bytes[2] === 0x44 && bytes[3] === 0x46) return readPdf(bytes);
  if ((bytes[0] === 0x49 && bytes[1] === 0x49) || (bytes[0] === 0x4d && bytes[1] === 0x4d)) return readTiff(bytes);
  return [await imageToGray(new Blob([bytes], { type }))];
}

// ---------------------------------------------------------------- payload pages
const GROUP_BYTES = 32;
const HEADER_OFFSETS = [0xfffffff0, 0xfffffff1, 0xfffffff2];
const LAYOUTS = { 1: 'grid', 2: 'runs', 3: 'picture', 5: 'capacity' };
export const NEWER = 'These encoded pages were made by a newer version of Faxbot; update this decoder to read them.';

function runsOf(gray, width, y) {
  const runs = [];
  let start = 0;
  const row = y * width;
  let black = gray[row] < 128;
  for (let x = 1; x <= width; x += 1) {
    const b = x < width ? gray[row + x] < 128 : !black;
    if (b !== black) { runs.push([start, x - start, black]); start = x; black = b; }
  }
  return runs;
}

export function findLadder(page) {
  for (let y = 0; y < page.height; y += 1) {
    const runs = runsOf(page.gray, page.width, y);
    if (runs.length < 40) continue;
    for (let i = 0; i < runs.length; i += 1) {
      const [, length, black] = runs[i];
      if (!black || length < 3) continue;
      const unit = length / 3;
      const tolerance = Math.max(1, unit * 0.5);
      let end = i + 1;
      while (end < runs.length && Math.abs(runs[end][1] - unit) <= tolerance) end += 1;
      const cells = end - i - 3;
      if (cells >= 31 && cells % 2 === 1 && end < runs.length && runs[end][2]
          && Math.abs(runs[end][1] - 3 * unit) <= 3 * tolerance && !runs[i + 1][2]) {
        const edges = [];
        for (let e = i + 2; e < end; e += 1) edges.push(runs[e][0]);
        return { y, edges, unit };
      }
      i = Math.max(i, end - 1);
    }
  }
  return null;
}

export function groupSizes(columns) {
  const available = columns - 32;
  const count = Math.max(1, Math.round(available / (8 * GROUP_BYTES + 16)));
  const total = Math.floor((available - 16 * count) / 8);
  return Array.from({ length: count }, (_, i) => Math.floor(total / count) + (i < total % count ? 1 : 0));
}

function bitsToBytes(bits, start, count) {
  const out = new Uint8Array(count);
  for (let i = 0; i < count; i += 1) {
    let v = 0;
    for (let b = 0; b < 8; b += 1) v = (v << 1) | (bits.charCodeAt(start + i * 8 + b) === 49 ? 1 : 0);
    out[i] = v;
  }
  return out;
}

function u32bytes(value) {
  return [(value >>> 24) & 255, (value >>> 16) & 255, (value >>> 8) & 255, value & 255];
}

function parseRow(bits, sizes, tag) {
  const offset = parseInt(bits.slice(0, 32), 2) >>> 0;
  let position = 32;
  let start = 0;
  const good = [];
  sizes.forEach((size, index) => {
    const chunk = bitsToBytes(bits, position, size);
    const crc = parseInt(bits.slice(position + 8 * size, position + 8 * size + 16), 2);
    const input = new Uint8Array([...tag, ...u32bytes(offset), index, ...chunk]);
    if (crc16(input) === crc) good.push({ index, start, chunk });
    position += 8 * size + 16;
    start += size;
  });
  return { offset, good };
}

function gridBits(page, y, centres) {
  let s = '';
  const row = y * page.width;
  for (const x of centres) s += page.gray[row + x] < 128 ? '1' : '0';
  return s;
}

function pictureBits(page, y, edges) {
  let s = '';
  const row = y * page.width;
  for (let i = 0; i + 1 < edges.length; i += 1) {
    const middle = (edges[i] + edges[i + 1]) >> 1;
    let first = 0; let second = 0;
    for (let x = edges[i]; x < middle; x += 1) if (page.gray[row + x] < 128) first += 1;
    for (let x = middle; x < edges[i + 1]; x += 1) if (page.gray[row + x] < 128) second += 1;
    s += second > first ? '1' : '0';
  }
  return s;
}

function decodeHeader(bytes) {
  const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const fxp = bytes[0] === 0x46 && bytes[1] === 0x58 && bytes[2] === 0x50;
  // A header whose CRCs passed but whose format, layout or profile this decoder does not know: a newer Faxbot's.
  if (fxp && (bytes[3] > 1 || (bytes[3] === 1 && !LAYOUTS[bytes[4]])
      || (bytes[3] === 1 && bytes[4] === 5 && !CAPACITY_TABLES[String(bytes[31])]))) return NEWER;
  if (!fxp || bytes[3] !== 1 || !LAYOUTS[bytes[4]]) return null;
  if (bytes[4] === 5 && bytes[30] !== 7) return null;
  // The header is untrusted: its sizes must be the ones a real container of that length has.
  const parity = bytes[5]; const length = v.getUint32(18);
  if (parity < 1 || parity > 128 || length < 1 || length > 32 * 1024 * 1024 + 4096
      || v.getUint32(14) !== Math.max(1, Math.ceil(length / (255 - parity))) || bytes[30] < 1 || bytes[30] > 63) return null;
  return {
    layout: LAYOUTS[bytes[4]], parity: bytes[5], page: v.getUint16(6), pages: v.getUint16(8),
    tag: Array.from(bytes.subarray(10, 14)), codewords: v.getUint32(14), containerLength: v.getUint32(18),
    runLimit: bytes[30], profile: bytes[31],
  };
}

// Capacity lines (format 2, api/app/codec/capacity.py): the data part's runs are re-encoded with the arithmetic
// encoder over the frozen frequency tables; the bits it settles are the offset (masked by the tag) and the payload
// (XORed with an xorshift32 keystream keyed by the tag and the offset). Plain Number arithmetic only: every value
// stays below 2**53 (P = 31 bits, totals of 2**20), so no bitwise operator touches the coder's state.
const PRECISION = 31;
const FULL = 2 ** PRECISION - 1;
const HALF = 2 ** (PRECISION - 1);
const QUARTER = 2 ** (PRECISION - 2);
const RESERVE = 16 * 7 + 1;
const cumulativeCache = new Map();
function cumulative(profile) {
  if (cumulativeCache.has(profile)) return cumulativeCache.get(profile);
  const table = CAPACITY_TABLES[String(profile)];
  const both = ['white', 'black'].map((key) => {
    const cum = [0];
    for (const frequency of table[key]) cum.push(cum[cum.length - 1] + frequency);
    return cum;
  });
  cumulativeCache.set(profile, both);
  return both;
}

function xorshift(x) {
  let y = (x ^ (x << 13)) >>> 0;
  y = (y ^ (y >>> 17)) >>> 0;
  return (y ^ (y << 5)) >>> 0;
}
const tagValue = (tag) => ((tag[0] << 24) | (tag[1] << 16) | (tag[2] << 8) | tag[3]) >>> 0;
const offsetMask = (tag) => xorshift(((tagValue(tag) ^ 0xa5a5a5a5) >>> 0) || 0x6d2b79f5);
function keystream(tag, offset, count) {
  let state = ((tagValue(tag) ^ (Math.imul(offset, 0x9e3779b1) >>> 0)) >>> 0) || 0x6d2b79f5;
  let out = '';
  while (out.length < count) {
    state = xorshift(state);
    out += state.toString(2).padStart(32, '0');
  }
  return out.slice(0, count);
}

function settledBits(runs, width, cum) {
  let low = 0; let high = FULL; let pending = 0; let room = width; let colour = 0;
  const out = [];
  for (const run of runs) {
    const table = cum[colour];
    const limit = Math.min(table.length - 1, room - RESERVE);
    if (run < 1 || run > limit || table[run] === table[run - 1]) return null;
    const total = table[limit];
    const span = high - low + 1;
    high = low + Math.floor((span * table[run]) / total) - 1;
    low += Math.floor((span * table[run - 1]) / total);
    for (;;) {
      if (high < HALF) {
        out.push('0' + '1'.repeat(pending)); pending = 0;
      } else if (low >= HALF) {
        out.push('1' + '0'.repeat(pending)); pending = 0; low -= HALF; high -= HALF;
      } else if (low >= QUARTER && high < HALF + QUARTER) {
        pending += 1; low -= QUARTER; high -= QUARTER;
      } else break;
      low *= 2; high = high * 2 + 1;
    }
    room -= run; colour ^= 1;
  }
  return room === RESERVE ? out.join('') : null;
}

function decodeCapacityLine(page, y, profile, tag) {
  const runs = runsOf(page.gray, page.width, y).map(([, length]) => length);
  if (page.gray[y * page.width] < 128 || runs.length < 8) return null;
  const width = page.width;
  let room = width; let index = 0;
  while (room > RESERVE) {
    if (index >= runs.length) return null;
    room -= runs[index]; index += 1;
  }
  if (room !== RESERVE) return null;
  const settled = settledBits(runs.slice(0, index), width, cumulative(profile));
  if (settled === null || settled.length < 32) return null;
  let colour = index % 2;
  const crcPaths = [paths(0, 7), paths(1, 7)];
  let crcBits = '';
  while (crcBits.length < 16) {
    if (index >= runs.length) return null;
    const path = crcPaths[colour].get(runs[index]);
    if (path === undefined) return null;
    crcBits += path; room -= runs[index]; index += 1; colour ^= 1;
  }
  if (index !== runs.length - 1 || runs[index] !== room || room < 1 || /1/.test(crcBits.slice(16))) return null;
  const offset = (parseInt(settled.slice(0, 32), 2) ^ offsetMask(tag)) >>> 0;
  const scrambled = settled.slice(32);
  const key = keystream(tag, offset, scrambled.length);
  let payload = '';
  for (let i = 0; i < scrambled.length; i += 1) payload += scrambled[i] === key[i] ? '0' : '1';
  const padded = payload + '0'.repeat((8 - (payload.length % 8)) % 8);
  const packed = bitsToBytes(padded, 0, padded.length / 8);
  const input = new Uint8Array([...tag, ...u32bytes(offset), (payload.length >> 8) & 255, payload.length & 255, ...packed]);
  if (crc16(input) !== parseInt(crcBits.slice(0, 16), 2)) return null;
  return { offset, payload };
}

// Run-coded lines: the MH code of each run, read back as the payload bits its tree path carried.
const pathCache = new Map();
function paths(colour, limit) {
  const key = `${colour}:${limit}`;
  if (pathCache.has(key)) return pathCache.get(key);
  const term = colour === 0 ? WHITE_TERM : BLACK_TERM;
  const root = {};
  for (let run = 1; run <= limit; run += 1) {
    let node = root;
    for (const bit of term[run]) { node[bit] = node[bit] || {}; node = node[bit]; }
  }
  const result = new Map();
  for (let run = 1; run <= limit; run += 1) {
    let node = root; let carried = '';
    for (const bit of term[run]) { if (node['0'] && node['1']) carried += bit; node = node[bit]; }
    result.set(run, carried);
  }
  pathCache.set(key, result);
  return result;
}

function decodeRunLine(page, y, limit, tag) {
  const runs = runsOf(page.gray, page.width, y).map(([, length]) => length);
  if (page.gray[y * page.width] < 128 || runs.length < 3) return null;
  const reserve = 16 * 7 + limit + 1;
  let room = page.width; let colour = 0; let index = 0; let bits = '';
  const data = [paths(0, limit), paths(1, limit)];
  while (room > reserve) {
    const path = data[colour].get(runs[index]);
    if (path === undefined) return null;
    bits += path; room -= runs[index]; index += 1; colour ^= 1;
    if (index >= runs.length) return null;
  }
  if (bits.length < 32) return null;
  const crcPaths = [paths(0, 7), paths(1, 7)];
  let crcBits = '';
  while (crcBits.length < 16) {
    if (index >= runs.length) return null;
    const path = crcPaths[colour].get(runs[index]);
    if (path === undefined) return null;
    crcBits += path; room -= runs[index]; index += 1; colour ^= 1;
  }
  if (index !== runs.length - 1 || runs[index] !== room || /1/.test(crcBits.slice(16))) return null;
  const offset = parseInt(bits.slice(0, 32), 2) >>> 0;
  const payload = bits.slice(32);
  const padded = payload + '0'.repeat((8 - (payload.length % 8)) % 8);
  const packed = bitsToBytes(padded, 0, padded.length / 8);
  const input = new Uint8Array([...tag, ...u32bytes(offset), (payload.length >> 8) & 255, payload.length & 255, ...packed]);
  if (crc16(input) !== parseInt(crcBits.slice(0, 16), 2)) return null;
  return { offset, payload };
}

// Received pages as real receivers keep them (api/app/codec/pages.py read_page): a page that arrived inverted is
// turned the right way round (every payload page has white margins at both sides), one upside down the right way up
// (the ladder reads either way, the header only the right way up), and the rows of a run-coded or capacity page that
// was moved sideways or padded to another width are moved back to where the ladder says they started. Those layouts
// need every dot as it arrived, so a page drawn again at another size is refused (RESIZED), and so is a small preview
// instead of the fax (PREVIEW).
export const RESIZED = 'This page was resized after it arrived, and these encoded pages can be read only from the '
  + 'fax image exactly as received, such as the PDF a fax service offers for download.';
export const PREVIEW = 'This is a small preview of the fax rather than the fax itself, so decode the full-size fax '
  + 'file, such as the PDF a fax service offers for download.';
const PREVIEW_WIDTH = 1000;
const EXACT_LAYOUTS = new Set(['runs', 'capacity']);
// {ladder columns: [page width, first cell's dot on an unmoved page]} for the exact layouts (2-dot cells).
const EXACT_WIDTHS = new Map([[1728, 204], [2592, 300], [3456, 400]].map(([width, xdpi]) => {
  const quiet = Math.max(1, Math.round((3 * xdpi) / 25.4));
  let columns = Math.floor((width - 2 * quiet) / 2) - 8;
  if (columns % 2 === 0) columns -= 1;
  return [columns, [width, quiet + 8]];
}));

function paperWhite(page) {
  const edge = Math.max(1, Math.min(Math.floor(page.width / 50), 16));
  let dark = 0;
  for (let y = 0; y < page.height; y += 1) {
    const row = y * page.width;
    for (let x = 0; x < edge; x += 1) {
      if (page.gray[row + x] < 128) dark += 1;
      if (page.gray[row + page.width - 1 - x] < 128) dark += 1;
    }
  }
  return dark * 2 > 2 * edge * page.height ? { ...page, gray: page.gray.map((value) => 255 - value) } : page;
}

function alignedRow(page, y, shift, width) {
  const start = y * page.width;
  const out = new Uint8Array(width);
  for (let x = 0; x < width; x += 1) {
    const from = x + shift;
    if (from < 0) out[x] = 255; // the receiver cut the left edge: white, as a row starts
    else if (from < page.width) out[x] = page.gray[start + from];
    else out[x] = x > 0 ? out[x - 1] : 255; // past the right edge: the row's padding run goes on
  }
  return { width, height: 1, gray: out };
}

export function readPage(source) {
  const page = paperWhite(source);
  const read = readOriented(page);
  if (!read || !read.noHeader) return read;
  const turned = readOriented({ ...page, gray: page.gray.slice().reverse() });
  return turned && !turned.noHeader ? turned : null;
}

function readOriented(page) {
  const ladder = findLadder(page);
  if (!ladder) return null;
  const { edges } = ladder;
  const columns = edges.length - 1;
  const centres = Array.from({ length: columns }, (_, i) => (edges[i] + edges[i + 1]) >> 1);
  const sizes = groupSizes(columns);
  let header = null; let newer = false;
  for (let y = 0; y < page.height && !header; y += 1) {
    const bits = gridBits(page, y, centres);
    if (!bits.includes('1')) continue;
    const { offset, good } = parseRow(bits, sizes, [0, 0, 0, 0]);
    if (HEADER_OFFSETS.includes(offset) && good.length === sizes.length) {
      header = decodeHeader(new Uint8Array(good.flatMap((g) => [...g.chunk])));
      if (header === NEWER) { newer = true; header = null; }
    }
  }
  if (!header && newer) return { newer: true };
  if (!header) return { noHeader: true };
  const gaps = edges.slice(1).map((edge, i) => edge - edges[i]);
  const resized = new Set(gaps).size !== 1;
  let rowWidth = page.width; let shift = 0;
  if (EXACT_LAYOUTS.has(header.layout)) {
    const exact = EXACT_WIDTHS.get(columns);
    if (!exact || gaps.some((gap) => gap !== 2)) return { resized: true };
    [rowWidth] = exact;
    shift = edges[0] - exact[1];
  }
  const segments = new Map();
  for (let y = ladder.y; y < page.height; y += 1) {
    if (EXACT_LAYOUTS.has(header.layout)) {
      const row = alignedRow(page, y, shift, rowWidth);
      const line = header.layout === 'runs' ? decodeRunLine(row, 0, header.runLimit, header.tag)
        : decodeCapacityLine(row, 0, header.profile, header.tag);
      if (line && line.payload.length) segments.set(`${line.offset}:${line.payload.length}`, { offset: line.offset, bits: line.payload });
      continue;
    }
    const bits = header.layout === 'grid' ? gridBits(page, y, centres) : pictureBits(page, y, edges);
    if (!bits.includes('1')) continue;
    const { offset, good } = parseRow(bits, sizes, header.tag);
    for (const g of good) {
      const bitOffset = offset + 8 * g.start;
      let s = '';
      for (const b of g.chunk) s += b.toString(2).padStart(8, '0');
      segments.set(`${bitOffset}:${s.length}`, { offset: bitOffset, bits: s });
    }
  }
  return { header, segments: [...segments.values()], resized };
}

export function assemble(reads) {
  const tags = new Map();
  for (const read of reads) { const key = read.header.tag.join(','); tags.set(key, (tags.get(key) || 0) + 1); }
  const tag = [...tags.entries()].sort((a, b) => b[1] - a[1])[0][0];
  const chosen = reads.filter((read) => read.header.tag.join(',') === tag);
  const { codewords: n, parity, containerLength } = chosen[0].header;
  const total = n * 255;
  const stream = new Uint8Array(total);
  const covered = new Uint8Array(total);
  for (const read of chosen) {
    for (const { offset, bits } of read.segments) {
      for (let i = 0; i < bits.length; i += 1) {
        const position = offset + i;
        if (position >= total * 8) break;
        const byte = position >> 3; const mask = 0x80 >> (position & 7);
        if (bits.charCodeAt(i) === 49) stream[byte] |= mask; else stream[byte] &= ~mask;
        covered[byte] |= mask;
      }
    }
  }
  const k = 255 - parity;
  const container = new Uint8Array(n * k);
  let failed = 0;
  for (let i = 0; i < n; i += 1) {
    const word = new Array(255); const erasures = [];
    for (let j = 0; j < 255; j += 1) {
      const t = j * n + i;
      word[j] = stream[t];
      if (covered[t] !== 0xff) erasures.push(j);
    }
    let fixed = word;
    try { fixed = rsCorrect(word, parity, erasures); } catch (error) { failed += 1; }
    for (let j = 0; j < k; j += 1) container[i * k + j] = fixed[j];
  }
  const pages = new Set(chosen.map((read) => read.header.page));
  if (failed) {
    const missing = chosen[0].header.pages - pages.size;
    throw new DecodeError(missing
      ? `${missing} of the ${chosen[0].header.pages} payload pages are missing and the rest could not make up for them.`
      : `The payload pages are too damaged to decode (${failed} of ${n} codewords).`);
  }
  return { container: container.subarray(0, containerLength), pagesRead: pages.size, pagesExpected: chosen[0].header.pages };
}

// ---------------------------------------------------------------- the container
const subtle = () => globalThis.crypto.subtle;
const CONTENT = { 1: 'application/pdf', 2: 'text/plain' };

export async function unpack(container, secrets = []) {
  const v = new DataView(container.buffer, container.byteOffset, container.byteLength);
  if (container.length < 12 || String.fromCharCode(...container.subarray(0, 4)) !== 'FXBC' || container[4] !== 1) {
    throw new DecodeError('These pages do not carry a Faxbot document.');
  }
  const flags = container[5]; const method = container[6]; const length = v.getUint32(8);
  if (length > 32 * 1024 * 1024 + 4096) throw new DecodeError('The encoded document is larger than Faxbot accepts.');
  let inner;
  if (flags & 1) {
    const header = container.subarray(0, 40);
    const salt = container.subarray(12, 28); const nonce = container.subarray(28, 40);
    const body = container.subarray(40, 40 + length);
    for (const secret of secrets) {
      try {
        const material = await subtle().importKey('raw', new TextEncoder().encode(secret.trim()), 'PBKDF2', false, ['deriveKey']);
        const key = await subtle().deriveKey({ name: 'PBKDF2', salt, iterations: 200000, hash: 'SHA-256' }, material,
          { name: 'AES-GCM', length: 256 }, false, ['decrypt']);
        inner = new Uint8Array(await subtle().decrypt({ name: 'AES-GCM', iv: nonce, additionalData: header }, key, body));
        break;
      } catch (error) { /* try the next key */ }
    }
    if (!inner) {
      throw new DecodeError(secrets.length ? 'The document is encrypted with a key that does not match.'
        : 'The document is encrypted; enter the shared key.');
    }
  } else {
    inner = container.subarray(12, 12 + length);
  }
  const digest = inner.subarray(0, 32);
  const contentType = CONTENT[inner[32]];
  const iv = new DataView(inner.buffer, inner.byteOffset, inner.byteLength);
  const originalLength = iv.getUint32(33);
  const nameLength = inner[37];
  if (!contentType || originalLength > 32 * 1024 * 1024) throw new DecodeError('The encoded document is not a PDF or a text file.');
  const name = new TextDecoder().decode(inner.subarray(38, 38 + nameLength));
  const packed = inner.subarray(38 + nameLength);
  let data;
  if (method === 0) data = packed;
  else if (method === 1) data = await inflate(packed);
  else if (method === 2) data = zstdDecompress(packed);
  else throw new DecodeError('This payload uses a compression the decoder does not know.');
  if (data.length !== originalLength) throw new DecodeError('The encoded document is not the length its header states.');
  const actual = new Uint8Array(await subtle().digest('SHA-256', data));
  if (actual.some((b, i) => b !== digest[i])) throw new DecodeError('The decoded document does not match its fingerprint.');
  const sha256 = Array.from(actual, (b) => b.toString(16).padStart(2, '0')).join('');
  return { data, contentType, name, sha256 };
}

// ---------------------------------------------------------------- everything together
export async function decodeFiles(files, { secrets = [] } = {}) {
  const reads = [];
  let newer = false; let resized = false;
  const widths = [];
  for (const { bytes, type } of files) {
    for (const page of await readFile(bytes, type)) {
      widths.push(page.width);
      const read = readPage(page);
      if (read && read.newer) newer = true;
      else if (read && read.resized && !read.header) resized = true;
      else if (read) { reads.push(read); resized = resized || read.resized; }
    }
  }
  const small = widths.length > 0 && widths.every((width) => width < PREVIEW_WIDTH);
  if (!reads.length && newer) throw new DecodeError(NEWER);
  if (!reads.length && small) throw new DecodeError(PREVIEW);
  if (!reads.length && resized) throw new DecodeError(RESIZED);
  if (!reads.length) throw new DecodeError('No payload pages were found in this file.');
  let assembled;
  try {
    assembled = assemble(reads);
  } catch (error) {
    if (small) throw new DecodeError(PREVIEW);
    if (resized) throw new DecodeError(RESIZED);
    throw error;
  }
  const { container, pagesRead, pagesExpected } = assembled;
  const document = await unpack(container, secrets.filter(Boolean));
  return { ...document, pagesRead, pagesExpected, layout: reads[0].header.layout };
}

export const decodeHeaderForTest = decodeHeader;
