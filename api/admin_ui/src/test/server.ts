// In-memory implementation of the management HTTP contract (/auth/*,
// /access/*) for component tests. Request bodies are strict like the server:
// unknown or missing fields return 422, a stale expected_policy_version or
// entity version returns 409, and cookie-session writes need X-CSRF-Token.
import { http, HttpResponse, type JsonBodyType } from 'msw';
import { setupServer } from 'msw/node';

type Json = Record<string, any>;

const CSRF_DETAIL = 'Browser request verification failed. Refresh your session and try again.';
const AUTH_DETAIL = 'Authentication required or credentials no longer valid.';
const CONFLICT_DETAIL = 'Access policy changed. Reload and try again.';
export const NUMBER_DETAIL = 'Enter the full fax number with its area code, or with its country code starting with +.';

export const ALL_PERMISSIONS: Array<[string, string]> = [
  ['fax:send', 'fax'], ['fax:read', 'fax'], ['fax:document', 'fax'], ['fax:refresh', 'fax'], ['fax:reconcile', 'fax'],
  ['inbound:list', 'inbound'], ['inbound:read', 'inbound'], ['inbound:document', 'inbound'],
  ['keys:manage', 'identity'], ['users:read', 'identity'], ['users:manage', 'identity'], ['groups:read', 'identity'],
  ['groups:manage', 'identity'], ['roles:read', 'identity'], ['roles:manage', 'identity'], ['grants:read', 'identity'],
  ['grants:manage', 'identity'], ['sessions:read', 'identity'], ['sessions:revoke', 'identity'],
  ['settings:read', 'config'], ['settings:write', 'config'], ['providers:read', 'config'], ['providers:write', 'config'],
  ['providers:install', 'config'], ['diagnostics:read', 'host'], ['logs:read', 'audit'], ['audit:read', 'audit'],
  ['tunnels:pair', 'host'], ['host:restart', 'host'],
  ['host:terminal', 'host'], ['owner:recover', 'identity'], ['mailboxes:read', 'mailbox'],
  ['mailboxes:manage', 'mailbox'],
];

const ADMIN_PERMISSIONS = ALL_PERMISSIONS.map(([permission]) => permission)
  .filter((permission) => !['host:restart', 'host:terminal', 'owner:recover', 'diagnostics:read', 'settings:read'].includes(permission));

interface Principal {
  id: string;
  kind: 'user' | 'integration' | 'bootstrap';
  login: string | null;
  display_name: string;
  enabled: boolean;
  password: string | null;
  password_change_required: boolean | null;
  created_at: string;
  last_login_at: string | null;
  version: number;
  permissions: string[];
}

interface Key {
  id: string;
  token: string;
  principal_id: string;
  name: string | null;
  note: string | null;
  expires_at: string | null;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  pending_review: boolean;
  ceiling: Array<{ permission: string; resource_id: string }>;
  version: number;
}

interface Group { id: string; name: string; description: string; enabled: boolean; version: number }
interface Membership { id: string; group_id: string; principal_id: string; version: number }
interface Role { id: string; name: string; description: string; builtin: boolean; enabled: boolean; permissions: string[]; version: number }
interface Mailbox { id: string; label: string; enabled: boolean; resource_id: string; version: number }
interface Rule { id: string; to_number: string; mailbox_id: string; version: number; [option: string]: unknown }

// A number rule's receiving options (design §4.9), each optional on create and change.
const RECEIVING_OPTIONS = ['position', 'enabled', 'any_number', 'account_key', 'site_key', 'subaddress', 'from_numbers',
  'days', 'start_minute', 'end_minute', 'email_connector_id', 'email_off', 'urgent', 'keep_days'];
const optionsIn = (body: Json) => Object.fromEntries(RECEIVING_OPTIONS.filter((key) => key in body).map((key) => [key, body[key]]));
interface Assignment { id: string; subject: { kind: 'principal' | 'group'; id: string }; role_id: string; resource_id: string; version: number }

interface Captured { method: string; path: string; body: Json | null; headers: Record<string, string> }

const now = () => new Date().toISOString().replace(/Z$/, '');

function createState() {
  const created = '2026-10-01T12:00:00';
  const principals = new Map<string, Principal>([
    ['p_admin', { id: 'p_admin', kind: 'user', login: 'admin', display_name: 'Ada Admin', enabled: true, password: 'correct horse',
      password_change_required: false, created_at: created, last_login_at: null, version: 1, permissions: ADMIN_PERMISSIONS }],
    ['p_new', { id: 'p_new', kind: 'user', login: 'newbie', display_name: 'Nia New', enabled: true, password: 'temporary-1',
      password_change_required: true, created_at: created, last_login_at: null, version: 1, permissions: ['fax:send', 'fax:read'] }],
    ['p_scanner', { id: 'p_scanner', kind: 'integration', login: null, display_name: 'Front desk scanner', enabled: true, password: null,
      password_change_required: null, created_at: created, last_login_at: null, version: 3, permissions: ['fax:send', 'fax:read'] }],
    ['p_bootstrap', { id: 'p_bootstrap', kind: 'bootstrap', login: null, display_name: 'Installation key', enabled: true, password: null,
      password_change_required: null, created_at: created, last_login_at: null, version: 1, permissions: ALL_PERMISSIONS.map(([p]) => p) }],
  ]);
  const keys = new Map<string, Key>([
    ['key_scan', { id: 'key_scan', token: 'fbk_live_scan_original', principal_id: 'p_scanner', name: 'Scanner', note: '',
      expires_at: null, created_at: created, last_used_at: null, revoked_at: null, pending_review: false,
      ceiling: [{ permission: 'fax:send', resource_id: 'res_installation' }, { permission: 'fax:read', resource_id: 'res_installation' }], version: 2 }],
    ['key_old', { id: 'key_old', token: 'fbk_live_old', principal_id: 'p_scanner', name: 'Old scanner', note: '',
      expires_at: null, created_at: created, last_used_at: null, revoked_at: '2026-10-02T09:00:00', pending_review: false,
      ceiling: [], version: 4 }],
    // A migrated wildcard key: no name or note, waiting for review.
    ['key_legacy', { id: 'key_legacy', token: 'fbk_live_legacy', principal_id: 'p_scanner', name: null, note: null,
      expires_at: null, created_at: created, last_used_at: null, revoked_at: null, pending_review: true,
      ceiling: [], version: 1 }],
  ]);
  const roles = new Map<string, Role>([
    ['role_owner', { id: 'role_owner', name: 'Owner', description: 'Everything', builtin: true, enabled: true, permissions: ALL_PERMISSIONS.map(([p]) => p), version: 1 }],
    ['role_fax_operator', { id: 'role_fax_operator', name: 'Fax Operator', description: 'Send and read faxes', builtin: true, enabled: true,
      permissions: ['fax:send', 'fax:read', 'fax:document', 'fax:refresh', 'inbound:list', 'inbound:read', 'inbound:document'], version: 1 }],
  ]);
  const groups = new Map<string, Group>([
    ['grp_front', { id: 'grp_front', name: 'Front office', description: 'Reception staff', enabled: true, version: 1 }],
  ]);
  const memberships = new Map<string, Membership>();
  const mailboxes = new Map<string, Mailbox>([
    ['mbx_main', { id: 'mbx_main', label: 'Main line', enabled: true, resource_id: 'res_mbx_main', version: 1 }],
  ]);
  const rules = new Map<string, Rule>();
  const assignments = new Map<string, Assignment>();
  return {
    policyVersion: 7,
    csrf: 'csrf-1',
    session: null as null | { principalId: string },
    keyLoginStatus: null as null | { status: number; detail: string },
    principals, keys, roles, groups, memberships, mailboxes, rules, assignments,
    sequence: 100,
    requests: [] as Captured[],
    // Installation country and how fax numbers typed there are saved. A test
    // sets resolveNumber to stand in for the server's reading of a number;
    // null refuses it with the server's sentence.
    country: 'US',
    numberExample: '(201) 555-0123',
    resolveNumber: ((value: string) => value) as (value: string) => string | null,
    // The provider hints in /auth/context, and whether sign-in still needs a first owner.
    providerView: null as null | { plugins_enabled: boolean; install_enabled: boolean; active_outbound: string; active_inbound: string;
      extra_routes?: string[]; trunk_preset?: string },
    // Names the installation gives its providers, such as the trunk's carrier.
    providerNames: {} as Record<string, string>,
    firstOwner: false,
  };
}

