import type { SipCallPage, SipPreset, SipTrunkStatus } from './sipTypes';
import type {
  HealthStatus,
  FaxJob,
  FaxSendResult,
  OperatorDelivery,
  ProviderIdentityConfirmation,
  Settings,
  SettingsPatch,
  PluginConfiguration,
  PluginConfigurationPatch,
  ConfigurationWriteResult,
  PluginRole,
  DiagnosticsResult,
  ValidationResult,
  InboundFax,
  AuthMe,
  ConsoleContext,
  Page,
  PermissionInfo,
  AccessRole,
  AccessUser,
  AccessUserDetail,
  AccessGroup,
  AccessGroupDetail,
  AccessResource,
  AccessAssignment,
  AccessKey,
  AccessSession,
  AccessMailbox,
  InboundRule,
  KeyCeiling,
  ResourceKind,
} from './types';
import type {
  Destination,
  DestinationDetail,
  DestinationPatch,
  DirectCard,
  DirectDeliveryRecord,
  DirectPartner,
  EmailConnector,
  EmailConnectorInput,
  IntakeCounts,
  IntakeItem,
  ProviderCosts,
  RateCard,
} from './deliveryTypes';
import type {
  ImportManifest, ImportResult, WorkAssignee, WorkCounts, WorkEvent, WorkItem, WorkSettings, WorkView,
} from './types';

// These manifest validation messages contain no paths, credentials, or provider
// responses. All other server error bodies remain opaque to the UI.
const safeManifestDetails = new Set([
  'Manifest id is required',
  'Manifest id required',
  'manifest.id missing',
  'Invalid provider id or path',
  'HTTP provider id is reserved for storage.',
  'Invalid HTTP provider manifest.',
  'No manifest candidates provided',
  'Fax sending is disabled. Validate the manifest without sending, or use Send to queue a test document.',
]);

const safeRefreshDetails = new Set([
  'This provider reports status through callbacks; refresh is unsupported.',
  'This fax requires reconciliation with its original provider account before refresh.',
  'Provider status is temporarily unavailable. This fax has not been resubmitted.',
]);

// Fixed 409 refusals from attaching a provider fax ID (outbound_store.py
// _bind_refusal and bind_provider_identity). Plain server sentences are shown
// as-is; the others map to a plain equivalent.
const safeReconciliationDetails = new Map<string, string>([
  ['Delivery record is unavailable.', 'This fax is no longer available. Reload the job.'],
  ['Delivery changed; reload before attaching a provider identity.', 'This fax changed. Reload the job and try again.'],
  ['This provider identity already belongs to another delivery from the original account.',
    'This fax ID already belongs to another fax from the same provider account.'],
  ...[
    'This fax is not waiting for confirmation, so it does not need a provider fax ID.',
    'This fax was sent by an older Faxbot version; check its status in your provider account.',
    'This fax was not sent through a provider, so there is no fax ID to attach.',
    'Faxbot has no record of sending this fax, so there is no fax ID to attach.',
    'This fax already has a final result, so there is no fax ID to attach.',
    'The provider account that sent this fax is no longer set up; check the fax in that account.',
    'This fax already has a provider fax ID; use Refresh to update its status.',
    'This provider cannot look up fax status; check the fax in your provider account.',
  ].map((detail): [string, string] => [detail, detail]),
]);

const safeReconciliationInputDetails = new Map<string, string>([
  ['Confirm that this fax ID matches the fax in its original provider account.',
    'Confirm that this fax ID matches the fax in its original provider account.'],
  ['Invalid provider identity reconciliation input.', 'Enter the fax ID exactly as your provider shows it.'],
]);

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
const CSRF_FAILURE = 'Browser request verification failed';
export const TRANSPORT_REFUSED = 'Credential transport or browser origin is not allowed.';
export const POLICY_CHANGED = 'Access settings changed; review and save again.';

export function normalizeFaxDestination(number: string): string {
  return number.replace(/[\s\-\(\)]/g, '');
}

export class AdminAPIError extends Error {
  constructor(readonly status: number, statusText: string, readonly detail: string | null = null) {
    super(`API Error: ${status} ${statusText}`);
  }
}

// A refusal the server wrote as one plain sentence for the person entering a
// value, such as a fax number it cannot read. Generic refusals stay generic.
const PLAIN_SENTENCE = /^[A-Z][^<>{}]{3,240}[.!?]$/;
const GENERIC_REFUSALS = new Set(['Invalid access request.', 'Invalid fax request.', 'Invalid credential input.']);

export function plainRefusal(error: unknown): string | null {
  if (!(error instanceof AdminAPIError) || (error.status !== 400 && error.status !== 422)) return null;
  const detail = error.detail?.trim();
  return detail && PLAIN_SENTENCE.test(detail) && !GENERIC_REFUSALS.has(detail) ? detail : null;
}

// The server refused a fax before accepting it, so nothing was sent.
export class FaxRefusedError extends Error {}

// Faxbot's fixed sentences for a missing connection to its fax engine (Asterisk).
export const FAX_ENGINE_SENTENCES = new Set([
  "Faxbot can't sign in to its fax engine. Check that the Asterisk manager password matches.",
  "Faxbot can't reach its fax engine. Check that the Asterisk service is running.",
  'Faxbot is still connecting to its fax engine.',
  'Faxbot connects to its fax engine when the SIP trunk is the provider in use.',
]);

