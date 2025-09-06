import fs from 'fs';
import path from 'path';

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
    return await extractTextFromBuffer(buffer);
  } catch (err) {
    if (err && (err.code === 'ENOENT' || err.code === 'ENOTDIR')) {
      throw new Error(`File not found: ${filePath}`);
    }
    throw err instanceof Error ? err : new Error(String(err));
  }
}

export default { extractTextFromPDF, extractTextFromBuffer };
