import type { FaxTogetherSummary } from './batchingTypes';
// TypeScript types for the admin API

// Active operator fields consumed by Send and Plugins. The shell builds this
// from /auth/context; Settings edits the stored configuration separately.
export interface AdminConfig {
  fax_disabled: boolean;
  max_file_size_mb: number;
  // How fax numbers are written in the installation country, when known.
  number_format?: NumberFormat | null;
  branding?: { docs_base?: string; logo_path?: string };
  inbound?: { enabled: boolean };
  v3_plugins?: { enabled: boolean };
}

export interface HealthStatus {
  timestamp: string;
  backend: string;
  backend_healthy: boolean;
  // One plain reason when sending cannot work, such as the fax engine refusing Faxbot's login.
  backend_message?: string | null;
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
  // The destination as the server saved it, in international form.
  to?: string;
}

// The installation country (ISO 3166 alpha-2) and a sample fax number written
// the way people there dial it and in international form. Either sample may be
// empty when the server does not know one.
export interface NumberFormat {
  country: string;
  national: string;
  international: string;
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
  // Present when the fax waited, or went, with other faxes to the same number.
  together?: FaxTogetherSummary | null;
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
    route?: string;
    reason?: string;
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
  humblefax?: {
    access_key: string;
    secret_key: string;
    from_number: string;
    // The fax numbers on the HumbleFax account, read from HumbleFax.
    account_numbers?: string[];
    configured: boolean;
  };
  efax?: {
    app_id: string;
    api_key: string;
    user_id: string;
    caller_id: string;
    csid: string;
    poll_seconds: number;
    delete_after_download: boolean;
    webhook_secret?: string;
    webhook_secret_set?: boolean;
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
    // Faxbot has written this login where its own Asterisk reads it.
    ami_password_shared?: boolean;
    station_id: string;
    configured: boolean;
    // A key Faxbot uses only to read what Telnyx charged for each trunk call.
    telnyx_api_key?: string;
    telnyx_api_key_set?: boolean;
  };
  fs?: {
    esl_host?: string;
    esl_port?: number;
    esl_password?: string;
    gateway_name?: string;
    caller_id_number?: string;
    // Why FreeSWITCH cannot send yet, in a sentence; null when nothing is missing.
    problem?: string | null;
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
    // Diagnostics also check that Faxbot can reach the S3 bucket.
    s3_diagnostics?: boolean;
  };
  database?: {
    url: string;
    persistent: boolean;
    scheme?: string;
    editable?: boolean;
    maintenance_required?: boolean;
  };
  routing?: {
    outbound_routes: string;
    min_success_percent: number;
  };
  intake?: {
    email_enabled: boolean;
    smtp_host: string;
    smtp_port: number;
    smtp_security: 'starttls' | 'tls' | 'none';
    smtp_username: string;
    smtp_password: string;
    email_from: string;
    email_to: string;
    email_subject: string;
  };
  direct?: {
    enabled: boolean;
    organization: string;
    fax_number: string;
    allow_private_peers?: boolean;
  };
  // The header text and station ID faxes sent over the carrier trunk carry.
  sender?: {
    header: string;
    station_id: string;
  };
  numbers?: {
    default_country: string;
    example: { national: string; international: string };
    supported_countries: string[];
  };
  audit?: {
    enabled: boolean;
    format: string;
    file: string;
    syslog: boolean;
    syslog_address: string;
  };
  persisted?: { enabled: boolean; path: string };
  // The installation's time zone (an IANA name such as America/Denver); '' when none is set.
  installation?: { time_zone: string };
  // The address paired phones use on the installation's own network.
  mobile?: { local_base: string };
  // Where the console's help links point.
  developer?: { docs_base_url: string };
  // Whether the console may restart Faxbot.
  restart?: { allowed: boolean };
  // The older settings file, read once when a new installation first starts (read only).
  legacy_config?: { path: string };
  // Where provider plugin files are read from (read only).
  plugin_files?: { providers_dir: string };
  // Environment-only settings, shown read-only, by variable name.
  deployment?: Record<string, DeploymentValue>;
  // Settings only the owner may change, by the names a settings change sends.
  owner_only?: string[];
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

// Diagnostics report (/admin/diagnostics/report): one sentence and at most one fix per check.
export type DiagnosticsStatus = 'ok' | 'attention' | 'problem' | 'off';
export interface DiagnosticsFinding {
  id: string;
  section: string;
  title: string;
  status: DiagnosticsStatus;
  sentence: string;
  fix: { label: string; page: string | null } | null;
}
export interface DiagnosticsReport {
  checked_at: string | null;
  checked_at_text: string;
  status: DiagnosticsStatus | null;
  summary: string | null;
  sections: Array<{ id: string; title: string; checks: DiagnosticsFinding[] }>;
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
  // waiting (the document is still being fetched), received, or failed.
  status: string;
  backend: string;
  pages?: number;
  size_bytes?: number | null;
  // When Faxbot recorded the fax; source_received_at is the provider's own time.
  received_at?: string;
  mailbox?: string | null;
  status_text?: string | null;
  source_received_at?: string | null;
  provider_fax_id?: string | null;
  sha256?: string | null;
  is_test?: boolean;
  retry_at?: string | null;
  problem?: string | null;
  can_fetch_again?: boolean;
  // Brought in later from an image the fax engine could not hand over.
  recovered?: boolean;
  // A sentence about the provider's own copy, such as an eFax deletion Faxbot is still retrying.
  provider_note?: string | null;
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
  navigation: { jobs: boolean; inbox: boolean; send: boolean; work?: boolean };
  send: { fax_disabled: boolean; max_file_size_mb: number; default_country?: string; number_example?: string } | null;
  inbound_enabled: boolean | null;
  branding: { docs_base: string; logo_path: string };
  provider_view: {
    plugins_enabled: boolean; install_enabled: boolean; active_outbound: string; active_inbound: string;
    // Further sending routes, and the carrier or phone system preset the trunk uses.
    extra_routes?: string[]; trunk_preset?: string;
  } | null;
  // Names this installation gives its providers, such as the trunk's carrier ({ sip: 'Telnyx' }).
  provider_names?: Record<string, string>;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

// GET /access/audit: one security audit entry, newest first.
export interface AuditEntry {
  id: string;
  at: string;
  actor: { id: string; display_name: string | null } | null;
  // How the person or system was signed in when it happened.
  credential_kind: 'session' | 'key' | 'bootstrap' | 'system';
  operation: string;
  target: { kind: string; id: string; name: string | null } | null;
  outcome: 'allowed' | 'denied';
  policy_version: number | null;
  details: Record<string, unknown>;
}

// GET /admin/db-status: the database Faxbot uses and whether it can reach it.
export interface DatabaseStatus {
  url: string;
  engine: 'sqlite' | 'postgres' | 'mysql' | 'unknown';
  connected: boolean;
  error: string | null;
  // Rows this person can see; api_keys is null without keys:manage.
  counts: { fax_jobs?: number; inbound_fax?: number; api_keys?: number | null };
  sqlite: { path: string; exists: boolean; size_bytes?: number; modified?: string; persistent_volume?: boolean } | null;
}

// An environment-only setting: whether it is set, and its value unless it is a secret.
export interface DeploymentValue {
  set: boolean;
  value: string | null;
  // ENABLE_ADMIN_EXEC only: whether the terminal is on.
  effective?: boolean;
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
  description: string | null;
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
  // Older migrated keys may have no name or note.
  name: string | null;
  note: string | null;
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
  description: string | null;
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

// Work queue: received documents with an owner, an acknowledgement target and a history.
export type WorkView = 'all' | 'mine' | 'unassigned' | 'overdue';
export type WorkStateKey = 'waiting' | 'assigned' | 'overdue' | 'escalated' | 'acknowledged' | 'done';
export type WorkAction = 'assign' | 'acknowledge' | 'done' | 'reopen' | 'export' | 'document';

export interface WorkPerson {
  id: string;
  name: string | null;
}

export interface WorkItem {
  id: string;
  inbound_fax_id: string;
  state: 'open' | 'acknowledged' | 'done';
  state_key: WorkStateKey;
  state_text: string;
  due_text: string;
  due_at: string | null;
  due_hours: number | null;
  due_source: 'mailbox' | 'installation' | null;
  available_at: string;
  from_number: string | null;
  to_number: string | null;
  pages: number | null;
  mailbox: string | null;
  owner: WorkPerson | null;
  backup: WorkPerson | null;
  assigned_at: string | null;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  escalated_at: string | null;
  done_at: string | null;
  done_by: string | null;
  done_note: string | null;
  duplicate_of: { id: string; available_at: string } | null;
  is_mine: boolean;
  overdue: boolean;
  is_test?: boolean;
  version: number;
  actions: WorkAction[];
  owner_can_see?: boolean | null;
}

export interface WorkCounts {
  open: number;
  acknowledged: number;
  done: number;
  unassigned: number;
  mine: number;
  overdue: number;
}

export interface WorkEvent {
  kind: string;
  occurred_at: string;
  actor: string | null;
  text: string;
}

export interface WorkAssignee {
  id: string;
  name: string;
  login: string;
}

export interface WorkMailboxSetting {
  mailbox_id: string;
  label: string;
  enabled: boolean;
  acknowledge_hours: number | null;
  backup: WorkPerson | null;
  version: number;
  people: WorkAssignee[];
}

export interface WorkSettings {
  acknowledge_hours: number;
  mailboxes: WorkMailboxSetting[];
}

export interface ImportManifest {
  source_system: string;
  operation_id: string;
  revision?: string;
  source_received_at?: string;
  to_number?: string;
  from_number?: string;
  pages?: number;
}

export interface ImportResult {
  import_id: string;
  inbound_id: string;
  status: 'received' | 'duplicate';
}

// GET /admin/inbound/efax: Faxbot checking eFax for received faxes, and copies left at eFax.
export interface EfaxStatus {
  receiving: boolean;
  checked_at: string | null;
  problem: string | null;
  pending_deletions: number;
  stopped_deletions: number;
  notes: string[];
}