type State = ReturnType<typeof createState>;

export const backend = {
  state: createState() as State,
  reset() { this.state = createState(); },
  // Simulate another administrator changing access policy.
  bumpPolicy() { this.state.policyVersion += 1; },
  requestsTo(method: string, path: string) {
    return this.state.requests.filter((r) => r.method === method && r.path === path);
  },
};

const s = () => backend.state;
const nextId = (prefix: string) => `${prefix}_${++s().sequence}`;
const json = (body: JsonBodyType, status = 200) => HttpResponse.json(body, { status });

// GET /routing/recommendations/fax-marker for a new installation: no calls yet.
export function newFaxMarkerAdvice() {
  const side = { calls: 0, delivered: 0, failed: 0, result_unknown: 0, delivered_percent: null, t38: 0, audio: 0,
    mode_unknown: 0, t38_percent: null, average_seconds: null, seconds_per_page: null, cost_per_delivered: null,
    cost_text: 'None delivered', settled: 0, reported: 0, estimated: 0, unpriced: 0 };
  return { days: 90, min_calls: 10, state: 'no_calls', enough: false, setting_on: true, difference: null, caveat: null,
    sentence: 'Faxbot placed no calls over your carrier line in the last 90 days, so there is nothing to compare yet.',
    setting_sentence: 'Mark calls as fax is on, and Faxbot leaves it on: this comparison never changes a setting.',
    left_out: 0, left_out_sentence: null, marked: side, not_marked: side };
}

// GET /routing/recommendations/receiving for a new installation: too little call history to advise.
export function newReceivingAdvice() {
  const window = (start: string, end: string) => ({ start, end, days: 30 });
  return {
    days: 30, estimate: true, carrier: 'Telnyx',
    sentence: 'Faxbot needs 60 days of call history to advise on shared lines; it has none yet.',
    windows: { choose: window('2026-08-06T00:00:00', '2026-09-05T00:00:00'),
      check: window('2026-09-05T00:00:00', '2026-10-05T00:00:00') },
    history: { enough: false, first_call_at: null, days: 0 },
    pool: { state: 'too_little_history', numbers: [],
      sentence: 'Faxbot needs 60 days of call history to advise on shared lines; it has none yet.' },
    quiet_numbers: { state: 'too_little_history', numbers: [], monthly_total: [],
      sentence: 'Faxbot needs 30 days of call history to tell which numbers are quiet; it has none yet.' },
    connections: { sentence: 'Telnyx is your only fax service, so there is no second monthly fee to save.',
      items: [{ name: 'Telnyx', kind: 'trunk', monthly_fee: [{ currency: 'USD', amount: '0.00' }] }] },
    prices: [{ label: 'Telnyx inbound channel, US', read_on: '2026-10-05', source_url: 'https://telnyx.com/pricing/elastic-sip',
      text: '$12.00 a month each for the first 10, $11.00 for the next 40, $9.00 for the next 200 and $8.00 after 250' }],
  };
}

// GET /routing/savings for an installation that has saved nothing yet.
export function emptySavings() {
  const part = (sentence: string) => ({ estimate: true, saved: [], sentence });
  return {
    days: 30, since: '2026-09-04T00:00:00', estimate: true, total_saved: [],
    total_sentence: 'No money saved in the last 30 days, as far as Faxbot can tell.',
    sentence: 'Each figure is an estimate: what you paid compared with what the same faxes would have cost the usual way.',
    sending_together: { ...part('No faxes were sent together in the last 30 days.'),
      numbers: 0, calls: 0, faxes: 0, calls_saved: 0, priced_calls: 0 },
    direct_delivery: { ...part('No documents went straight to a partner in the last 30 days.'),
      faxes: 0, calls_avoided: 0, pages: 0, priced: 0, in_plan: 0, unpriced: 0 },
    direct_fax_images: { ...part('No fax went to a partner as a fax image in the last 30 days.'),
      faxes: 0, calls_avoided: 0, pages: 0, priced: 0, in_plan: 0, unpriced: 0 },
    case_packets: { ...part('No case packet in the last 30 days left out a document the recipient already had.'),
      counted_from: null, earlier_not_counted: false, counted_from_sentence: null, packets: 0, documents_left_out: 0,
      pages_not_resent: 0, pages_saved: 0, priced: 0, in_plan: 0, unpriced: 0 },
    own_numbers: { ...part('No faxes went to your own numbers in the last 30 days.'),
      faxes: 0, calls_avoided: 0, pages: 0, priced: 0, in_plan: 0, unpriced: 0 },
  };
}
const fail = (status: number, detail: string) => json({ detail }, status);

