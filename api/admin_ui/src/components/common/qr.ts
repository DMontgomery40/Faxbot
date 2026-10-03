// Minimal QR Code encoder for short numeric payloads (pairing codes):
// version 1 (21x21), error correction level M, numeric mode, up to 34 digits.
// Follows ISO/IEC 18004; structure mirrors Project Nayuki's reference encoder.

const SIZE = 21;
const DATA_CODEWORDS = 16;
const EC_CODEWORDS = 10;
const MAX_DIGITS = 34;

function gfMultiply(x: number, y: number): number {
  let z = 0;
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((y >>> i) & 1) * x;
  }
  return z;
}

function reedSolomonDivisor(degree: number): number[] {
  const result = new Array<number>(degree).fill(0);
  result[degree - 1] = 1;
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < result.length; j++) {
      result[j] = gfMultiply(result[j], root);
      if (j + 1 < result.length) result[j] ^= result[j + 1];
    }
    root = gfMultiply(root, 0x02);
  }
  return result;
}

function reedSolomonRemainder(data: number[], divisor: number[]): number[] {
  const result = divisor.map(() => 0);
  for (const byte of data) {
    const factor = byte ^ (result.shift() as number);
    result.push(0);
    divisor.forEach((coefficient, i) => { result[i] ^= gfMultiply(coefficient, factor); });
  }
  return result;
}

function dataCodewords(digits: string): number[] {
  const bits: number[] = [];
  const append = (value: number, length: number) => {
    for (let i = length - 1; i >= 0; i--) bits.push((value >>> i) & 1);
  };
  append(0b0001, 4); // numeric mode
  append(digits.length, 10);
  for (let i = 0; i < digits.length; i += 3) {
    const group = digits.slice(i, i + 3);
    append(Number(group), group.length * 3 + 1);
  }
  const capacity = DATA_CODEWORDS * 8;
  append(0, Math.min(4, capacity - bits.length));
  append(0, (8 - (bits.length % 8)) % 8);
  const bytes: number[] = [];
  for (let i = 0; i < bits.length; i += 8) bytes.push(bits.slice(i, i + 8).reduce((acc, bit) => (acc << 1) | bit, 0));
  for (let pad = 0xec; bytes.length < DATA_CODEWORDS; pad ^= 0xec ^ 0x11) bytes.push(pad);
  return bytes;
}

const MASKS: Array<(x: number, y: number) => boolean> = [
  (x, y) => (x + y) % 2 === 0,
  (_x, y) => y % 2 === 0,
  (x) => x % 3 === 0,
  (x, y) => (x + y) % 3 === 0,
  (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0,
  (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
  (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0,
  (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0,
];

// Simplified penalty (runs, 2x2 blocks, dark balance) to pick a mask.
function penalty(modules: boolean[][]): number {
  let score = 0;
  for (let a = 0; a < SIZE; a++) {
    let rowRun = 1;
    let colRun = 1;
    for (let b = 1; b < SIZE; b++) {
      rowRun = modules[a][b] === modules[a][b - 1] ? rowRun + 1 : 1;
      colRun = modules[b][a] === modules[b - 1][a] ? colRun + 1 : 1;
      if (rowRun === 5) score += 3; else if (rowRun > 5) score += 1;
      if (colRun === 5) score += 3; else if (colRun > 5) score += 1;
    }
  }
  for (let y = 0; y < SIZE - 1; y++) {
    for (let x = 0; x < SIZE - 1; x++) {
      const c = modules[y][x];
      if (c === modules[y][x + 1] && c === modules[y + 1][x] && c === modules[y + 1][x + 1]) score += 3;
    }
  }
  const dark = modules.flat().filter(Boolean).length;
  score += Math.floor(Math.abs(dark * 20 - SIZE * SIZE * 10) / (SIZE * SIZE)) * 10;
  return score;
}

// Returns the module matrix (true = dark), indexed [row][column], or null
// when the payload is not 1-34 digits.
export function qrMatrix(digits: string): boolean[][] | null {
  if (!/^\d{1,34}$/.test(digits) || digits.length > MAX_DIGITS) return null;
  const modules = Array.from({ length: SIZE }, () => new Array<boolean>(SIZE).fill(false));
  const reserved = Array.from({ length: SIZE }, () => new Array<boolean>(SIZE).fill(false));
  const setFunction = (x: number, y: number, dark: boolean) => {
    modules[y][x] = dark;
    reserved[y][x] = true;
  };

  for (let i = 0; i < SIZE; i++) {
    setFunction(6, i, i % 2 === 0);
    setFunction(i, 6, i % 2 === 0);
  }
  for (const [cx, cy] of [[3, 3], [SIZE - 4, 3], [3, SIZE - 4]]) {
    for (let dy = -4; dy <= 4; dy++) {
      for (let dx = -4; dx <= 4; dx++) {
        const x = cx + dx;
        const y = cy + dy;
        if (x < 0 || x >= SIZE || y < 0 || y >= SIZE) continue;
        const distance = Math.max(Math.abs(dx), Math.abs(dy));
        setFunction(x, y, distance !== 2 && distance !== 4);
      }
    }
  }

  const drawFormat = (mask: number) => {
    const data = (0b00 << 3) | mask; // level M
    let remainder = data;
    for (let i = 0; i < 10; i++) remainder = (remainder << 1) ^ ((remainder >>> 9) * 0x537);
    const bits = ((data << 10) | remainder) ^ 0x5412;
    const bit = (i: number) => ((bits >>> i) & 1) !== 0;
    for (let i = 0; i <= 5; i++) setFunction(8, i, bit(i));
    setFunction(8, 7, bit(6));
    setFunction(8, 8, bit(7));
    setFunction(7, 8, bit(8));
    for (let i = 9; i < 15; i++) setFunction(14 - i, 8, bit(i));
    for (let i = 0; i < 8; i++) setFunction(SIZE - 1 - i, 8, bit(i));
    for (let i = 8; i < 15; i++) setFunction(8, SIZE - 15 + i, bit(i));
    setFunction(8, SIZE - 8, true);
  };
  drawFormat(0);

  const data = dataCodewords(digits);
  const codewords = [...data, ...reedSolomonRemainder(data, reedSolomonDivisor(EC_CODEWORDS))];
  let index = 0;
  for (let right = SIZE - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vertical = 0; vertical < SIZE; vertical++) {
      for (let j = 0; j < 2; j++) {
        const x = right - j;
        const upward = ((right + 1) & 2) === 0;
        const y = upward ? SIZE - 1 - vertical : vertical;
        if (!reserved[y][x] && index < codewords.length * 8) {
          modules[y][x] = ((codewords[index >>> 3] >>> (7 - (index & 7))) & 1) !== 0;
          index++;
        }
      }
    }
  }

  const applyMask = (mask: number) => {
    for (let y = 0; y < SIZE; y++) {
      for (let x = 0; x < SIZE; x++) {
        if (!reserved[y][x] && MASKS[mask](x, y)) modules[y][x] = !modules[y][x];
      }
    }
  };

  let best = 0;
  let bestScore = Infinity;
  for (let mask = 0; mask < MASKS.length; mask++) {
    applyMask(mask);
    drawFormat(mask);
    const score = penalty(modules);
    if (score < bestScore) {
      best = mask;
      bestScore = score;
    }
    applyMask(mask); // undo (XOR)
  }
  applyMask(best);
  drawFormat(best);
  return modules;
}