export function configurationWriteRejected(error: unknown): boolean {
  return error instanceof AdminAPIError && [400, 401, 403, 404, 409, 413, 422].includes(error.status);
}

export function isNotAvailable(error: unknown): boolean {
  return error instanceof AdminAPIError && (error.status === 404 || error.status === 405);
}

export function isForbidden(error: unknown): boolean {
  return error instanceof AdminAPIError && error.status === 403;
}

export function isConflict(error: unknown): boolean {
  return error instanceof AdminAPIError && error.status === 409;
}

// One plain sentence for an access-management failure.
export function accessErrorMessage(error: unknown): string {
  if (error instanceof AdminAPIError) {
    switch (error.status) {
      case 400:
      case 422: return 'Check the entered values and try again.';
      case 401: return 'Your session has ended. Sign in again.';
      case 403: return 'You do not have permission to do this.';
      case 404: return 'This item no longer exists. Reload and try again.';
      case 409: return POLICY_CHANGED;
      case 429: return 'Too many attempts. Try again later.';
      case 503: return 'The server is busy. Try again in a moment.';
      default: return 'The request failed. Try again.';
    }
  }
  if (error instanceof TypeError) return 'Could not reach the server. Check the connection and try again.';
  return error instanceof Error && error.message ? error.message : 'The request failed. Try again.';
}

function configurationResult(value: unknown): ConfigurationWriteResult {
  const result = value as Partial<ConfigurationWriteResult> | null;
  const meta = result?._meta;
  const hasExactKeys = (candidate: object, keys: string[]) =>
    Object.keys(candidate).length === keys.length && keys.every(key => Object.prototype.hasOwnProperty.call(candidate, key));
  if (!result || typeof result !== 'object'
      || !hasExactKeys(result, ['ok', 'changed', '_meta'])
      || result.ok !== true || typeof result.changed !== 'boolean'
      || !meta || typeof meta !== 'object'
      || !hasExactKeys(meta, ['active_revision_id', 'desired_revision_id', 'generation', 'apply_state', 'restart_recommended'])
      || typeof meta.active_revision_id !== 'string' || !meta.active_revision_id
      || typeof meta.desired_revision_id !== 'string' || !meta.desired_revision_id
      || !Number.isSafeInteger(meta.generation) || meta.generation < 1
      || !['applied', 'pending_restart'].includes(meta.apply_state)
      || typeof meta.restart_recommended !== 'boolean'
      || meta.restart_recommended !== (meta.apply_state === 'pending_restart')) {
    throw new Error('The server returned an unexpected reply. Reload and try again.');
  }
  return result as ConfigurationWriteResult;
}

