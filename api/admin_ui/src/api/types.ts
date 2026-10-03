// TypeScript types for the admin API

// Active operator fields consumed by Send and Plugins. The shell builds this
// from /auth/context; Settings edits the stored configuration separately.
export interface AdminConfig {
  fax_disabled: boolean;
  max_file_size_mb: number;
  branding?: { docs_base?: string; logo_path?: string };
  inbound?: { enabled: boolean };
  v3_plugins?: { enabled: boolean };
}

export interface HealthStatus {
  timestamp: string;
  backend: string;
  backend_healthy: boolean;
  jobs: {
    queued: number;
    in_progress: number;
    recent_failures: number;
    held?: number;
    reconciliation_required?: number;
  };
  inbound_enabled: boolean;
  api_keys_configured: boolean;
  require_auth: boolean;
}

export interface DeliveryMetadata {
  delivery_state?: string | null;
  dispatch_mode?: string | null;
  delivery_version?: number | null;
  reconciliation_reason?: string | null;
}

export interface FaxSendResult extends DeliveryMetadata {
  id: string;
  status: string;
}

export interface FaxJob extends DeliveryMetadata {
  id: string;
  to_number: string;
  status: string;
  backend: string;
  pages?: number;
  error?: string;
  provider_sid?: string | null;
  created_at: string;
  updated_at: string;
  file_name?: string;
}

export interface DeliveryHistoryEvent {
  id: string;
  attempt_id: string | null;
  kind: string;
  created_at: string;
  details: {
    category?: string;
    status?: string;
    dispatch_mode?: string;
    actor?: string;
    provider_sid?: string;
    legacy_status?: string;
  };
}

export interface OperatorDelivery {
  version: number;
  state: string;
  dispatch_mode: string;
  provider_id: string | null;
  profile_id: string | null;
  revision_id: string | null;
  attempt: {
    id: string;
    phase: string;
    provider_sid: string | null;
    submitted_at: string | null;
    completed_at: string | null;
  } | null;
  can_bind_provider_identity: boolean;
  bind_refusal_reason: string | null;
  events: DeliveryHistoryEvent[];
  events_truncated: boolean;
}

export interface ProviderIdentityConfirmation {
  expected_version: number;
  provider_sid: string;
  confirm_original_account: true;
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
  _meta?: {
    active_revision_id: string;
    desired_revision_id: string;
    generation: number;
    apply_state: 'applied' | 'pending_restart';
    pending_fields: string[];
    [hint: string]: unknown;
  };
  backend: {
    type: string;
    disabled: boolean;
  };
  hybrid?: {
    outbound_backend: string;
    inbound_backend: string;
    outbound_override?: string;
    inbound_override?: string;
    outbound_explicit?: boolean;
    inbound_explicit?: boolean;
  };
  phaxio: {
    api_key: string;
    api_secret: string;
    callback_token: string;
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
    base_url?: string;
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
    from_sms?: string;
    webhook_signing_key?: string;
    status_poll_seconds?: number;
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
    esl_password?: string;
    gateway_name?: string;
    caller_id_number?: string;
    t38_enable?: boolean;
  };
  security: {
    api_key?: string;
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
    s3_kms_key_id?: string;
  };
  database?: {
    url: string;
    persistent: boolean;
    scheme?: string;
    editable?: boolean;
    maintenance_required?: boolean;
  };
  audit?: {
    enabled: boolean;
    format: string;
    file: string;
    syslog: boolean;
    syslog_address: string;
  };
  persisted?: { enabled: boolean; path: string };
  mcp?: {
    sse_enabled: boolean;
    sse_path: string;
    http_enabled: boolean;
    http_path: string;
    require_oauth: boolean;
    oauth: { issuer: string; audience: string; jwks_url: string };
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
      basic_user?: string;
      basic_pass?: string;
      hmac_secret?: string;
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
    artifact_ttl_days?: number;
    cleanup_interval_minutes?: number;
  };
}

// Settings uses flat ConfigurationValues public patch names. Null preserves a
// value; an explicit empty string clears a string, and false/zero are values.
export type SettingsPatch = Record<string, string | number | boolean | null | undefined> & {
  expected_revision_id?: string;
};

export type PluginRole = 'outbound' | 'inbound' | 'storage';

// A confirmed write is not a read projection. Editors must obtain a
// separately authorized snapshot before allowing another mutation.
export interface ConfigurationWriteResult {
  ok: true;
  changed: boolean;
  _meta: {
    active_revision_id: string;
    desired_revision_id: string;
    generation: number;
    apply_state: 'applied' | 'pending_restart';
    restart_recommended: boolean;
  };
}

export interface PluginConfiguration {
  enabled: boolean;
  settings: Record<string, unknown>;
  role: PluginRole;
  _meta: ConfigurationWriteResult['_meta'];
}

