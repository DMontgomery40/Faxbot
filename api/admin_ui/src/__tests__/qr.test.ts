import { describe, expect, it } from 'vitest';
import jsQR from 'jsqr';
import { qrMatrix } from '../components/common/qr';

// Rasterize with a 4-module quiet zone and decode with an independent reader.
function decode(matrix: boolean[][], scale = 8): string | null {
  const quiet = 4;
  const modules = matrix.length + quiet * 2;
  const width = modules * scale;
  const pixels = new Uint8ClampedArray(width * width * 4).fill(255);
  matrix.forEach((row, y) => row.forEach((dark, x) => {
    if (!dark) return;
    for (let dy = 0; dy < scale; dy++) {
      for (let dx = 0; dx < scale; dx++) {
        const offset = (((y + quiet) * scale + dy) * width + (x + quiet) * scale + dx) * 4;
        pixels[offset] = pixels[offset + 1] = pixels[offset + 2] = 0;
      }
    }
  }));
  return jsQR(pixels, width, width)?.data ?? null;
}

describe('pairing QR code', () => {
  it.each(['000000', '123456', '987650', '4', '0123456789', '1234567890123456789012345678901234'])('round-trips %s', (code) => {
    const matrix = qrMatrix(code);
    expect(matrix).not.toBeNull();
    expect(matrix!.length).toBe(21);
    expect(decode(matrix!)).toBe(code);
  });

  it('refuses payloads it cannot encode', () => {
    expect(qrMatrix('')).toBeNull();
    expect(qrMatrix('12ab56')).toBeNull();
    expect(qrMatrix('1'.repeat(35))).toBeNull();
  });
});