async function capture(request: Request, path: string): Promise<Json | null> {
  let body: Json | null = null;
  const text = await request.clone().text();
  if (text) {
    try { body = JSON.parse(text); } catch { body = null; }
  }
  const headers: Record<string, string> = {};
  request.headers.forEach((value, key) => { headers[key] = value; });
  s().requests.push({ method: request.method, path, body, headers });
  return body;
}

function strict(body: Json | null, required: string[], optional: string[] = []): Response | null {
  if (!body || typeof body !== 'object') return fail(422, 'Invalid access request.');
  const allowed = new Set([...required, ...optional]);
  if (Object.keys(body).some((key) => !allowed.has(key))) return fail(422, 'Invalid access request.');
  if (required.some((key) => !(key in body))) return fail(422, 'Invalid access request.');
  return null;
}

type Actor = { principal: Principal; source: 'key' | 'session' };

function authenticate(request: Request): Actor | Response {
  const key = request.headers.get('x-api-key');
  if (key !== null) {
    if (key === 'bootstrap-secret') return { principal: s().principals.get('p_bootstrap')!, source: 'key' };
    const found = [...s().keys.values()].find((k) => k.token === key && !k.revoked_at);
    const principal = found && s().principals.get(found.principal_id);
    return principal && principal.enabled ? { principal, source: 'key' } : fail(401, AUTH_DETAIL);
  }
  const session = s().session;
  if (!session) return fail(401, AUTH_DETAIL);
  const principal = s().principals.get(session.principalId);
  if (!principal || !principal.enabled) return fail(401, AUTH_DETAIL);
  if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method) && request.headers.get('x-csrf-token') !== s().csrf) {
    return fail(403, CSRF_DETAIL);
  }
  return { principal, source: 'session' };
}

function stale(body: Json, ...versions: Array<[unknown, number]>): Response | null {
  if (body.expected_policy_version !== s().policyVersion) return fail(409, CONFLICT_DETAIL);
  if (versions.some(([given, current]) => given !== current)) return fail(409, CONFLICT_DETAIL);
  return null;
}

const committed = (extra: Json = {}) => {
  s().policyVersion += 1;
  return json({ ...extra, policy_version: s().policyVersion });
};

function userView(p: Principal) {
  const { password: _password, permissions: _permissions, ...rest } = p;
  return rest;
}

function keyView(k: Key) {
  const p = s().principals.get(k.principal_id)!;
  const { token: _token, principal_id: _pid, ...rest } = k;
  return { ...rest, principal: { id: p.id, display_name: p.display_name, kind: p.kind } };
}

function assignmentView(a: Assignment) {
  const subjectName = a.subject.kind === 'group' ? s().groups.get(a.subject.id)?.name : s().principals.get(a.subject.id)?.display_name;
  const role = s().roles.get(a.role_id)!;
  const resource = resources().find((r) => r.id === a.resource_id)!;
  return {
    id: a.id,
    subject: { ...a.subject, name: subjectName ?? '' },
    role: { id: role.id, name: role.name, builtin: role.builtin },
    resource: { id: resource.id, kind: resource.kind, name: resource.name },
    version: a.version,
  };
}

function resources() {
  return [
    { id: 'res_installation', kind: 'installation', name: 'Installation', parent_id: null, mailbox_id: null, principal_id: null },
    { id: 'res_legacy', kind: 'legacy', name: 'Earlier faxes', parent_id: 'res_installation', mailbox_id: null, principal_id: null },
    ...[...s().mailboxes.values()].map((m) => ({ id: m.resource_id, kind: 'mailbox', name: m.label, parent_id: 'res_installation', mailbox_id: m.id, principal_id: null })),
    ...[...s().principals.values()].filter((p) => p.kind !== 'bootstrap').map((p) => ({
      id: `res_personal_${p.id}`, kind: 'personal', name: p.display_name, parent_id: 'res_installation', mailbox_id: null, principal_id: p.id })),
  ];
}

const page = (items: unknown[]) => json({ items, next_cursor: null });

function meView(actor: Actor) {
  const p = actor.principal;
  return {
    principal: { id: p.id, kind: p.kind, display_name: p.display_name, version: p.version },
    source: actor.source,
    password_change_required: Boolean(p.password_change_required),
    policy_version: s().policyVersion,
    permissions: p.password_change_required ? [] : [...p.permissions].sort(),
    session: actor.source === 'session' ? { id: 'sess_current', source_kind: p.kind === 'bootstrap' ? 'bootstrap' : 'password', expires_at: '2026-10-03T23:00:00' } : null,
    can_enroll_owner: p.kind === 'bootstrap',
    is_owner: false,
    grantable: { installation: [...p.permissions].sort() },
    ...(actor.source === 'session' ? { csrf_token: s().csrf } : {}),
  };
}

// Wraps a handler: records the request, authenticates, then runs it.
function guarded(method: 'get' | 'post' | 'patch', path: string,
  run: (ctx: { actor: Actor; body: Json; params: Record<string, string>; url: URL }) => Response | Promise<Response>) {
  return http[method](path, async ({ request, params }) => {
    const url = new URL(request.url);
    const body = await capture(request, url.pathname);
    const actor = authenticate(request);
    if (actor instanceof Response) return actor;
    return run({ actor, body: body ?? {}, params: params as Record<string, string>, url });
  });
}