export interface PluginConfigurationPatch {
  expected_revision_id: string;
  role: PluginRole;
  enabled?: boolean;
  settings?: Record<string, unknown>;
}

export type DiagnosticsValue = string | number | boolean | null | DiagnosticsValue[] | { [key: string]: DiagnosticsValue };
export type DiagnosticsOutcome = 'pass' | 'fail' | 'warning' | 'info' | 'not_applicable';

export interface DiagnosticsResult {
  timestamp: string;
  backend: string;
  default_backend: string;
  outbound_backend: string;
  inbound_backend: string;
  configuration: {
    active_revision_id: string;
    desired_revision_id: string;
    generation: number;
    pending_restart: boolean;
  };
  checks: Record<string, Record<string, DiagnosticsValue>>;
  check_outcomes: Record<string, Record<string, DiagnosticsOutcome>>;
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

// Authentication and access management (/auth/*, /access/*). Datetimes are
// naive UTC ISO strings; format them with src/api/time.ts.

export type PrincipalKind = 'user' | 'integration' | 'bootstrap';

export interface AuthPrincipal {
  id: string;
  kind: PrincipalKind;
  display_name: string;
  version: number;
}

export interface AuthMe {
  principal: AuthPrincipal;
  source: 'key' | 'session';
  password_change_required: boolean;
  policy_version: number;
  permissions: string[];
  session: { id: string; source_kind: 'password' | 'key' | 'bootstrap'; expires_at: string } | null;
  can_enroll_owner: boolean;
  csrf_token?: string | null;
  is_owner?: boolean;
  grantable?: { installation: string[] };
}

export interface ConsoleContext {
  policy_version: number;
  permissions: string[];
  navigation: { jobs: boolean; inbox: boolean; send: boolean };
  send: { fax_disabled: boolean; max_file_size_mb: number } | null;
  inbound_enabled: boolean | null;
  branding: { docs_base: string; logo_path: string };
  provider_view: { plugins_enabled: boolean; install_enabled: boolean; active_outbound: string; active_inbound: string } | null;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export type PermissionGroup = 'fax' | 'inbound' | 'identity' | 'config' | 'host' | 'mailbox' | 'audit';

export interface PermissionInfo {
  permission: string;
  group: PermissionGroup;
  description: string;
}

export interface AccessRole {
  id: string;
  name: string;
  description: string;
  builtin: boolean;
  enabled: boolean;
  permissions: string[];
  version: number;
}

export interface AccessUser {
  id: string;
  kind: PrincipalKind;
  login: string | null;
  display_name: string;
  enabled: boolean;
  password_change_required: boolean | null;
  created_at: string;
  last_login_at: string | null;
  version: number;
}

export type ResourceKind = 'installation' | 'mailbox' | 'personal' | 'legacy';

export interface AccessAssignment {
  id: string;
  subject: { kind: 'principal' | 'group'; id: string; name: string };
  role: { id: string; name: string; builtin: boolean };
  resource: { id: string; kind: ResourceKind; name: string };
  version: number;
}

export interface KeyCeiling {
  permission: string;
  resource_id: string;
}

export interface AccessKey {
  id: string;
  principal: { id: string; display_name: string; kind: PrincipalKind };
  name: string;
  note: string;
  expires_at: string | null;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  pending_review: boolean;
  ceiling: KeyCeiling[];
  version: number;
}

export interface AccessUserDetail extends AccessUser {
  memberships: Array<{ membership_id: string; group_id: string; group_name: string; version: number }>;
  assignments: AccessAssignment[];
  keys: AccessKey[];
  effective: { installation: string[]; personal: string[] };
}

export interface AccessGroup {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  member_count: number;
  version: number;
}

export interface AccessGroupDetail extends AccessGroup {
  members: Array<{ membership_id: string; principal_id: string; display_name: string; kind: PrincipalKind; version: number }>;
  assignments: AccessAssignment[];
}

export interface AccessResource {
  id: string;
  kind: ResourceKind;
  name: string;
  parent_id: string | null;
  mailbox_id: string | null;
  principal_id: string | null;
}

export interface AccessSession {
  session_id: string;
  principal: { id: string; display_name: string };
  source_kind: 'password' | 'key' | 'bootstrap';
  created_at: string;
  last_used_at: string;
  expires_at: string;
  revoked_at: string | null;
  current: boolean;
}

export interface AccessMailbox {
  id: string;
  label: string;
  enabled: boolean;
  resource_id: string;
  rule_count: number;
  version: number;
}

export interface InboundRule {
  id: string;
  to_number: string;
  mailbox_id: string;
  mailbox_label: string;
  version: number;
}
