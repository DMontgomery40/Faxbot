// 'email' opens Settings at the email delivery settings; 'routes' opens Tools, Delivery routes.
export type AdminDestination = 'send' | 'jobs' | 'inbox' | 'settings' | 'keys' | 'diagnostics' | 'routes' | 'email';

export type TopTab = 'dashboard' | 'send' | 'jobs' | 'inbox' | 'settings' | 'tools';
export type SettingsTab = 'setup' | 'settings' | 'keys' | 'users' | 'groups' | 'roles' | 'access' | 'sessions' | 'mcp';
export type ToolTab = 'routes' | 'terminal' | 'diagnostics' | 'logs' | 'plugins' | 'scripts';

// Each entry is visible when the signed-in identity holds any listed
// permission at the installation. These are display hints only; the server
// checks every request again.
const SETTINGS_REQUIREMENTS: Array<{ value: SettingsTab; label: string; anyOf: string[] | null }> = [
  { value: 'setup', label: 'Setup', anyOf: ['settings:write'] },
  { value: 'settings', label: 'Settings', anyOf: ['settings:read'] },
  { value: 'keys', label: 'Keys', anyOf: ['keys:manage'] },
  { value: 'users', label: 'Users', anyOf: ['users:read', 'users:manage'] },
  { value: 'groups', label: 'Groups', anyOf: ['groups:read', 'groups:manage'] },
  { value: 'roles', label: 'Roles', anyOf: ['roles:read', 'roles:manage'] },
  { value: 'access', label: 'Access', anyOf: ['grants:read', 'grants:manage', 'mailboxes:read', 'mailboxes:manage'] },
  // Everyone can see and end their own sessions.
  { value: 'sessions', label: 'Sessions', anyOf: null },
  { value: 'mcp', label: 'MCP', anyOf: ['settings:read'] },
];

const TOOL_REQUIREMENTS: Array<{ value: ToolTab; label: string; anyOf: string[] }> = [
  { value: 'routes', label: 'Delivery routes', anyOf: ['settings:read'] },
  { value: 'terminal', label: 'Terminal', anyOf: ['host:terminal'] },
  { value: 'diagnostics', label: 'Diagnostics', anyOf: ['diagnostics:read'] },
  { value: 'logs', label: 'Logs', anyOf: ['logs:read'] },
  { value: 'plugins', label: 'Plugins', anyOf: ['providers:read'] },
  { value: 'scripts', label: 'Scripts & Tests', anyOf: ['providers:write'] },
];

const holdsAny = (permissions: ReadonlySet<string>, anyOf: string[] | null) =>
  anyOf === null || anyOf.some((permission) => permissions.has(permission));

export function visibleSettings(permissions: ReadonlySet<string>): Array<{ value: SettingsTab; label: string }> {
  return SETTINGS_REQUIREMENTS.filter((item) => holdsAny(permissions, item.anyOf))
    .map(({ value, label }) => ({ value, label }));
}

export function visibleTools(permissions: ReadonlySet<string>, pluginsEnabled: boolean): Array<{ value: ToolTab; label: string }> {
  return TOOL_REQUIREMENTS.filter((item) => holdsAny(permissions, item.anyOf) && (item.value !== 'plugins' || pluginsEnabled))
    .map(({ value, label }) => ({ value, label }));
}

export function visibleTopTabs(
  permissions: ReadonlySet<string>,
  navigation: { send: boolean; jobs: boolean; inbox: boolean },
  hasTools: boolean,
): TopTab[] {
  const tabs: TopTab[] = [];
  if (permissions.has('diagnostics:read') || permissions.has('settings:read')) tabs.push('dashboard');
  if (navigation.send) tabs.push('send');
  if (navigation.jobs) tabs.push('jobs');
  if (navigation.inbox) tabs.push('inbox');
  tabs.push('settings');
  if (hasTools) tabs.push('tools');
  return tabs;
}
