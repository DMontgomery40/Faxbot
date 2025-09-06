import fs from 'fs';
import path from 'path';
import os from 'os';
import { execFile } from 'child_process';

function normalizeWhitespace(text) {
  if (!text || typeof text !== 'string') return '';
  // Normalize Windows newlines, collapse more than 2 newlines to 2
  let t = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  // Replace tabs with spaces
  t = t.replace(/\t/g, ' ');
  // Trim spaces at line ends
  t = t.replace(/[ \f\v]+$/gm, '');
  // Collapse 3+ newlines into 2 to keep paragraph breaks
  t = t.replace(/\n{3,}/g, '\n\n');
  // Collapse runs of spaces
  t = t.replace(/ {2,}/g, ' ');
  // Final trim
  return t.trim();
}

export async function extractTextFromBuffer(buffer) {
  if (!buffer || !Buffer.isBuffer(buffer) || buffer.length === 0) {
    throw new Error('Invalid or empty buffer provided');
  }
  try {
    const mod = await import('pdf-parse/lib/pdf-parse.js');
    const pdf = mod.default || mod;
    const data = await pdf(buffer);
    const cleaned = normalizeWhitespace(data.text || '');
    if (!cleaned) {
      throw new Error('No text could be extracted from the PDF');
    }
    return cleaned;
  } catch (err) {
    throw new Error(`PDF extraction failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}

export async function extractTextFromPDF(filePath) {
  try {
    if (!filePath || typeof filePath !== 'string') {
      throw new Error('filePath must be a string');
    }
    // Basic validation
    const isAbs = path.isAbsolute(filePath);
    if (!isAbs) {
      // Not strictly required, but recommended for clarity/safety
      // Accept relative too but resolve to absolute for pdf-parse error messages
      filePath = path.resolve(process.cwd(), filePath);
    }
    await fs.promises.access(filePath, fs.constants.R_OK);
    const stat = await fs.promises.stat(filePath);
    if (!stat.isFile()) {
      throw new Error('Provided path is not a file');
    }
    const buffer = await fs.promises.readFile(filePath);
    const text = await extractTextFromBuffer(buffer);
    if (text && text.length >= 32) return text;
    // Optional OCR fallback using pdftoppm + tesseract if available/enabled
    const ocrEnabled = (process.env.FAXBOT_OCR_ENABLE || 'true').toLowerCase() !== 'false';
    if (ocrEnabled) {
      const ocr = await ocrPdfWithTesseract(filePath);
      if (ocr) return ocr;
    }
    return text;
  } catch (err) {
    if (err && (err.code === 'ENOENT' || err.code === 'ENOTDIR')) {
      throw new Error(`File not found: ${filePath}`);
    }
    throw err instanceof Error ? err : new Error(String(err));
  }
}

export default { extractTextFromPDF, extractTextFromBuffer };

// Helpers
function which(cmd) {
  const sep = process.platform === 'win32' ? ';' : ':';
  const exts = process.platform === 'win32' ? (process.env.PATHEXT || '.EXE;.CMD;.BAT').toLowerCase().split(';') : [''];
  const paths = (process.env.PATH || '').split(sep);
  for (const p of paths) {
    const full = path.join(p, cmd);
    for (const ext of exts) {
      const candidate = full + ext;
      try {
        fs.accessSync(candidate, fs.constants.X_OK);
        return candidate;
      } catch {}
    }
  }
  return null;
}

function execFileAsync(cmd, args, options = {}) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, options, (error, stdout, stderr) => {
      if (error) return reject(Object.assign(error, { stdout, stderr }));
      resolve({ stdout, stderr });
    });
  });
}

async function ocrPdfWithTesseract(pdfPath) {
  const pdftoppm = which('pdftoppm');
  const tesseract = which('tesseract');
  if (!pdftoppm || !tesseract) return '';
  const dpi = parseInt(process.env.FAXBOT_OCR_DPI || '200', 10);
  const tmp = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'faxbot-ocr-'));
  try {
    const prefix = path.join(tmp, 'page');
    await execFileAsync(pdftoppm, ['-r', String(dpi), '-png', pdfPath, prefix], { maxBuffer: 1024 * 1024 * 64 });
    // Collect generated images
    const files = await fs.promises.readdir(tmp);
    const pngs = files.filter((f) => f.startsWith('page-') && f.endsWith('.png')).sort((a, b) => a.localeCompare(b));
    let out = '';
    for (const f of pngs) {
      const img = path.join(tmp, f);
      try {
        const { stdout } = await execFileAsync(tesseract, [img, 'stdout']);
        out += `\n\n${stdout || ''}`;
      } catch {}
    }
    return normalizeWhitespace(out);
  } catch {
    return '';
  } finally {
    // cleanup
    try {
      const files = await fs.promises.readdir(tmp);
      await Promise.all(files.map((f) => fs.promises.unlink(path.join(tmp, f)).catch(() => {})));
      await fs.promises.rmdir(tmp).catch(() => {});
    } catch {}
  }
}
