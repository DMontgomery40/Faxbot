import { McpError, ErrorCode } from '@modelcontextprotocol/sdk/types.js';
import { extractTextFromPDF } from '../shared/pdf-extractor.js';
import fs from 'fs';
import path from 'path';

export const internalPdfTools = [
  {
    name: 'extract_pdf_text',
    description: 'Extracts text from a local PDF file path. INTERNAL only.',
    inputSchema: {
      type: 'object',
      properties: {
        filePath: { type: 'string', description: 'Absolute path to PDF file' },
      },
      required: ['filePath'],
    },
  },
];

export async function handleExtractPdfText(args) {
  const { filePath } = args || {};
  if (!filePath || typeof filePath !== 'string') {
    throw new McpError(ErrorCode.InvalidParams, 'filePath must be provided as a string');
  }
  try {
    // Allow relative input but resolve for consistency
    const resolved = path.isAbsolute(filePath) ? filePath : path.resolve(process.cwd(), filePath);
    await fs.promises.access(resolved, fs.constants.R_OK);
    const text = await extractTextFromPDF(resolved);
    return text;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (/File not found/.test(msg) || /ENOENT/.test(msg)) {
      throw new McpError(ErrorCode.InvalidParams, `File not found: ${filePath}`);
    }
    throw new McpError(ErrorCode.InternalError, `PDF extraction failed: ${msg}`);
  }
}

export default { internalPdfTools, handleExtractPdfText };

