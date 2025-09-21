// TypeScript types for the admin API

export interface HealthStatus {
  timestamp: string;
  backend: string;
  backend_healthy: boolean;
  jobs: {
    queued: number;
    in_progress: number;
    recent_failures: number;
  };
  inbound_enabled: boolean;
  api_keys_configured: boolean;
  require_auth: boolean;
}

export interface FaxJob {
  id: string;
  to_number: string;
  status: string;
  backend: string;
  pages?: number;
  error?: string;
  created_at: string;
  updated_at: string;
  file_name?: string;
}

export interface ApiKey {
  key_id: string;
  name?: string;
  scopes: string[];
  owner?: string;
  created_at: string;
  last_used_at?: string;
  expires_at?: string;
}

export interface Settings {
  backend: {
    type: string;
    disabled: boolean;
  };
  hybrid?: {
    outbound_backend: string;
    inbound_backend: string;
    outbound_explicit?: boolean;
    inbound_explicit?: boolean;
  };
  phaxio: {
    api_key: string;
    api_secret: string;
    callback_url: string;
    verify_signature: boolean;
    configured: boolean;
  };
  documo?: {
    api_key: string;
    base_url?: string;
    sandbox?: boolean;
    configured: boolean;
  };
  sinch: {
    project_id: string;
    api_key: string;
    api_secret: string;
    configured: boolean;
  };
  signalwire?: {
    space_url: string;
    project_id: string;
    api_token: string;
    from_fax: string;
    callback_url?: string;
    configured: boolean;
  };
  sip: {
    ami_host: string;
    ami_port: number;
    ami_username: string;
    ami_password: string;
    ami_password_is_default: boolean;
    station_id: string;
    configured: boolean;
  };
  fs?: {
    esl_host?: string;
    esl_port?: number;
    gateway_name?: string;
    caller_id_number?: string;
    t38_enable?: boolean;
  };
  security: {
    require_api_key: boolean;
    enforce_https: boolean;
    audit_enabled: boolean;
    public_api_url: string;
  };
  storage: {
    backend: string;
    s3_bucket: string;
    s3_kms_enabled: boolean;
    s3_region?: string;
    s3_prefix?: string;
    s3_endpoint_url?: string;
  };
  database?: {
    url: string;
    persistent: boolean;
  };
  inbound: {
    enabled: boolean;
    retention_days: number;
    token_ttl_minutes?: number;
    sip?: {
      asterisk_secret: string;
      configured: boolean;
    };
    phaxio?: {
      verify_signature: boolean;
    };
    sinch?: {
      verify_signature: boolean;
      basic_auth_configured: boolean;
      hmac_configured: boolean;
    };
  };
  features?: {
    v3_plugins: boolean;
    fax_disabled: boolean;
    inbound_enabled: boolean;
    plugin_install: boolean;
  };
  limits: {
    max_file_size_mb: number;
    pdf_token_ttl_minutes: number;
    rate_limit_rpm: number;
    inbound_list_rpm?: number;
    inbound_get_rpm?: number;
  };
}

export interface DiagnosticsResult {
  timestamp: string;
  backend: string;
  checks: Record<string, any>;
  summary: {
    healthy: boolean;
    critical_issues: string[];
    warnings: string[];
  };
}

export interface ValidationResult {
  backend: string;
  checks: Record<string, any>;
  test_fax?: {
    sent: boolean;
    job_id?: string;
    error?: string;
  };
}

export interface InboundFax {
  id: string;
  fr?: string;
  to?: string;
  status: string;
  backend: string;
  pages?: number;
  received_at?: string;
}

// Tunnel types
export interface TunnelStatus {
  enabled: boolean;
  provider: 'none' | 'cloudflare' | 'wireguard' | 'tailscale';
  status: 'disabled' | 'connecting' | 'connected' | 'error';
  public_url?: string;
  local_ip?: string;
  last_checked?: string;
  error_message?: string;
}
