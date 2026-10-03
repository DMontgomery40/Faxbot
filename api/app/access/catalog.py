"""Runtime literals; independent of frozen schema migration declarations."""
from types import MappingProxyType

CATALOGUE_VERSION = 1
PERMISSIONS = frozenset({
    "fax:send", "fax:read", "fax:document", "fax:refresh", "fax:reconcile",
    "inbound:list", "inbound:read", "inbound:document", "keys:manage", "users:read",
    "users:manage", "groups:read", "groups:manage", "roles:read", "roles:manage",
    "grants:read", "grants:manage", "sessions:read", "sessions:revoke", "settings:read",
    "settings:write", "providers:read", "providers:write", "providers:install",
    "diagnostics:read", "logs:read", "audit:read", "tunnels:read", "tunnels:manage",
    "tunnels:pair", "host:restart", "host:actions", "host:terminal", "owner:recover",
    "mailboxes:read", "mailboxes:manage",
})
OUTBOUND_PERMISSIONS = frozenset({"fax:read", "fax:document", "fax:refresh", "fax:reconcile"})
INBOUND_PERMISSIONS = frozenset({"inbound:list", "inbound:read", "inbound:document"})
GLOBAL_PERMISSIONS = PERMISSIONS - OUTBOUND_PERMISSIONS - INBOUND_PERMISSIONS - {"fax:send"}
BUILTIN_ROLE_PERMISSIONS = MappingProxyType({
    "role_owner": PERMISSIONS,
    "role_administrator": PERMISSIONS - {"host:restart", "host:actions", "host:terminal", "owner:recover"},
    "role_fax_operator": frozenset({"fax:send", "fax:read", "fax:document", "fax:refresh", "inbound:list", "inbound:read", "inbound:document"}),
    "role_fax_viewer": frozenset({"fax:read", "fax:document", "inbound:list", "inbound:read", "inbound:document"}),
    "role_auditor": frozenset({"audit:read", "fax:read", "inbound:list", "inbound:read"}),
    "role_host_operator": frozenset({"host:restart", "host:actions", "host:terminal", "diagnostics:read", "settings:read"}),
})
