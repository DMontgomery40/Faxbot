"""Runtime literals; independent of frozen schema migration declarations."""
from types import MappingProxyType

CATALOGUE_VERSION = 1
PERMISSIONS = frozenset({
    "fax:send", "fax:read", "fax:document", "fax:refresh", "fax:reconcile",
    "inbound:list", "inbound:read", "inbound:document", "keys:manage", "users:read",
    "users:manage", "groups:read", "groups:manage", "roles:read", "roles:manage",
    "grants:read", "grants:manage", "sessions:read", "sessions:revoke", "settings:read",
    "settings:write", "providers:read", "providers:write", "providers:install",
    "diagnostics:read", "logs:read", "audit:read",
    "tunnels:pair", "host:restart", "host:terminal", "owner:recover",
    "mailboxes:read", "mailboxes:manage",
    "work:read", "work:manage", "work:export", "work:import",
    # Approve, refuse or send anyway a fax held by sending rules (revision 0032).
    "fax:approve",
})
OUTBOUND_PERMISSIONS = frozenset({"fax:read", "fax:document", "fax:refresh", "fax:reconcile"})
# Work on a received document is scoped like the document itself: at the
# installation, a mailbox, the unassigned container or one received fax.
WORK_PERMISSIONS = frozenset({"work:read", "work:manage", "work:export"})
INBOUND_PERMISSIONS = frozenset({"inbound:list", "inbound:read", "inbound:document"}) | WORK_PERMISSIONS
GLOBAL_PERMISSIONS = PERMISSIONS - OUTBOUND_PERMISSIONS - INBOUND_PERMISSIONS - {"fax:send"}
# The ordinary scopes an integration API key may carry, granted at installation.
# Document scopes let mobile and SDK keys download fax PDFs; metadata never implies them.
# work:import lets another system hand documents to the work queue.
KEY_SCOPES = frozenset({"fax:send", "fax:read", "fax:document", "inbound:list", "inbound:read",
                        "inbound:document", "keys:manage", "work:import"})
BUILTIN_ROLE_PERMISSIONS = MappingProxyType({
    "role_owner": PERMISSIONS,
    "role_administrator": PERMISSIONS - {"host:restart", "host:terminal", "owner:recover"},
    "role_fax_operator": frozenset({"fax:send", "fax:read", "fax:document", "fax:refresh", "inbound:list",
                                    "inbound:read", "inbound:document", "work:read", "work:manage"}),
    "role_fax_viewer": frozenset({"fax:read", "fax:document", "inbound:list", "inbound:read", "inbound:document",
                                  "work:read"}),
    "role_auditor": frozenset({"audit:read", "fax:read", "inbound:list", "inbound:read", "work:read",
                               "work:export"}),
    # The Terminal is the Owner role's by default (revision 0018); a role of the owner's own can grant it.
    "role_host_operator": frozenset({"host:restart", "diagnostics:read", "settings:read"}),
})
