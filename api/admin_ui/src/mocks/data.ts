import type { HealthStatus, FaxJob, ApiKey, Settings, DiagnosticsResult, ValidationResult, InboundFax } from '../api/types';

export const demoHealth: HealthStatus = {
  timestamp: new Date().toISOString(),
  backend: 'phaxio',
  backend_healthy: true,
  jobs: { queued: 1, in_progress: 0, recent_failures: 0 },
  inbound_enabled: true,
  api_keys_configured: true,
  require_auth: true,
};

export const demoJobs: FaxJob[] = [
  { id: 'job_001', to_number: '+1 (415) 555‑0111', status: 'sent', backend: 'phaxio', pages: 2, created_at: new Date(Date.now()-3600e3).toISOString(), updated_at: new Date(Date.now()-3500e3).toISOString(), file_name: 'referral.pdf' },
  { id: 'job_002', to_number: '+1 (206) 555‑0144', status: 'queued', backend: 'phaxio', created_at: new Date(Date.now()-1200e3).toISOString(), updated_at: new Date(Date.now()-1100e3).toISOString(), file_name: 'labs.pdf' },
  { id: 'job_003', to_number: '+1 (312) 555‑0177', status: 'failed', backend: 'phaxio', error: 'No answer', created_at: new Date(Date.now()-7200e3).toISOString(), updated_at: new Date(Date.now()-7100e3).toISOString(), file_name: 'auth.pdf' },
];

export const demoInbound: InboundFax[] = [
  { id: 'in_001', fr: '+1 (555) 000‑1111', to: '+1 (555) 222‑3333', status: 'received', backend: 'phaxio', pages: 3, received_at: new Date(Date.now()-5000e3).toISOString() },
  { id: 'in_002', fr: '+1 (555) 444‑5555', to: '+1 (555) 222‑3333', status: 'received', backend: 'phaxio', pages: 1, received_at: new Date(Date.now()-2000e3).toISOString() },
];

export const demoApiKeys: ApiKey[] = [
  { key_id: 'k_live_demo1', name: 'Ops', scopes: ['admin','send','inbound'], owner: 'Faxbot', created_at: new Date(Date.now()-86400e3).toISOString(), last_used_at: new Date().toISOString() },
  { key_id: 'k_live_demo2', name: 'Automation', scopes: ['send'], owner: 'CI', created_at: new Date(Date.now()-172800e3).toISOString() },
];

export const demoSettings: Settings = {
  backend: { type: 'phaxio', disabled: false },
  phaxio: { api_key: '***', api_secret: '***', callback_url: 'https://example.com/phaxio-callback', verify_signature: true, configured: true },
  sinch: { project_id: '', api_key: '', api_secret: '', configured: false },
  sip: { ami_host: 'asterisk', ami_port: 5038, ami_username: 'api', ami_password: '****', ami_password_is_default: false, station_id: '+15551234567', configured: false },
  security: { require_api_key: true, enforce_https: true, audit_enabled: true, public_api_url: 'https://demo.faxbot.net' },
  storage: { backend: 'local', s3_bucket: '', s3_kms_enabled: false, s3_region: '', s3_prefix: '', s3_endpoint_url: '' },
  inbound: { enabled: true, retention_days: 30, token_ttl_minutes: 60, phaxio: { verify_signature: true }, sinch: { verify_signature: true, basic_auth_configured: false, hmac_configured: false } },
  features: { v3_plugins: true, fax_disabled: false, inbound_enabled: true, plugin_install: false },
  limits: { max_file_size_mb: 10, pdf_token_ttl_minutes: 60, rate_limit_rpm: 120, inbound_list_rpm: 30, inbound_get_rpm: 60 },
};

export const demoValidation: ValidationResult = {
  backend: 'phaxio',
  checks: {
    credentials: 'ok',
    connectivity: 'ok',
  },
  test_fax: {
    sent: true,
    job_id: 'job_999',
  },
};

export const demoDiagnostics: DiagnosticsResult = {
  timestamp: new Date().toISOString(),
  backend: 'phaxio',
  checks: {
    system: {
      database_connected: true,
      ghostscript: true,
      fax_data_writable: true,
      version: 'demo-1.0.0',
      os: 'Darwin',
      python: '3.11',
    },
    phaxio: {
      api_key_set: true,
      api_secret_set: true,
      callback_url_set: true,
      public_url_https: true,
    },
    sinch: {
      project_id_set: false,
      api_key_set: false,
      api_secret_set: false,
    },
    sip: {
      ami_reachable: false,
      ami_password_not_default: false,
    },
    storage: {
      backend: 'local',
      kms_enabled: false,
      bucket_set: false,
    },
    inbound: {
      enabled: true,
      token_ttl: 60,
      retention_days: 30,
    },
    security: {
      enforce_https: true,
      audit_logging: true,
      rate_limiting: true,
      pdf_token_ttl: 60,
    },
  },
  summary: {
    healthy: true,
    critical_issues: [],
    warnings: [
      'SIP/Asterisk not reachable (expected in demo)',
      'Sinch not configured (demo backend set to Phaxio)',
    ],
  },
};
