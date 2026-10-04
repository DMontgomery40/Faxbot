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
  ['tunnels:read', 'host'], ['tunnels:manage', 'host'], ['tunnels:pair', 'host'], ['host:restart', 'host'],
  ['host:actions', 'host'], ['host:terminal', 'host'], ['owner:recover', 'identity'], ['mailboxes:read', 'mailbox'],
  ['mailboxes:manage', 'mailbox'],
];

const ADMIN_PERMISSIONS = ALL_PERMISSIONS.map(([permission]) => permission)
  .filter((permission) => !['host:restart', 'host:actions', 'host:terminal', 'owner:recover', 'diagnostics:read', 'settings:read'].includes(permission));

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
interface Rule { id: string; to_number: string; mailbox_id: string; version: number }
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
    provider_view: null,
  })),
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
    const invalid = strict(body, ['to_number', 'mailbox_id', 'expected_policy_version']) ?? stale(body);
    if (invalid) return invalid;
    const toNumber = s().resolveNumber(body.to_number);
    if (toNumber === null) return fail(400, NUMBER_DETAIL);
    const rule: Rule = { id: nextId('rule'), to_number: toNumber, mailbox_id: body.mailbox_id, version: 1 };
    s().rules.set(rule.id, rule);
    return committed({ rule });
  }),
  guarded('patch', '/access/inbound-rules/:id', ({ body, params }) => {
    const r = s().rules.get(params.id);
    if (!r) return fail(404, 'Access target not found.');
    const invalid = strict(body, ['version', 'expected_policy_version'], ['to_number', 'mailbox_id']) ?? stale(body, [body.version, r.version]);
    if (invalid) return invalid;
    const toNumber = 'to_number' in body ? s().resolveNumber(body.to_number) : r.to_number;
    if (toNumber === null) return fail(400, NUMBER_DETAIL);
    if ('to_number' in body) r.to_number = toNumber;
    if ('mailbox_id' in body) r.mailbox_id = body.mailbox_id;
    r.version += 1;
    return committed({ rule: r });
  }),
];

// Console sections that load on entry; minimal replies keep tests quiet.
const consoleHandlers = [
  http.get('/admin/health-status', () => json({ timestamp: now(), backend: 'phaxio', backend_healthy: true,
    jobs: { queued: 0, in_progress: 0, recent_failures: 0 }, inbound_enabled: true, api_keys_configured: true, require_auth: true })),
  http.get('/admin/config', () => json({ fax_disabled: true, max_file_size_mb: 10 })),
  http.get('/admin/fax-jobs', () => json({ total: 0, jobs: [] })),
  http.get('/inbound', () => json([])),
  http.get('/admin/inbound/callbacks', () => json({ callbacks: [] })),
  // Delivery routes, intake and direct delivery: empty until a test says otherwise.
  http.get('/routing/costs', () => json({ since: '2026-09-03T00:00:00', providers: [] })),
  http.get('/intake/items', () => json({ items: [], counts: { received: 0, sending: 0, delivered: 0, failed: 0 } })),
  http.get('/intake/connectors', () => json({ connectors: [] })),
  http.get('/direct/peers', () => json({ peers: [] })),
  http.get('/direct/deliveries', () => json({ deliveries: [] })),
  // SIP trunk call history (the Dashboard names a received call that left no fax).
  http.get('/admin/sip/calls', () => json({ items: [], next_cursor: null })),
];

export const server = setupServer(...accessHandlers, ...consoleHandlers);