const accessHandlers = [
  http.post('/auth/login', async ({ request }) => {
    const body = await capture(request, '/auth/login');
    const invalid = strict(body, ['login', 'password']);
    if (invalid) return invalid;
    const user = [...s().principals.values()].find((p) => p.login === body!.login);
    if (!user || user.password !== body!.password || !user.enabled) return fail(401, AUTH_DETAIL);
    s().session = { principalId: user.id };
    return json({ ok: true, password_change_required: Boolean(user.password_change_required) });
  }),
  http.post('/auth/key-login', async ({ request }) => {
    const body = await capture(request, '/auth/key-login');
    const invalid = strict(body, ['api_key']);
    if (invalid) return invalid;
    if (s().keyLoginStatus) return fail(s().keyLoginStatus!.status, s().keyLoginStatus!.detail);
    if (body!.api_key === 'bootstrap-secret') {
      s().session = { principalId: 'p_bootstrap' };
      return json({ ok: true, password_change_required: false });
    }
    const found = [...s().keys.values()].find((k) => k.token === body!.api_key && !k.revoked_at);
    if (!found) return fail(401, AUTH_DETAIL);
    s().session = { principalId: found.principal_id };
    return json({ ok: true, password_change_required: false });
  }),
  guarded('get', '/auth/me', ({ actor }) => json(meView(actor))),
  guarded('get', '/auth/context', ({ actor }) => json({
    policy_version: s().policyVersion,
    active_revision_id: 'rev_1',
    generation: 1,
    permissions: actor.principal.permissions,
    navigation: { jobs: true, inbox: true, send: actor.principal.permissions.includes('fax:send') },
    send: actor.principal.permissions.includes('fax:send')
      ? { fax_disabled: true, max_file_size_mb: 10, default_country: s().country, number_example: s().numberExample } : null,
    inbound_enabled: true,
    branding: { docs_base: 'https://docs.faxbot.net/latest/', logo_path: '/admin/ui/faxbot_full_logo.png' },
    provider_view: s().providerView,
    provider_names: s().providerNames,
  })),
  http.get('/auth/setup', () => json({ first_owner: s().firstOwner })),
  guarded('post', '/auth/logout', () => {
    s().session = null;
    return json({ ok: true });
  }),
  guarded('post', '/auth/password', ({ actor, body }) => {
    const invalid = strict(body, ['current_password', 'password']);
    if (invalid) return invalid;
    if (actor.principal.password !== body.current_password) return fail(401, AUTH_DETAIL);
    actor.principal.password = body.password;
    actor.principal.password_change_required = false;
    actor.principal.version += 1;
    s().csrf = 'csrf-2';
    return json({ ok: true, password_change_required: false });
  }),
  guarded('post', '/auth/owner/enroll', ({ body }) => {
    const invalid = strict(body, ['login', 'display_name', 'expected_policy_version']) ?? stale(body);
    if (invalid) return invalid;
    const user: Principal = { id: nextId('p'), kind: 'user', login: body.login, display_name: body.display_name, enabled: true,
      password: 'owner-temp-1', password_change_required: true, created_at: now(), last_login_at: null, version: 1,
      permissions: ALL_PERMISSIONS.map(([p]) => p) };
    s().principals.set(user.id, user);
    return committed({ temporary_password: 'owner-temp-1', user: userView(user) });
  }),
  guarded('get', '/auth/sessions', () => json({ items: [{ session_id: 'sess_current', source_kind: 'password',
    created_at: '2026-10-03T08:00:00', last_used_at: '2026-10-03T09:00:00', expires_at: '2026-10-03T20:00:00', revoked_at: null,
    current: true }], next_cursor: null })),

  guarded('get', '/access/permissions', () => json({ items: ALL_PERMISSIONS.map(([permission, group]) => ({ permission, group, description: permission })) })),

  guarded('get', '/access/roles', () => page([...s().roles.values()])),
  guarded('post', '/access/roles', ({ body }) => {
    const invalid = strict(body, ['name', 'description', 'permissions', 'enabled', 'expected_policy_version']) ?? stale(body);
    if (invalid) return invalid;
    const role: Role = { id: nextId('role'), name: body.name, description: body.description, builtin: false, enabled: body.enabled, permissions: body.permissions, version: 1 };
    s().roles.set(role.id, role);
    return committed({ role });
  }),
  guarded('patch', '/access/roles/:id', ({ body, params }) => {
    const role = s().roles.get(params.id);
    if (!role) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['version', 'expected_policy_version'], ['name', 'description', 'permissions', 'enabled']) ?? stale(body, [body.version, role.version]);
    if (invalid) return invalid;
    if (role.builtin) return fail(403, 'This operation is not permitted.');
    Object.assign(role, { ...body, version: role.version + 1 });
    delete (role as Json).expected_policy_version;
    return committed({ role });
  }),

  guarded('get', '/access/users', ({ url }) => {
    const kind = url.searchParams.get('kind') ?? 'all';
    const q = (url.searchParams.get('q') ?? '').toLowerCase();
    return page([...s().principals.values()]
      .filter((p) => kind === 'all' || p.kind === kind)
      .filter((p) => !q || p.display_name.toLowerCase().includes(q) || (p.login ?? '').includes(q))
      .map(userView));
  }),
  guarded('get', '/access/users/:id', ({ params }) => {
    const p = s().principals.get(params.id);
    if (!p) return fail(404, 'Access target not found.');
    return json({
      ...userView(p),
      memberships: [...s().memberships.values()].filter((m) => m.principal_id === p.id)
        .map((m) => ({ membership_id: m.id, group_id: m.group_id, group_name: s().groups.get(m.group_id)?.name ?? '', version: m.version })),
      assignments: [...s().assignments.values()].filter((a) => a.subject.id === p.id).map(assignmentView),
      keys: [...s().keys.values()].filter((k) => k.principal_id === p.id).map(keyView),
      effective: { installation: p.permissions, personal: [] },
    });
  }),
  guarded('post', '/access/users', ({ body }) => {
    const invalid = strict(body, ['login', 'display_name', 'enabled', 'expected_policy_version']) ?? stale(body);
    if (invalid) return invalid;
    const user: Principal = { id: nextId('p'), kind: 'user', login: body.login, display_name: body.display_name, enabled: body.enabled,
      password: 'temp-pass-1', password_change_required: true, created_at: now(), last_login_at: null, version: 1, permissions: [] };
    s().principals.set(user.id, user);
    return committed({ user: userView(user), temporary_password: 'temp-pass-1' });
  }),
  guarded('post', '/access/integrations', ({ body }) => {
    const invalid = strict(body, ['display_name', 'enabled', 'expected_policy_version']) ?? stale(body);
    if (invalid) return invalid;
    const integration: Principal = { id: nextId('p'), kind: 'integration', login: null, display_name: body.display_name, enabled: body.enabled,
      password: null, password_change_required: null, created_at: now(), last_login_at: null, version: 1, permissions: [] };
    s().principals.set(integration.id, integration);
    return committed({ integration: userView(integration) });
  }),
  guarded('patch', '/access/users/:id', ({ body, params }) => {
    const p = s().principals.get(params.id);
    if (!p) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['version', 'expected_policy_version'], ['display_name', 'enabled', 'login']) ?? stale(body, [body.version, p.version]);
    if (invalid) return invalid;
    for (const field of ['display_name', 'enabled', 'login'] as const) if (field in body) (p as Json)[field] = body[field];
    p.version += 1;
    return committed({ user: userView(p) });
  }),
  guarded('post', '/access/users/:id/reset-password', ({ body, params }) => {
    const p = s().principals.get(params.id);
    if (!p) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['version', 'expected_policy_version']) ?? stale(body, [body.version, p.version]);
    if (invalid) return invalid;
    p.password = 'temp-reset-1';
    p.password_change_required = true;
    p.version += 1;
    return committed({ temporary_password: 'temp-reset-1', user: userView(p) });
  }),

  guarded('get', '/access/groups', () => page([...s().groups.values()].map((g) => ({
    ...g, member_count: [...s().memberships.values()].filter((m) => m.group_id === g.id).length })))),
  guarded('get', '/access/groups/:id', ({ params }) => {
    const g = s().groups.get(params.id);
    if (!g) return fail(404, 'Access target not found.');
    const members = [...s().memberships.values()].filter((m) => m.group_id === g.id).map((m) => {
      const p = s().principals.get(m.principal_id)!;
      return { membership_id: m.id, principal_id: p.id, display_name: p.display_name, kind: p.kind, version: m.version };
    });
    return json({ ...g, member_count: members.length, members,
      assignments: [...s().assignments.values()].filter((a) => a.subject.id === g.id).map(assignmentView) });
  }),
  guarded('post', '/access/groups', ({ body }) => {
    const invalid = strict(body, ['name', 'description', 'enabled', 'expected_policy_version']) ?? stale(body);
    if (invalid) return invalid;
    const group: Group = { id: nextId('grp'), name: body.name, description: body.description, enabled: body.enabled, version: 1 };
    s().groups.set(group.id, group);
    return committed({ group: { ...group, member_count: 0 } });
  }),
  guarded('patch', '/access/groups/:id', ({ body, params }) => {
    const g = s().groups.get(params.id);
    if (!g) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['version', 'expected_policy_version'], ['name', 'description', 'enabled']) ?? stale(body, [body.version, g.version]);
    if (invalid) return invalid;
    for (const field of ['name', 'description', 'enabled'] as const) if (field in body) (g as Json)[field] = body[field];
    g.version += 1;
    return committed({ group: g });
  }),
  guarded('post', '/access/groups/:id/members', ({ body, params }) => {
    const g = s().groups.get(params.id);
    const p = s().principals.get(body.principal_id);
    if (!g || !p) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['principal_id', 'principal_version', 'group_version', 'expected_policy_version'])
      ?? stale(body, [body.principal_version, p.version], [body.group_version, g.version]);
    if (invalid) return invalid;
    const membership: Membership = { id: nextId('mem'), group_id: g.id, principal_id: p.id, version: 1 };
    s().memberships.set(membership.id, membership);
    g.version += 1;
    return committed({ membership_id: membership.id });
  }),
  guarded('post', '/access/groups/:id/members/:membership/remove', ({ body, params }) => {
    const g = s().groups.get(params.id);
    const m = s().memberships.get(params.membership);
    if (!g || !m) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['membership_version', 'group_version', 'expected_policy_version'])
      ?? stale(body, [body.membership_version, m.version], [body.group_version, g.version]);
    if (invalid) return invalid;
    s().memberships.delete(m.id);
    g.version += 1;
    return committed();
  }),

  guarded('get', '/access/resources', ({ url }) => {
    const kind = url.searchParams.get('kind');
    return page(resources().filter((r) => !kind || r.kind === kind));
  }),
  guarded('get', '/access/assignments', () => page([...s().assignments.values()].map(assignmentView))),
  guarded('post', '/access/assignments', ({ body }) => {
    const invalid = strict(body, ['subject', 'role', 'resource_id', 'expected_policy_version']) ?? stale(body);
    if (invalid) return invalid;
    const assignment: Assignment = { id: nextId('asg'), subject: { kind: body.subject.kind, id: body.subject.id }, role_id: body.role.id, resource_id: body.resource_id, version: 1 };
    s().assignments.set(assignment.id, assignment);
    return committed({ assignment: assignmentView(assignment) });
  }),
  guarded('post', '/access/assignments/:id/remove', ({ body, params }) => {
    const a = s().assignments.get(params.id);
    if (!a) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['version', 'expected_policy_version']) ?? stale(body, [body.version, a.version]);
    if (invalid) return invalid;
    s().assignments.delete(a.id);
    return committed();
  }),

  guarded('get', '/access/keys', ({ url }) => {
    const principal = url.searchParams.get('principal_id');
    return page([...s().keys.values()].filter((k) => !principal || k.principal_id === principal).map(keyView));
  }),
  guarded('post', '/access/keys', ({ body }) => {
    const invalid = strict(body, ['principal', 'name', 'note', 'expires_at', 'ceiling', 'expected_policy_version']);
    if (invalid) return invalid;
    const p = s().principals.get(body.principal?.id);
    if (!p) return fail(404, 'Access target not found.');
    const conflict = stale(body, [body.principal.version, p.version]);
    if (conflict) return conflict;
    const token = `fbk_live_new_${++s().sequence}`;
    const key: Key = { id: nextId('key'), token, principal_id: p.id, name: body.name, note: body.note, expires_at: body.expires_at,
      created_at: now(), last_used_at: null, revoked_at: null, pending_review: false, ceiling: body.ceiling, version: 1 };
    s().keys.set(key.id, key);
    return committed({ key: keyView(key), token });
  }),
  guarded('patch', '/access/keys/:id', ({ body, params }) => {
    const k = s().keys.get(params.id);
    if (!k) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['version', 'expected_policy_version'], ['name', 'note', 'expires_at']) ?? stale(body, [body.version, k.version]);
    if (invalid) return invalid;
    for (const field of ['name', 'note', 'expires_at'] as const) if (field in body) (k as Json)[field] = body[field];
    k.version += 1;
    return committed({ key: keyView(k) });
  }),
  guarded('post', '/access/keys/:id/rotate', ({ body, params }) => {
    const k = s().keys.get(params.id);
    if (!k) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['version', 'expected_policy_version']) ?? stale(body, [body.version, k.version]);
    if (invalid) return invalid;
    if (k.revoked_at) return fail(403, 'This operation is not permitted.');
    k.token = `fbk_live_rotated_${++s().sequence}`;
    k.version += 1;
    return committed({ key: keyView(k), token: k.token });
  }),
  guarded('post', '/access/keys/:id/revoke', ({ body, params }) => {
    const k = s().keys.get(params.id);
    if (!k) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['version', 'expected_policy_version']) ?? stale(body, [body.version, k.version]);
    if (invalid) return invalid;
    k.revoked_at = now();
    k.version += 1;
    return committed({ key: keyView(k) });
  }),
  guarded('post', '/access/keys/:id/approve', ({ body, params }) => {
    const k = s().keys.get(params.id);
    if (!k) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['principal', 'ceiling', 'version', 'expected_policy_version']) ?? stale(body, [body.version, k.version]);
    if (invalid) return invalid;
    Object.assign(k, { principal_id: body.principal.id, ceiling: body.ceiling, pending_review: false, version: k.version + 1 });
    return committed({ key: keyView(k) });
  }),

  guarded('get', '/access/sessions', ({ actor }) => page([{ session_id: 'sess_current', principal: { id: actor.principal.id, display_name: actor.principal.display_name },
    source_kind: 'password', created_at: '2026-10-03T08:00:00', last_used_at: '2026-10-03T09:00:00', expires_at: '2026-10-03T20:00:00',
    revoked_at: null, current: true }])),
  guarded('post', '/access/sessions/:id/revoke', ({ body }) => {
    const invalid = strict(body, ['expected_policy_version']) ?? stale(body);
    return invalid ?? committed();
  }),

  guarded('get', '/access/mailboxes', () => page([...s().mailboxes.values()].map((m) => ({
    ...m, rule_count: [...s().rules.values()].filter((r) => r.mailbox_id === m.id).length })))),
  guarded('post', '/access/mailboxes', ({ body }) => {
    const invalid = strict(body, ['label', 'enabled', 'expected_policy_version']) ?? stale(body);
    if (invalid) return invalid;
    const mailbox: Mailbox = { id: nextId('mbx'), label: body.label, enabled: body.enabled, resource_id: nextId('res_mbx'), version: 1 };
    s().mailboxes.set(mailbox.id, mailbox);
    return committed({ mailbox: { ...mailbox, rule_count: 0 } });
  }),
  guarded('patch', '/access/mailboxes/:id', ({ body, params }) => {
    const m = s().mailboxes.get(params.id);
    if (!m) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['version', 'expected_policy_version'], ['label', 'enabled']) ?? stale(body, [body.version, m.version]);
    if (invalid) return invalid;
    for (const field of ['label', 'enabled'] as const) if (field in body) (m as Json)[field] = body[field];
    m.version += 1;
    return committed({ mailbox: m });
  }),
  guarded('get', '/access/inbound-rules', () => page([...s().rules.values()].map((r) => ({
    ...r, mailbox_label: s().mailboxes.get(r.mailbox_id)?.label ?? '' })))),
  guarded('post', '/access/inbound-rules', ({ body }) => {
    const invalid = strict(body, ['to_number', 'mailbox_id', 'expected_policy_version'], RECEIVING_OPTIONS) ?? stale(body);
    if (invalid) return invalid;
    const toNumber = s().resolveNumber(body.to_number);
    if (toNumber === null) return fail(400, NUMBER_DETAIL);
    const rule: Rule = { id: nextId('rule'), to_number: toNumber, mailbox_id: body.mailbox_id, version: 1, ...optionsIn(body) };
    s().rules.set(rule.id, rule);
    return committed({ rule });
  }),
  guarded('patch', '/access/inbound-rules/:id', ({ body, params }) => {
    const r = s().rules.get(params.id);
    if (!r) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['version', 'expected_policy_version'], ['to_number', 'mailbox_id', ...RECEIVING_OPTIONS])
      ?? stale(body, [body.version, r.version]);
    if (invalid) return invalid;
    const toNumber = 'to_number' in body ? s().resolveNumber(body.to_number) : r.to_number;
    if (toNumber === null) return fail(400, NUMBER_DETAIL);
    if ('to_number' in body) r.to_number = toNumber;
    if ('mailbox_id' in body) r.mailbox_id = body.mailbox_id;
    Object.assign(r, optionsIn(body));
    r.version += 1;
    return committed({ rule: r });
  }),
];

