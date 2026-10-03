import type { AccessResource, AuthMe, PermissionGroup, PermissionInfo } from '../../api/types';

// Plain-language names for the permission catalogue.
export const PERMISSION_LABELS: Record<string, string> = {
  'fax:send': 'Send faxes',
  'fax:read': 'See sent faxes',
  'fax:document': 'Open sent fax documents',
  'fax:refresh': 'Refresh fax status',
  'fax:reconcile': 'Resolve uncertain faxes',
  'inbound:list': 'List received faxes',
  'inbound:read': 'See received fax details',
  'inbound:document': 'Open received fax documents',
  'keys:manage': 'Manage API keys',
  'users:read': 'See users',
  'users:manage': 'Manage users',
  'groups:read': 'See groups',
  'groups:manage': 'Manage groups',
  'roles:read': 'See roles',
  'roles:manage': 'Manage roles',
  'grants:read': 'See who has access',
  'grants:manage': 'Give and remove access',
  'sessions:read': "See other people's sessions",
  'sessions:revoke': "End other people's sessions",
  'settings:read': 'See settings',
  'settings:write': 'Change settings',
  'providers:read': 'See fax providers',
  'providers:write': 'Configure fax providers',
  'providers:install': 'Install fax providers',
  'diagnostics:read': 'Run diagnostics',
  'logs:read': 'Read logs',
  'audit:read': 'Read the audit trail',
  'tunnels:read': 'See remote access',
  'tunnels:manage': 'Configure remote access',
  'tunnels:pair': 'Pair mobile devices',
  'host:restart': 'Restart the server',
  'host:actions': 'Run server actions',
  'host:terminal': 'Use the server terminal',
  'owner:recover': 'Owner recovery',
  'mailboxes:read': 'See mailboxes',
  'mailboxes:manage': 'Manage mailboxes',
};

export const GROUP_LABELS: Record<PermissionGroup, string> = {
  fax: 'Sent faxes',
  inbound: 'Received faxes',
  mailbox: 'Mailboxes',
  identity: 'People and access',
  config: 'Settings and providers',
  host: 'Server',
  audit: 'Logs and audit',
};

export const GROUP_ORDER: PermissionGroup[] = ['fax', 'inbound', 'mailbox', 'identity', 'config', 'host', 'audit'];

const FALLBACK_GROUPS: Record<string, PermissionGroup> = {
  fax: 'fax', inbound: 'inbound', mailboxes: 'mailbox', keys: 'identity', users: 'identity', groups: 'identity',
  roles: 'identity', grants: 'identity', sessions: 'identity', owner: 'identity', settings: 'config',
  providers: 'config', diagnostics: 'host', tunnels: 'host', host: 'host', logs: 'audit', audit: 'audit',
};

// Used when the server does not publish its catalogue yet.
export const FALLBACK_CATALOGUE: PermissionInfo[] = Object.keys(PERMISSION_LABELS).map((permission) => ({
  permission,
  group: FALLBACK_GROUPS[permission.split(':')[0]] ?? 'config',
  description: PERMISSION_LABELS[permission],
}));

export function permissionLabel(permission: string): string {
  return PERMISSION_LABELS[permission] ?? permission;
}

// Permissions the signed-in identity may hand out at the installation.
export function grantable(me: AuthMe): Set<string> {
  return new Set(me.grantable?.installation ?? me.permissions);
}

export function resourceLabel(resource: Pick<AccessResource, 'kind' | 'name'>): string {
  switch (resource.kind) {
    case 'installation': return 'Everything';
    case 'mailbox': return `Mailbox: ${resource.name}`;
    case 'personal': return `${resource.name}'s own faxes`;
    case 'legacy': return 'Earlier faxes without an owner';
    default: return resource.name;
  }
}
