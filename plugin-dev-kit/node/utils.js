const crypto = require('crypto');

function maskPhoneNumber(phone) {
  if (!phone) return '[EMPTY]';
  const digits = String(phone).replace(/\D/g, '');
  if (digits.length < 4) return '***';
  return `***-***-${digits.slice(-4)}`;
}

function hashDocument(buf) { return crypto.createHash('sha256').update(buf).digest('hex'); }
function generateToken(length = 32) { return crypto.randomBytes(length).toString('base64url'); }
function redactText(text) { if (!text) return text; let out = String(text); out = out.replace(/\b\d{3}-\d{2}-\d{4}\b/g, '***-**-****'); out = out.replace(/\d{10,}/g, '***'); return out; }
function timestamp() { return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'); }

module.exports = { maskPhoneNumber, hashDocument, generateToken, redactText, timestamp };