// Console sections that load on entry; minimal replies keep tests quiet.
const consoleHandlers = [
  http.get('/admin/health-status', () => json({ timestamp: now(), backend: 'phaxio', backend_healthy: true,
    jobs: { queued: 0, in_progress: 0, recent_failures: 0 }, inbound_enabled: true, api_keys_configured: true, require_auth: true })),
  http.get('/admin/config', () => json({ fax_disabled: true, max_file_size_mb: 10 })),
  // Public liveness and readiness: up and ready unless a test says otherwise.
  http.get('/health', () => json({ status: 'ok' })),
  http.get('/health/ready', () => json({ status: 'ready', backend: 'phaxio', checks: {}, warnings: [] })),
  http.get('/admin/fax-jobs', () => json({ total: 0, jobs: [] })),
  http.get('/inbound', () => json([])),
  http.get('/admin/inbound/callbacks', () => json({ callbacks: [] })),
  // Receiving through HumbleFax: off until a test says otherwise.
  http.get('/admin/inbound/humblefax', () => json({ account: 'humblefax', receiving: false, turned_on: false,
    reason: 'Receive faxes from HumbleFax is off.', receiving_provider: false, poll_seconds: 60, checked_at: null,
    found: null, problem: null })),
  // Delivery routes, intake and direct delivery: empty until a test says otherwise.
  http.get('/routing/costs', () => json({ since: '2026-09-03T00:00:00', providers: [] })),
  // One fax's cost: nothing to say for a fax that placed no call.
  http.get('/routing/faxes/:jobId/cost', () => json({ state: 'none', summary: null, reported_cost: [], estimated_cost: [] })),
  // Provider rules (tests of their screens use providerRulesFake.ts): nothing held, a fax with no routing
  // decision to explain, and no provider accounts yet.
  http.get('/routing/holds', () => json({ holds: [] })),
  http.get('/routing/rules', () => json({ scope: { kind: 'organization', name: 'Organization' }, active: null, draft: null,
    organization: null, matches_30_days: {}, can_write: true, time_zone: 'America/Denver',
    choices: { accounts: [], people: [], keys: [], groups: [], mailboxes: [] } })),
  http.get('/routing/faxes/:jobId/route', ({ params }) => json({ job_id: params.jobId, sentence: null, attempts: [],
    hold: null, trace: [] })),
  http.get('/admin/providers/accounts', () => json({ generation: 1, default_sending: null, default_receiving: null,
    accounts: [], providers: [], sites: [] })),
  // Several trunks (WP-T): send-only numbers on the trunk page, and trunk advice under Recommendations.
  http.get('/admin/sip/send-only', () => json({ numbers: [], advice: [], quiet_days: 90 })),
  // Number advice (BG): placement, site advice, your NPI record and the check before a first fax.
  http.get('/routing/recommendations/numbers', () => json({ days: 30, estimate: true, state: 'no_numbers',
    sentence: 'Faxbot knows none of your fax numbers yet, so there is nothing to place.', numbers: [], accounts: [],
    note: 'Faxbot only advises: it never moves, releases or cancels a number or an account.' })),
  http.get('/routing/recommendations/sites', () => json({ days: 30, estimate: true, sentence: '', carriers: [],
    items: [], prices: [], caller_id: '' })),
  http.get('/routing/npi', () => json({ npis: [], sentence: 'Add your NPI so Faxbot can tell you when a number you '
    + 'might give up is still printed on your NPI record.', source_url: 'https://npiregistry.cms.hhs.gov/api-page' })),
  http.get('/routing/recipient-check', ({ request }) => json({ number: new URL(request.url).searchParams.get('to'),
    first_send: true, checked: false, state: 'no_name', warning: false, sentence: null, name: null, listed: [],
    source_url: 'https://npiregistry.cms.hhs.gov/api-page' })),
  http.get('/routing/recommendations/trunks', () => json({ window_days: 30, trunks: [], items: [],
    sentence: 'Trunk advice needs two or more trunks; with one, there is nothing to move.' })),
  http.get('/routing/inbound-costs', () => json({ costs: {} })),
  http.get('/routing/fax-costs', () => json({ costs: {} })),
  // Savings: nothing saved yet, every part an estimate.
  http.get('/routing/savings', () => json(emptySavings())),
  // Pricing the document itself: refused by default, so Send a fax keeps its page-count price.
  http.post('/routing/predict', () => json({ detail: 'This operation is not permitted.' }, 403)),
  // The Overview's savings map: an answer with no mechanisms draws no map.
  http.get('/routing/savings/mechanisms', () => json({ days: 30, title: 'How Faxbot saves money',
    sentence: 'Every way Faxbot saves money, in the order a fax meets them.', legend: [], stages: [] })),
  // Sending recommendations: no number has enough delivered faxes on two routes yet.
  http.get('/routing/recommendations/sending', () => json({ window_days: 30, min_delivered: 3, items: [],
    empty_sentence: 'Nothing to suggest yet. Faxbot compares the cost of two routes once each has delivered 3 faxes to the same number in the last 30 days.' })),
  // Receiving recommendations: too little call history yet.
  http.get('/routing/recommendations/receiving', () => json(newReceivingAdvice())),
  // Plans: no fax service with a monthly fee.
  http.get('/routing/recommendations/plans', () => json({ days: 30, estimate: true, plans: [],
    empty_sentence: 'You pay no monthly fee for a fax service, so there is no plan to review.' })),
  // Plans this month (Prices & plans) and other carriers (Recommendations): no plan, and no faxes to compare yet.
  http.get('/routing/plans', () => json({ plans: [], estimate: true, plan_budgets: '',
    empty_sentence: 'You pay no monthly fee for a fax service and set no allowance or commitment, so there is no plan to show.' })),
  http.get('/routing/plans/allocation', () => json({ plans: [], estimate: true,
    empty_sentence: 'None of your plans has a limited allowance or a normal-use budget this month, so there is nothing to share out.' })),
  http.get('/routing/recommendations/carriers', () => json({ days: 30, estimate: true, advice_only: true, sent: 0,
    received: 0, sentence: 'You sent and received no faxes in the last 30 days, so there is nothing to compare yet.',
    switching_sentence: 'Changing carriers means moving (porting) your fax numbers to the new carrier and opening an '
      + 'account there, often under a contract; Faxbot only compares published prices and never switches anything.',
    unpublished_sentence: null, cheapest: null, current: null, carriers: [] })),
  // The advice from history (fax marker, billing steps, partner candidates, toll-free numbers): nothing yet.
  http.get('/routing/recommendations/fax-marker', () => json(newFaxMarkerAdvice())),
  http.get('/routing/recommendations/billing-steps', () => json({ days: 30, estimate: true, carrier: null,
    state: 'no_trunk', sentence: 'Faxbot has no carrier line set up, so there are no calls to measure.', min_calls: 3,
    step: null, numbers: [], numbers_total: 0, calls_near: 0, saving: null })),
  http.get('/routing/recommendations/partners', () => json({ days: 30, min_faxes: 3, estimate: true, state: 'none',
    sentence: 'Nothing to suggest: in the last 30 days no fax went by a route that charges per call, or every number '
      + 'you fax is already a partner.', items: [], items_total: 0, link: 'recipients/partners' })),
  http.get('/routing/recommendations/toll-free', () => json({ state: 'none', days: 30, items: [],
    sentence: 'No recipient has a toll-free fax number on file. If one publishes a toll-free number for the same '
      + 'intake, add it under Recipients → Details; Faxbot uses it only once you record their approval, because the '
      + 'recipient pays for those calls.' })),
  // A recipient's toll-free number: none on file.
  http.get('/routing/destinations/:number/toll-free', ({ params }) => json({ number: String(params.number), current: null,
    history: [], approved_alternate: null, sentence: null })),
  // Direct messages and FHIR: no account, no address on file, no message sent or received.
  http.get('/digital/accounts', () => json({ generation: 1, accounts: [], kinds: [], presets: [] })),
  http.get('/digital/recipients/:number', ({ params }) => json({ number: String(params.number), addresses: [],
    accounts: [], sentence: 'Faxes to this number go only by fax until you confirm a Direct address or FHIR endpoint.' })),
  http.get('/digital/messages', () => json({ messages: [] })),
  http.get('/digital/faxes/:job', ({ params }) => json({ job_id: String(params.job), messages: [] })),
  // Caller-name lookup at Telnyx: nothing to show until a test says otherwise.
  http.get('/admin/sip/telnyx/names', () => json({ applies: false, numbers: [], text: null,
    price: { text: '$0.40 a month for each number', monthly: { currency: 'USD', amount: '0.40' },
      source_url: 'https://support.telnyx.com/en/articles/4366901-your-number-lookup-guide', read_on: '2026-10-07' } })),
  // Shaded areas: kept with a fax-friendly pattern where it saves time, so nothing to recommend.
  http.get('/routing/recommendations/fax-friendly', () => json({ choice: 'where_it_saves',
    label: 'Fax-friendly shading on documents you send', measured_sentence: '', days: 30,
    recommend: false, faxes_checked: 0, faxes_changed: 0, seconds_saved: 0, sentence: null, action: null })),
  // Case packets: none sent yet.
  http.get('/cases', () => json({ cases: [] })),
  // The audit log: nothing recorded yet.
  http.get('/access/audit', () => json({ items: [], next_cursor: null })),
  // The database: a file on the data volume, reachable.
  http.get('/admin/db-status', () => json({ url: 'sqlite:////faxdata/faxbot.db', engine: 'sqlite', connected: true, error: null,
    counts: { fax_jobs: 0, inbound_fax: 0, api_keys: null },
    sqlite: { path: '/faxdata/faxbot.db', exists: true, size_bytes: 4096, persistent_volume: true } })),
  // A number with no history and no route recommendation yet.
  http.get('/routing/destinations/:number', ({ params }) => json({ number: params.number, display_name: null, notes: null,
    preferred_route: null, accepts_references: false, version: 0, routes: [], estimated_cost_30_days: [],
    direct_partner: null, recommended_routes: [], available_routes: [] })),
  // The work queue: nothing assigned until a test says otherwise.
  http.get('/work', () => json({ items: [] })),
  http.get('/work/settings', () => json({ acknowledge_hours: 24, mailboxes: [] })),
  http.get('/intake/items', () => json({ items: [], counts: { received: 0, sending: 0, delivered: 0, failed: 0 } })),
  http.get('/intake/connectors', () => json({ connectors: [] })),
  http.get('/direct/peers', () => json({ peers: [] })),
  http.get('/direct/deliveries', () => json({ deliveries: [] })),
  // Notice faxes, documents sent in pieces and repaired calls: none until a test adds them.
  http.get('/direct/notices', () => json({ notices: [], notice_text: null })),
  http.get('/direct/transfers', () => json({ transfers: [] })),
  http.get('/direct/repairs', () => json({ repairs: [] })),
  // Partner relays: none until a test offers one.
  http.get('/direct/relay/agreements', () => json({ agreements: [] })),
  http.get('/direct/relay/costs', () => json({ days: 30, agreements: [] })),
  http.get('/direct/relay/recommendations', () => json({ days: 30, recommendations: [] })),
  http.get('/direct/relay/faxes', () => json({ faxes: [] })),
  // Find partners: nothing found, nothing published, direct delivery off.
  http.get('/direct/discovery', () => json(emptyDiscovery())),
  http.get('/forms', () => json({ forms: [], renderer: 'faxbot-forms-1' })),
  http.get('/forms/received', () => json({ received: [] })),
  http.get('/forms/deliveries', () => json({ deliveries: [] })),
  // SIP trunk call history (the Dashboard names a received call that left no fax).
  http.get('/admin/sip/calls', () => json({ items: [], next_cursor: null })),
  // What fax calls negotiated (measurement only): no calls, and no call for any received fax.
  http.get('/admin/sip/negotiation', ({ request }) => json({
    days: Number(new URL(request.url).searchParams.get('days') ?? 30), calls: 0, measured_calls: 0, groups: [],
    sentence: 'No answered fax calls on your phone line in the last 30 days.',
    note: 'For one number at a time, Faxbot starts slower or uses a more robust compression only after its own calls to that number fail the same way more than once; it never turns error correction off or lowers resolution.' })),
  http.get('/admin/sip/negotiation/received/:id', () => json({ detail: 'No phone-line call carried this fax.' }, 404)),
  // The network check for fax over IP: nothing to show until a test says otherwise.
  http.get('/admin/sip/network', () => json({ applies: false, checked: false, t38: null, text: null })),
  // Telnyx's T.38 setting on the trunk numbers: nothing to show until a test says otherwise.
  http.get('/admin/sip/telnyx', () => json({ applies: false, numbers: [], connection_texts: [], text: null })),
  // Published plans for providers in use with no rate card yet: none.
  http.get('/routing/published-plans/in-use', () => json({ items: [] })),
  // Sent faxes Faxbot could not confirm: none until a test says otherwise.
  http.get('/certainty/items', () => json({ items: [] })),
  http.get('/certainty/counts', () => json({ open: 0, mine: 0, unassigned: 0, overdue: 0, settled: 0 })),
  http.get('/certainty/faxes/:faxId', () => json({ items: [], about: null })),
  http.get('/continuations/faxes/:faxId', ({ params }) => json({ fax_id: params.faxId, offer: null, continued_by: null,
    continues: null })),
  http.get('/certainty/settings', () => json({ settle_hours: 24, version: 0, fallback: null, people: [] })),
  // Work counts for the Overview's Needs attention card: nothing waiting.
  http.get('/work/counts', () => json({ open: 0, acknowledged: 0, done: 0, unassigned: 0, mine: 0, overdue: 0 })),
  // Sending together: no number sends faxes together until a test says otherwise.
  http.get('/batching/check', ({ request }) => json({ number: new URL(request.url).searchParams.get('to'),
    sends_together: false, wait_minutes: null, sentence: null })),
  http.get('/batching/faxes/:jobId', () => json({ state: null, sentence: null })),
  http.get('/batching/numbers/:number', ({ params }) => json({ number: params.number, enabled: false,
    max_wait_minutes: 10, max_pages: 30, mixed_senders: false, version: 0, saves_money: false,
    route_sentence: 'Faxbot sends faxes together only over its own SIP trunk; faxes to this number go through Phaxio, so they go straight away.',
    state_sentence: 'Off: faxes to this number go straight away.', agreement: null, history: [],
    savings: { calls: 0, faxes: 0, calls_saved: 0, estimated_saving: [], is_estimate: true,
      sentence: 'No faxes to this number have been sent together in the last 30 days.' },
    agreement_text: 'This recipient has agreed to receive several documents in one call.' })),
];

export function emptyDiscovery(): Json {
  return {
    direct_delivery: false,
    settings: { well_known: true, from_calls: true, directories: [], private_allowed: false },
    texts: { well_known: 'Turn on "Use direct delivery" under Recipients → Partners → Direct delivery to answer '
      + 'lookups and to find partners.',
      from_calls: 'When a fax call shows the other side runs Faxbot, Faxbot asks that address once whether it takes '
        + 'faxes directly. This never places a call.',
      directories: 'Faxbot looks numbers up only in directories you trust. None is listed, so nothing is looked up.',
      private: null, well_known_url: 'https://fax.example.test/.well-known/faxbot-direct' },
    suggestions: [], partners: [], introductions: [], publications: [], lookups: [],
    publishable: { number: null, receives: false, sentence: 'Turn on "Use direct delivery" under Recipients → Partners → '
      + 'Direct delivery first; senders reach this Faxbot through it.' },
  };
}

export const server = setupServer(...accessHandlers, ...consoleHandlers);