async function readDetail(res: Response): Promise<string | null> {
  const body = await res.clone().json().catch(() => null);
  return typeof body?.detail === 'string' ? body.detail : null;
}

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && String(value).length > 0) search.append(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

const id = (value: string) => encodeURIComponent(value);

// A browser session (cookie + CSRF header) or an explicit API key kept in
// memory. The console never sends an empty X-API-Key: an explicit header is
// the only credential the server considers for that request.
export type ClientCredential =
  | { kind: 'session'; csrf: string | null }
  | { kind: 'key'; key: string };

export interface ClientOptions {
  onUnauthorized?: () => void;
}

type RequestOptions = { method?: string; body?: string | FormData; headers?: Record<string, string> };
type RequestExtras = { manifestValidation?: boolean; quiet401?: boolean };
type PolicyResult = { policy_version: number };

export class AdminAPIClient {
  private baseURL: string;
  private credential: ClientCredential;
  private onUnauthorized?: () => void;
  // Last access policy version seen from /auth/me or a management reply.
  policyVersion: number | null = null;
  private policyRefresh: Promise<void> | null = null;

  constructor(credential: ClientCredential = { kind: 'session', csrf: null }, options: ClientOptions = {}) {
    this.baseURL = window.location.origin;
    this.credential = credential;
    this.onUnauthorized = options.onUnauthorized;
  }

  get credentialKind(): ClientCredential['kind'] {
    return this.credential.kind;
  }

  private authHeaders(method: string): Record<string, string> {
    if (this.credential.kind === 'key') {
      return this.credential.key ? { 'X-API-Key': this.credential.key } : {};
    }
    return !SAFE_METHODS.has(method) && this.credential.csrf ? { 'X-CSRF-Token': this.credential.csrf } : {};
  }

  // Every request goes through here: credential headers, one CSRF refresh
  // and retry, and the client-wide unauthorized callback.
  private async send(path: string, init: RequestOptions = {}, extras: RequestExtras = {}): Promise<Response> {
    const method = (init.method || 'GET').toUpperCase();
    const attempt = () => fetch(`${this.baseURL}${path}`, {
      method,
      body: init.body,
      credentials: 'same-origin',
      headers: { ...this.authHeaders(method), ...init.headers },
    });
    let res = await attempt();
    if (res.status === 403 && this.credential.kind === 'session' && !SAFE_METHODS.has(method)) {
      const detail = await readDetail(res);
      if (detail?.startsWith(CSRF_FAILURE) && await this.refreshCsrf()) res = await attempt();
    }
    if (res.status === 401 && !extras.quiet401 && await this.credentialRejected(path)) this.onUnauthorized?.();
    return res;
  }

  // A 401 from a single route may only mean that route does not accept this
  // kind of credential; the session has ended only if /auth/me agrees.
  private async credentialRejected(path: string): Promise<boolean> {
    if (path.startsWith('/auth/')) return true;
    const probe = await fetch(`${this.baseURL}/auth/me`, {
      credentials: 'same-origin',
      headers: this.authHeaders('GET'),
    }).catch(() => null);
    return probe?.status === 401;
  }

  private async refreshCsrf(): Promise<boolean> {
    const res = await fetch(`${this.baseURL}/auth/me`, { credentials: 'same-origin' }).catch(() => null);
    if (!res?.ok) return false;
    const me = await res.json().catch(() => null);
    if (typeof me?.csrf_token !== 'string' || !me.csrf_token) return false;
    this.credential = { kind: 'session', csrf: me.csrf_token };
    if (Number.isSafeInteger(me.policy_version)) this.policyVersion = me.policy_version;
    return true;
  }

  private async fetch(path: string, options: RequestOptions = {}, extras: RequestExtras = {}): Promise<Response> {
    const isForm = options.body instanceof FormData;
    const response = await this.send(path, {
      ...options,
      headers: { ...(isForm ? {} : { 'Content-Type': 'application/json' }), ...options.headers },
    }, extras);

    if (!response.ok) {
      if (path === '/admin/restart' && response.status === 403) {
        // Decode only this fixed refusal; arbitrary error details stay opaque.
        if (await readDetail(response) === 'Restart not allowed') {
          throw new Error("API process restart from this console is disabled for this installation. Use the installation's deployment manager to restart the service.");
        }
      }
      if (extras.manifestValidation && (response.status === 400 || response.status === 409)) {
        const detail = await readDetail(response);
        if (detail && safeManifestDetails.has(detail)) throw new Error(detail);
      }
      throw new AdminAPIError(response.status, response.statusText, await readDetail(response));
    }

    return response;
  }

  private async json<T>(path: string, options: RequestOptions = {}, extras: RequestExtras = {}): Promise<T> {
    const res = await this.fetch(path, options, extras);
    return res.json();
  }

  // Management mutations carry the last seen policy version and record the
  // new one from the reply.
  private async accessWrite<T extends object>(path: string, body: object, method: 'POST' | 'PATCH' = 'POST'): Promise<T & PolicyResult> {
    if (this.policyRefresh) await this.policyRefresh;
    let result: T & PolicyResult;
    try {
      result = await this.json<T & PolicyResult>(path, {
        method,
        body: JSON.stringify({ ...body, expected_policy_version: this.policyVersion ?? 0 }),
      });
    } catch (error) {
      // Someone else changed access meanwhile: read the current version once, so
      // saving again after a review is not refused for the same reason.
      if (error instanceof AdminAPIError && error.status === 409) await this.refreshPolicy();
      throw error;
    }
    if (Number.isSafeInteger(result?.policy_version)) this.policyVersion = result.policy_version;
    return result;
  }

  // Authentication
  static async login(login: string, password: string): Promise<{ ok: true; password_change_required: boolean }> {
    return new AdminAPIClient().json('/auth/login', { method: 'POST', body: JSON.stringify({ login, password }) }, { quiet401: true });
  }

  static async keyLogin(apiKey: string): Promise<{ ok: true; password_change_required: boolean }> {
    return new AdminAPIClient().json('/auth/key-login', { method: 'POST', body: JSON.stringify({ api_key: apiKey }) }, { quiet401: true });
  }

  async me(extras: { quiet401?: boolean } = {}): Promise<AuthMe> {
    const me = await this.json<AuthMe>('/auth/me', {}, extras);
    if (this.credential.kind === 'session') {
      this.credential = { kind: 'session', csrf: typeof me.csrf_token === 'string' ? me.csrf_token : null };
    }
    if (Number.isSafeInteger(me.policy_version)) this.policyVersion = me.policy_version;
    return me;
  }

  // Re-read the access policy version when someone starts a change (a dialog
  // opens), so only edits made by others while it is open are refused. A
  // write waits for a refresh that is still in flight; a failed refresh keeps
  // the last known version.
  refreshPolicy(): Promise<void> {
    if (!this.policyRefresh) {
      const pending: Promise<void> = this.me({ quiet401: true }).then(() => undefined, () => undefined).finally(() => {
        if (this.policyRefresh === pending) this.policyRefresh = null;
      });
      this.policyRefresh = pending;
    }
    return this.policyRefresh;
  }

  async context(): Promise<ConsoleContext> {
    return this.json('/auth/context');
  }

  async logout(): Promise<void> {
    if (this.credential.kind !== 'session') return;
    await this.fetch('/auth/logout', { method: 'POST', body: '{}' }, { quiet401: true });
  }

  async changePassword(currentPassword: string, password: string): Promise<{ ok: true; password_change_required: boolean }> {
    return this.json('/auth/password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, password }),
    }, { quiet401: true });
  }

  async enrollOwner(data: { login: string; display_name: string }): Promise<{ temporary_password: string; user: AccessUser } & PolicyResult> {
    return this.accessWrite('/auth/owner/enroll', data);
  }

  async listOwnSessions(): Promise<{ items: Array<Omit<AccessSession, 'principal'>> }> {
    return this.json('/auth/sessions?limit=100');
  }

  async revokeOwnSession(sessionId: string): Promise<{ session_id: string; changed: boolean } & PolicyResult> {
    return this.accessWrite(`/auth/sessions/${id(sessionId)}/revoke`, {});
  }

  // Access management (/access/*)
  async listPermissions(): Promise<{ items: PermissionInfo[] }> {
    return this.json('/access/permissions');
  }

  async listRoles(): Promise<Page<AccessRole>> {
    return this.json('/access/roles?limit=200');
  }

  async createRole(data: { name: string; description: string; permissions: string[]; enabled: boolean }) {
    return this.accessWrite<{ role?: AccessRole }>('/access/roles', data);
  }

  async updateRole(roleId: string, data: { name?: string; description?: string; permissions?: string[]; enabled?: boolean; version: number }) {
    return this.accessWrite<{ role?: AccessRole }>(`/access/roles/${id(roleId)}`, data, 'PATCH');
  }

  async listUsers(params: { kind?: 'user' | 'integration' | 'all'; q?: string; cursor?: string | null; limit?: number } = {}): Promise<Page<AccessUser>> {
    return this.json(`/access/users${query({ kind: params.kind, q: params.q, cursor: params.cursor, limit: params.limit ?? 50 })}`);
  }

  async getUser(userId: string): Promise<AccessUserDetail> {
    return this.json(`/access/users/${id(userId)}`);
  }

  async createUser(data: { login: string; display_name: string; enabled: boolean }) {
    return this.accessWrite<{ user: AccessUser; temporary_password: string }>('/access/users', data);
  }

  async createIntegration(data: { display_name: string; enabled: boolean }) {
    return this.accessWrite<{ integration: AccessUser }>('/access/integrations', data);
  }

  async updateUser(userId: string, data: { display_name?: string; enabled?: boolean; login?: string; version: number }) {
    return this.accessWrite<{ user?: AccessUser }>(`/access/users/${id(userId)}`, data, 'PATCH');
  }

  async resetPassword(userId: string, version: number) {
    return this.accessWrite<{ user: AccessUser; temporary_password: string }>(`/access/users/${id(userId)}/reset-password`, { version });
  }

  async listGroups(): Promise<Page<AccessGroup>> {
    return this.json('/access/groups?limit=200');
  }

  async getGroup(groupId: string): Promise<AccessGroupDetail> {
    return this.json(`/access/groups/${id(groupId)}`);
  }

  async createGroup(data: { name: string; description: string; enabled: boolean }) {
    return this.accessWrite<{ group?: AccessGroup }>('/access/groups', data);
  }

  async updateGroup(groupId: string, data: { name?: string; description?: string; enabled?: boolean; version: number }) {
    return this.accessWrite<{ group?: AccessGroup }>(`/access/groups/${id(groupId)}`, data, 'PATCH');
  }

  async addGroupMember(groupId: string, data: { principal_id: string; principal_version: number; group_version: number }) {
    return this.accessWrite<{ membership_id: string }>(`/access/groups/${id(groupId)}/members`, data);
  }

  async removeGroupMember(groupId: string, membershipId: string, data: { membership_version: number; group_version: number }) {
    return this.accessWrite<object>(`/access/groups/${id(groupId)}/members/${id(membershipId)}/remove`, data);
  }

  async listResources(kind?: ResourceKind): Promise<Page<AccessResource>> {
    return this.json(`/access/resources${query({ kind, limit: 200 })}`);
  }

  async listAssignments(params: { subject_id?: string; resource_id?: string } = {}): Promise<Page<AccessAssignment>> {
    return this.json(`/access/assignments${query({ ...params, limit: 200 })}`);
  }

  async createAssignment(data: { subject: { kind: 'principal' | 'group'; id: string; version: number }; role: { id: string; version: number }; resource_id: string }) {
    return this.accessWrite<{ assignment?: AccessAssignment }>('/access/assignments', data);
  }

  async removeAssignment(assignmentId: string, version: number) {
    return this.accessWrite<object>(`/access/assignments/${id(assignmentId)}/remove`, { version });
  }

  async listKeys(params: { principal_id?: string } = {}): Promise<Page<AccessKey>> {
    return this.json(`/access/keys${query({ ...params, limit: 200 })}`);
  }

  async createKey(data: { principal: { id: string; version: number }; name: string; note: string; expires_at: string | null; ceiling: KeyCeiling[] }) {
    return this.accessWrite<{ key: AccessKey; token: string }>('/access/keys', data);
  }

  async updateKey(keyId: string, data: { name?: string; note?: string; expires_at?: string | null; version: number }) {
    return this.accessWrite<{ key?: AccessKey }>(`/access/keys/${id(keyId)}`, data, 'PATCH');
  }

  async rotateKey(keyId: string, version: number) {
    return this.accessWrite<{ key: AccessKey; token: string }>(`/access/keys/${id(keyId)}/rotate`, { version });
  }

  async revokeKey(keyId: string, version: number) {
    return this.accessWrite<{ key?: AccessKey }>(`/access/keys/${id(keyId)}/revoke`, { version });
  }

  async approveKey(keyId: string, data: { principal: { id: string; version: number }; ceiling: KeyCeiling[]; version: number }) {
    return this.accessWrite<{ key?: AccessKey }>(`/access/keys/${id(keyId)}/approve`, data);
  }

  async listSessions(params: { principal_id?: string; cursor?: string | null; limit?: number } = {}): Promise<Page<AccessSession>> {
    return this.json(`/access/sessions${query({ principal_id: params.principal_id, cursor: params.cursor, limit: params.limit ?? 50 })}`);
  }

  async revokeSession(sessionId: string) {
    return this.accessWrite<object>(`/access/sessions/${id(sessionId)}/revoke`, {});
  }

  async listMailboxes(): Promise<Page<AccessMailbox>> {
    return this.json('/access/mailboxes?limit=200');
  }

  async createMailbox(data: { label: string; enabled: boolean }) {
    return this.accessWrite<{ mailbox?: AccessMailbox }>('/access/mailboxes', data);
  }

  async updateMailbox(mailboxId: string, data: { label?: string; enabled?: boolean; version: number }) {
    return this.accessWrite<{ mailbox?: AccessMailbox }>(`/access/mailboxes/${id(mailboxId)}`, data, 'PATCH');
  }

  async listInboundRules(): Promise<Page<InboundRule>> {
    return this.json('/access/inbound-rules?limit=200');
  }

  async createInboundRule(data: { to_number: string; mailbox_id: string }) {
    return this.accessWrite<{ rule?: InboundRule }>('/access/inbound-rules', data);
  }

  async updateInboundRule(ruleId: string, data: { to_number?: string; mailbox_id?: string; version: number }) {
    return this.accessWrite<{ rule?: InboundRule }>(`/access/inbound-rules/${id(ruleId)}`, data, 'PATCH');
  }

  // Terminal handshake: a short-lived, single-use ticket sent as the first
  // WebSocket message, never in the URL.
  async createTerminalTicket(): Promise<{ ticket: string; expires_at: string }> {
    return this.json('/admin/terminal/ticket', { method: 'POST', body: '{}' });
  }

  // Configuration
  async getConfig(): Promise<any> {
    return this.json('/admin/config');
  }

  async getSettings(): Promise<Settings> {
    return this.json('/admin/settings');
  }

  async validateSettings(settings: any): Promise<ValidationResult> {
    return this.json('/admin/settings/validate', { method: 'POST', body: JSON.stringify(settings) });
  }

  async exportSettings(): Promise<{ env: string }> {
    return this.json('/admin/settings/export');
  }

  async persistSettings(content?: string, path?: string): Promise<{ ok: boolean; path: string }> {
    return this.json('/admin/settings/persist', { method: 'POST', body: JSON.stringify({ content, path }) });
  }

  async updateSettings(settings: SettingsPatch): Promise<ConfigurationWriteResult> {
    const res = await this.fetch('/admin/settings', { method: 'PUT', body: JSON.stringify(settings) });
    return configurationResult(await res.json());
  }

  async reloadSettings(): Promise<Settings> {
    return this.json('/admin/settings/reload', { method: 'POST' });
  }

  async restart(): Promise<any> {
    return this.json('/admin/restart', { method: 'POST' });
  }

  // Whether the API answers its liveness check; false while it restarts or is unreachable.
  async isServing(): Promise<boolean> {
    try {
      const res = await this.send('/health', {}, { quiet401: true });
      return res.ok;
    } catch {
      return false;
    }
  }

  // SIP trunk for Faxbot's own fax engine
  async getSipPresets(): Promise<{ presets: SipPreset[] }> {
    return this.json('/admin/sip/presets');
  }

  async getSipStatus(): Promise<SipTrunkStatus> {
    return this.json('/admin/sip/status');
  }

  async applySipTrunk(): Promise<{ ok: true; message: string }> {
    return this.json('/admin/sip/apply', { method: 'POST' });
  }

  async listSipCalls(params: { cursor?: string | null; limit?: number; direction?: 'outbound' | 'inbound' } = {}): Promise<SipCallPage> {
    return this.json(`/admin/sip/calls${query(params)}`);
  }

  // Bring in faxes the SIP trunk received but could not hand to Faxbot.
  async recoverInbound(): Promise<{ found: number; imported: number; waiting: number; message: string }> {
    return this.json('/admin/inbound/recover', { method: 'POST', body: JSON.stringify({}) });
  }

  // Diagnostics
  async runDiagnostics(): Promise<DiagnosticsResult> {
    return this.json('/admin/diagnostics/run', { method: 'POST' });
  }

  async getHealthStatus(): Promise<HealthStatus> {
    return this.json('/admin/health-status');
  }

  // The fax engine sentence from public readiness (it answers 503 while not ready), or null.
  async getFaxEngineMessage(): Promise<string | null> {
    const res = await this.send('/health/ready', {}, { quiet401: true });
    if (res.status !== 200 && res.status !== 503) return null;
    const body = await res.json().catch(() => null);
    return typeof body?.message === 'string' && FAX_ENGINE_SENTENCES.has(body.message) ? body.message : null;
  }

  // MCP
  async getMcpConfig(): Promise<any> {
    return this.json('/admin/config');
  }

  // The embedded MCP server answers <mount>/health on the API origin without a
  // credential. Only its own reply counts; a page fallback or other 200 does not.
  async getMcpHealth(path: string = '/mcp/sse/health'): Promise<{ status: 'ok'; server: 'faxbot-mcp'; transport?: string }> {
    const res = await fetch(`${this.baseURL}${path}`, { cache: 'no-store', credentials: 'omit' });
    if (!res.ok) throw new Error(`MCP not healthy (${res.status})`);
    const body = await res.json().catch(() => null);
    if (!body || typeof body !== 'object' || body.status !== 'ok' || body.server !== 'faxbot-mcp') {
      throw new Error('MCP health reply was not recognized');
    }
    return body;
  }

  // Logs
  async getLogs(params: { q?: string; event?: string; since?: string; limit?: number } = {}): Promise<{ items: any[]; count: number }> {
    return this.json(`/admin/logs${query(params)}`);
  }

  async tailLogs(params: { q?: string; event?: string; lines?: number } = {}): Promise<{ items: any[]; count: number; source?: string }> {
    return this.json(`/admin/logs/tail${query(params)}`);
  }

  // Jobs
  async listJobs(params: { status?: string; backend?: string; limit?: number; offset?: number } = {}): Promise<{ total: number; jobs: FaxJob[] }> {
    return this.json(`/admin/fax-jobs${query(params)}`);
  }

  async getJob(jobId: string): Promise<FaxJob> {
    return this.json(`/admin/fax-jobs/${id(jobId)}`);
  }

  private async deliveryRequest(jobId: string, confirmation?: ProviderIdentityConfirmation): Promise<OperatorDelivery> {
    const attaching = confirmation !== undefined;
    const res = await this.send(`/admin/fax-jobs/${id(jobId)}/${attaching ? 'reconcile' : 'delivery'}`, {
      method: attaching ? 'POST' : 'GET',
      headers: { 'Content-Type': 'application/json' },
      ...(attaching ? { body: JSON.stringify(confirmation) } : {}),
    });
    if (!res.ok) {
      if (res.status === 400 || res.status === 409) {
        const detail = await readDetail(res);
        if (detail && attaching && res.status === 409 && safeReconciliationDetails.has(detail)) {
          throw new Error(safeReconciliationDetails.get(detail));
        }
        if (detail && attaching && res.status === 400 && safeReconciliationInputDetails.has(detail)) {
          throw new Error(safeReconciliationInputDetails.get(detail));
        }
        if (!attaching && res.status === 409 && detail === 'Delivery history is unavailable; reload the job before continuing.') {
          throw new Error(detail);
        }
      }
      throw new Error(`${attaching ? 'Provider identity attachment' : 'Delivery history request'} failed (HTTP ${res.status}). Reload delivery before continuing.`);
    }
    return res.json();
  }

  async getDelivery(jobId: string): Promise<OperatorDelivery> {
    return this.deliveryRequest(jobId);
  }

  async attachProviderIdentity(jobId: string, confirmation: ProviderIdentityConfirmation): Promise<OperatorDelivery> {
    return this.deliveryRequest(jobId, confirmation);
  }

  async downloadJobPdf(jobId: string): Promise<Blob> {
    const res = await this.send(`/admin/fax-jobs/${id(jobId)}/pdf`);
    if (!res.ok) throw new Error(`Download failed: ${res.status}`);
    return res.blob();
  }

  // Inbound
  async listInbound(): Promise<InboundFax[]> {
    return this.json('/inbound');
  }

  // Ask Faxbot to fetch a received fax's document again now.
  async fetchInboundAgain(inboundId: string): Promise<InboundFax> {
    return this.json(`/inbound/${id(inboundId)}/fetch`, { method: 'POST', body: '{}' });
  }

  async downloadInboundPdf(inboundId: string): Promise<Blob> {
    const res = await this.send(`/inbound/${id(inboundId)}/pdf`);
    if (!res.ok) throw new Error(`Download failed: ${res.status}`);
    return res.blob();
  }

  // Inbound helpers
  async getInboundCallbacks(): Promise<any> {
    return this.json('/admin/inbound/callbacks');
  }

  async simulateInbound(opts: { backend?: string; fr?: string; to?: string; pages?: number; status?: string } = {}): Promise<{ id: string; status: string }> {
    return this.json('/admin/inbound/simulate', { method: 'POST', body: JSON.stringify(opts) });
  }

  // Admin actions (container exec — allowlisted)
  async listActions(): Promise<{ enabled: boolean; items: Array<{ id: string; label: string; backend?: string[] }> }> {
    return this.json('/admin/actions');
  }

  async runAction(actionId: string): Promise<{ ok: boolean; id: string; code?: number; stdout?: string; stderr?: string }> {
    return this.json('/admin/actions/run', { method: 'POST', body: JSON.stringify({ id: actionId }) });
  }

  // Tunnel
  async getTunnelStatus(): Promise<any> {
    return this.json('/admin/tunnel/status');
  }

  async setTunnelConfig(payload: any): Promise<any> {
    return this.json('/admin/tunnel/config', { method: 'POST', body: JSON.stringify(payload || {}) });
  }

  async testTunnel(): Promise<{ ok: boolean; message?: string; target?: string }> {
    return this.json('/admin/tunnel/test', { method: 'POST' });
  }

  async createTunnelPairing(): Promise<{ code: string; expires_at: string }> {
    const result = await this.json<{ code: string; expires_at: string }>('/admin/tunnel/pair', { method: 'POST', body: '{}' });
    // Issuing a code changes access policy; keep later edits current.
    void this.refreshPolicy();
    return result;
  }

  async sendFax(to: string, file: File, options: { queueOnly?: boolean; idempotencyKey?: string } = {}): Promise<FaxSendResult> {
    const formData = new FormData();
    formData.append('to', normalizeFaxDestination(to));
    formData.append('file', file);
    if (options.queueOnly) formData.append('queue_only', 'true');

    const res = await this.send('/fax', {
      method: 'POST',
      headers: options.idempotencyKey ? { 'Idempotency-Key': options.idempotencyKey } : {},
      body: formData,
    });

    if (!res.ok) {
      const detail = await readDetail(res);
      if (res.status === 409) {
        if (detail === 'Queue-only request refused because outbound sending is now enabled. Refresh Send before submitting again.') {
          throw new Error('Queue-only submission was refused because active settings changed. Leave and reopen Send to review the current delivery mode before trying again.');
        }
        if (detail === 'Idempotency-Key already belongs to a different fax request.'
            || detail === 'Accepted fax record is unavailable; reconcile before submitting another request.') {
          throw new Error(detail);
        }
      }
      if (res.status === 400) {
        const sentence = plainRefusal(new AdminAPIError(res.status, res.statusText, detail));
        throw new FaxRefusedError(sentence ?? 'The fax was not accepted; check the number and the document, then try again.');
      }
      if (res.status === 503 && typeof detail === 'string') {
        // Refused before acceptance because the fax engine is not connected; nothing was sent.
        if (FAX_ENGINE_SENTENCES.has(detail)) throw new FaxRefusedError(detail);
        const uncertain = /^Fax acceptance is uncertain\. Retain job ([a-f0-9]{32}) for reconciliation\.$/.exec(detail);
        if (uncertain) {
          throw new Error(`Acceptance is uncertain. Check job ${uncertain[1]} in Jobs before starting another request.`);
        }
      }
      throw new Error(`Fax acceptance was not confirmed (HTTP ${res.status}). Check Jobs before starting another request.`);
    }

    return res.json();
  }

  // v3 Plugins (feature-gated)
  async listPlugins(): Promise<{ items: any[] }> {
    return this.json('/plugins');
  }

  async getPluginConfig(pluginId: string, role?: PluginRole): Promise<PluginConfiguration> {
    return this.json(`/plugins/${id(pluginId)}/config${query({ role })}`);
  }

  async updatePluginConfig(pluginId: string, payload: PluginConfigurationPatch): Promise<ConfigurationWriteResult> {
    const res = await this.fetch(`/plugins/${id(pluginId)}/config`, { method: 'PUT', body: JSON.stringify(payload || {}) });
    return configurationResult(await res.json());
  }

  async getPluginRegistry(): Promise<{ items: any[] }> {
    return this.json('/plugin-registry');
  }

  // Manifest providers
  async validateHttpManifest(payload: { manifest: any; credentials?: any; settings?: any; to?: string; file_url?: string; from_number?: string; render_only?: boolean }): Promise<any> {
    return this.json('/admin/plugins/http/validate', { method: 'POST', body: JSON.stringify(payload || {}) }, { manifestValidation: true });
  }

  async installHttpManifest(payload: { manifest: any }): Promise<{ ok: boolean; id: string; path: string }> {
    return this.json('/admin/plugins/http/install', { method: 'POST', body: JSON.stringify(payload || {}) }, { manifestValidation: true });
  }

  // Jobs admin helpers
  async refreshJob(jobId: string): Promise<FaxSendResult> {
    const res = await this.send(`/admin/fax-jobs/${id(jobId)}/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    if (!res.ok) {
      if ([400, 409, 502].includes(res.status)) {
        const detail = await readDetail(res);
        if (detail && safeRefreshDetails.has(detail)) throw new Error(detail);
      }
      throw new Error(`API Error: ${res.status} ${res.statusText}`);
    }
    return res.json();
  }

  async importHttpManifests(payload: { items?: any[]; markdown?: string; source?: 'repo_scrape' }): Promise<{ ok: boolean; imported: any[]; errors: Array<{ error: string }> }> {
    const result = await this.json<any>('/admin/plugins/http/import-manifests', { method: 'POST', body: JSON.stringify(payload || {}) }, { manifestValidation: true });
    return {
      ...result,
      errors: (Array.isArray(result.errors) ? result.errors : []).map((failure: any) => ({
        error: safeManifestDetails.has(failure?.error) ? failure.error : 'Manifest could not be imported.',
      })),
    };
  }

  // Polling helper. Stops for good once the server says the credential is
  // gone or the operation is not permitted.
  startPolling(onUpdate: (data: HealthStatus) => void, intervalMs: number = 5000): () => void {
    let running = true;
    let timer: number | undefined;

    const poll = async () => {
      if (!running) return;
      try {
        onUpdate(await this.getHealthStatus());
      } catch (e) {
        if (e instanceof AdminAPIError && (e.status === 401 || e.status === 403)) {
          running = false;
          return;
        }
        console.error('Polling error:', e);
      }
      if (running) timer = window.setTimeout(poll, intervalMs);
    };

    void poll();
    return () => {
      running = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }

  // Delivery routes, intake and direct delivery
  async listDestinations(): Promise<{ window_days: number; destinations: Destination[] }> {
    return this.json('/routing/destinations');
  }

  async getDestination(number: string): Promise<DestinationDetail> {
    return this.json(`/routing/destinations/${id(number)}`);
  }

  async updateDestination(number: string, patch: DestinationPatch): Promise<Destination> {
    return this.json(`/routing/destinations/${id(number)}`, { method: 'PATCH', body: JSON.stringify(patch) });
  }

  async getRouteCosts(): Promise<{ since: string; providers: ProviderCosts[] }> {
    return this.json('/routing/costs');
  }

  async listRateCards(): Promise<{ cards: RateCard[] }> {
    return this.json('/routing/rate-cards');
  }

  async saveRateCards(cards: RateCard[]): Promise<{ cards: RateCard[] }> {
    const body = cards.map(({ id: _id, ...card }) => card);
    return this.json('/routing/rate-cards', { method: 'PUT', body: JSON.stringify({ cards: body }) });
  }

  async listIntakeItems(params: { limit?: number } = {}): Promise<{ items: IntakeItem[]; counts: IntakeCounts }> {
    return this.json(`/intake/items${query(params)}`);
  }

  async retryIntakeItem(itemId: string): Promise<IntakeItem> {
    return this.json(`/intake/items/${id(itemId)}/retry`, { method: 'POST', body: '{}' });
  }

  async listEmailConnectors(): Promise<{ connectors: EmailConnector[] }> {
    return this.json('/intake/connectors');
  }

  async createEmailConnector(input: EmailConnectorInput): Promise<EmailConnector> {
    return this.json('/intake/connectors', { method: 'POST', body: JSON.stringify(input) });
  }

  async updateEmailConnector(connectorId: string, input: EmailConnectorInput): Promise<EmailConnector> {
    return this.json(`/intake/connectors/${id(connectorId)}`, { method: 'PUT', body: JSON.stringify(input) });
  }

  async deleteEmailConnector(connectorId: string): Promise<{ deleted: boolean }> {
    return this.json(`/intake/connectors/${id(connectorId)}`, { method: 'DELETE' });
  }

  async testEmailConnector(connectorId: string): Promise<{ ok: boolean; detail: string }> {
    return this.json(`/intake/connectors/${id(connectorId)}/test`, { method: 'POST', body: '{}' });
  }

  async getDirectCard(): Promise<{ card: DirectCard }> {
    return this.json('/direct/card');
  }

  async listDirectPartners(): Promise<{ peers: DirectPartner[] }> {
    return this.json('/direct/peers');
  }

  async addDirectPartner(card: string): Promise<DirectPartner> {
    return this.json('/direct/peers', { method: 'POST', body: JSON.stringify({ card }) });
  }

  async sendDirectCode(partnerId: string): Promise<DirectPartner & { fax_id: string }> {
    return this.json(`/direct/peers/${id(partnerId)}/challenge`, { method: 'POST', body: '{}' });
  }

  async confirmDirectCode(partnerId: string, code: string): Promise<{ confirmed: boolean; detail: string }> {
    return this.json(`/direct/peers/${id(partnerId)}/confirm`, { method: 'POST', body: JSON.stringify({ code }) });
  }

  async listDirectDeliveries(): Promise<{ deliveries: DirectDeliveryRecord[] }> {
    return this.json('/direct/deliveries');
  }

  async removeDirectPartner(partnerId: string): Promise<DirectPartner> {
    return this.json(`/direct/peers/${id(partnerId)}/revoke`, { method: 'POST', body: '{}' });
  }

  // Work queue
  async listWork(params: { view?: WorkView; mailbox?: string; limit?: number } = {}): Promise<{ items: WorkItem[] }> {
    return this.json(`/work${query(params)}`);
  }

  async workCounts(): Promise<WorkCounts> {
    return this.json('/work/counts');
  }

  async getWork(itemId: string): Promise<WorkItem> {
    return this.json(`/work/${id(itemId)}`);
  }

  async workHistory(itemId: string): Promise<{ events: WorkEvent[] }> {
    return this.json(`/work/${id(itemId)}/history`);
  }

  async workAssignees(itemId: string): Promise<{ people: WorkAssignee[] }> {
    return this.json(`/work/${id(itemId)}/assignees`);
  }

  async assignWork(itemId: string, principalId: string, version: number): Promise<WorkItem> {
    return this.json(`/work/${id(itemId)}/assign`, { method: 'POST', body: JSON.stringify({ principal_id: principalId, version }) });
  }

  async acknowledgeWork(itemId: string, version: number): Promise<WorkItem> {
    return this.json(`/work/${id(itemId)}/acknowledge`, { method: 'POST', body: JSON.stringify({ version }) });
  }

  async completeWork(itemId: string, note: string, version: number): Promise<WorkItem> {
    return this.json(`/work/${id(itemId)}/done`, { method: 'POST', body: JSON.stringify({ note, version }) });
  }

  async reopenWork(itemId: string, version: number): Promise<WorkItem> {
    return this.json(`/work/${id(itemId)}/reopen`, { method: 'POST', body: JSON.stringify({ version }) });
  }

  async exportWork(itemId: string): Promise<Blob> {
    const res = await this.fetch(`/work/${id(itemId)}/export`);
    return res.blob();
  }

  async getWorkSettings(): Promise<WorkSettings> {
    return this.json('/work/settings');
  }

  async saveWorkMailbox(entry: { mailbox_id: string; acknowledge_hours: number | null; backup_principal_id: string | null; version: number }): Promise<WorkSettings> {
    return this.json('/work/settings', { method: 'PUT', body: JSON.stringify({ mailboxes: [entry] }) });
  }

  async importDocument(file: File, manifest: ImportManifest): Promise<ImportResult> {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('manifest', JSON.stringify(manifest));
    return this.json('/imports', { method: 'POST', body: formData });
  }
}

export default AdminAPIClient;
