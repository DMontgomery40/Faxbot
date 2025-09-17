const fs = require('fs');
const path = require('path');
const Ajv = require('ajv');

const ajv = new Ajv({ allErrors: true, strict: false });

function validateManifest(manifest) {
  const errors = [];
  const required = ['id', 'name', 'version', 'description', 'author', 'categories', 'capabilities'];
  for (const k of required) if (!(k in manifest)) errors.push(`Missing required field: ${k}`);
  if (manifest.id && !/^[a-z0-9-]+$/.test(String(manifest.id))) errors.push('ID must be lowercase letters, numbers, and hyphens only');
  if (manifest.version && !/^\d+\.\d+\.\d+/.test(String(manifest.version))) errors.push('Version must be semver format (e.g., 1.0.0)');
  const validCategories = ['outbound', 'inbound', 'storage', 'auth', 'messaging', 'transform', 'custom'];
  if (Array.isArray(manifest.categories)) for (const c of manifest.categories) if (!validCategories.includes(c)) errors.push(`Invalid category: ${c}`);
  const expected = { outbound: ['send_fax', 'get_status'], inbound: ['receive_fax', 'list_inbound'], storage: ['put', 'get', 'delete'], auth: ['authenticate', 'validate_token'], messaging: ['send_message', 'get_message_status'] };
  if (Array.isArray(manifest.categories) && Array.isArray(manifest.capabilities)) {
    for (const cat of manifest.categories) if (expected[cat] && !expected[cat].some((x) => manifest.capabilities.includes(x))) errors.push(`Category ${cat} requires at least one of: ${expected[cat].join(', ')}`);
  }
  return errors;
}

function validateConfigSchema(schema) {
  const errors = [];
  try { ajv.compile(schema); } catch (e) { errors.push(`Invalid JSON Schema: ${e.message}`); return errors; }
  const s = JSON.stringify(schema).toLowerCase();
  for (const f of ['ssn', 'social_security', 'patient_name', 'dob', 'date_of_birth']) if (s.includes(f)) errors.push(`Config schema cannot contain PHI field: ${f}`);
  return errors;
}

function checkHipaaCompliance(code) {
  const warnings = [];
  if (/log.*\(\s*['"])?.*\d{3}[-.]?\d{2}[-.]?\d{4}/i.test(code)) warnings.push('Possible SSN in log statement');
  if (/log.*\(\s*['"])?.*\d{10}/i.test(code)) warnings.push('Possible phone number in log statement');
  if (/console\.log\([^)]*to(_)?number/i.test(code)) warnings.push('Printing phone numbers; use maskPhoneNumber()');
  if (/config\s*\[.*patient|ssn|dob/i.test(code)) warnings.push('Possible PHI being stored in configuration');
  if (code.includes('fs.open(') || code.includes('fs.writeFile(')) warnings.push('File operations should encrypt PHI');
  return warnings;
}

function validatePluginPackage(dir) {
  const result = { valid: true, errors: [], warnings: [] };
  const base = path.resolve(dir || '.');
  const manifestPath = path.join(base, 'manifest.json');
  if (!fs.existsSync(manifestPath)) { result.errors.push('Missing required file: manifest.json'); result.valid = false; }
  else {
    try {
      const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
      const errs = validateManifest(manifest);
      if (errs.length) { result.errors.push(...errs); result.valid = false; }
      if (manifest.config_schema) {
        const serrs = validateConfigSchema(manifest.config_schema);
        if (serrs.length) { result.errors.push(...serrs); result.valid = false; }
      }
    } catch (e) { result.errors.push(`Failed to parse manifest.json: ${e.message}`); result.valid = false; }
  }
  const walk = (p) => {
    for (const entry of fs.readdirSync(p)) {
      const full = path.join(p, entry);
      const stat = fs.statSync(full);
      if (stat.isDirectory()) walk(full);
      else if (/\.(js|ts)$/.test(entry)) {
        try { const code = fs.readFileSync(full, 'utf8'); const warns = checkHipaaCompliance(code); if (warns.length) result.warnings.push(...warns.map((w) => `${entry}: ${w}`)); } catch {}
      }
    }
  };
  walk(base);
  return result;
}

module.exports = { validateManifest, validateConfigSchema, checkHipaaCompliance, validatePluginPackage };

