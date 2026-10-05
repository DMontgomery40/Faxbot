"""Frozen 0019: three permissions that guard nothing leave the access catalogue; registration belongs to ``schema``.

``host:actions`` (the removed server checks) and ``tunnels:read`` / ``tunnels:manage``
(the removed remote-access tunnel) authorize no route any more. The access store checks
its catalogue exactly at startup, so removing them from the console's role editor is a
revision. ``tunnels:pair`` (phone pairing) stays.

The upgrade deletes, in foreign-key order:

- every role's row for the three permissions: the built-in Owner (all three),
  Administrator (both tunnel permissions) and Host Operator (``host:actions``), and any
  role an owner made themselves;
- every API key limit (``access_key_grants``) naming one of them. A key whose limits
  become empty is allowed nothing, because a key may only use what its limits name;
- the three ``access_permissions`` rows.

It writes one audit row (``access.retire_permissions``, outcome ``migrated``, no
actor) listing the removed rows of owners' own roles and of key limits with their
original ids, so history keeps them. Policy, role and key versions are unchanged, as
in 0011 and 0018: the permissions authorized nothing, so no decision changes.

The downgrade puts the three permissions and the built-in rows back with their
original identities, and restores owners' role rows and key limits from the newest
audit row of this revision, skipping any whose role, key or resource no longer exists.
It never deletes the audit row. Runtime code reflects these tables; it never imports
this metadata.
"""
from datetime import datetime
import json
import uuid

import sqlalchemy as sa

from .schema_access import _identity
from .schema_terminal import frozen_metadata as previous_metadata


REVISION = '0019_retired_permissions'
# This revision adds no table.
TABLES = frozenset()
OPERATION = 'access.retire_permissions'
# Literals belong to this revision, never the mutable runtime catalogue.
PERMISSIONS = {
    'host:actions': 'Execute approved host actions',
    'tunnels:read': 'Read tunnel settings',
    'tunnels:manage': 'Manage tunnels',
}
BUILTIN_ROWS = (
    ('role_owner', ('host:actions', 'tunnels:read', 'tunnels:manage')),
    ('role_administrator', ('tunnels:read', 'tunnels:manage')),
    ('role_host_operator', ('host:actions',)),
)
BUILTIN_IDS = frozenset(_identity('role_permission', role, permission)
                        for role, permissions in BUILTIN_ROWS for permission in permissions)


def frozen_metadata(*, dialect='sqlite'):
    return previous_metadata(dialect=dialect)


def _tables(connection):
    tables = frozen_metadata(dialect=connection.dialect.name).tables
    return (tables['access_permissions'], tables['access_role_permissions'], tables['access_key_grants'],
            tables['access_roles'], tables['access_audit'], tables['access_state'])


def _retired(column):
    return column.in_(sorted(PERMISSIONS))


def upgrade_retired_permissions(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a catalogue
    # that is not as the earlier revision left it before changing anything.
    from .schema import SchemaUpgradeError
    permissions, members, grants, roles, audit, state = _tables(connection)
    present = set(connection.execute(sa.select(permissions.c.id).where(_retired(permissions.c.id))).scalars())
    builtin = set(connection.execute(sa.select(members.c.id).join(roles, roles.c.id == members.c.role_id)
                                     .where(roles.c.kind == 'builtin', _retired(members.c.permission_id))).scalars())
    if present != set(PERMISSIONS) or builtin != BUILTIN_IDS:
        raise SchemaUpgradeError('The access catalogue is not as the earlier revision left it.')
    custom = [dict(row) for row in connection.execute(
        sa.select(members.c.id, members.c.role_id, members.c.permission_id).join(roles, roles.c.id == members.c.role_id)
        .where(roles.c.kind != 'builtin', _retired(members.c.permission_id)).order_by(members.c.id)).mappings()]
    limits = [dict(row) for row in connection.execute(
        sa.select(grants.c.id, grants.c.key_binding_id, grants.c.permission_id, grants.c.resource_id)
        .where(_retired(grants.c.permission_id)).order_by(grants.c.id)).mappings()]
    connection.execute(members.delete().where(_retired(members.c.permission_id)))
    connection.execute(grants.delete().where(_retired(grants.c.permission_id)))
    connection.execute(permissions.delete().where(_retired(permissions.c.id)))
    version = connection.execute(sa.select(state.c.policy_version)).scalar_one()
    details = {'revision': REVISION, 'permissions': sorted(PERMISSIONS), 'role_permissions': custom,
               'key_grants': limits}
    connection.execute(audit.insert().values(
        id=uuid.uuid4().hex, actor_principal_id=None, actor_key_binding_id=None, actor_session_id=None,
        operation=OPERATION, target_kind='installation', target_id='installation',
        policy_version_before=version, policy_version_after=version, outcome='migrated',
        details=json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True),
        created_at=datetime.utcnow()))


def _exists(connection, table, identity):
    return connection.execute(sa.select(table.c.id).where(table.c.id == identity)).first() is not None


def downgrade_retired_permissions(connection, operations):
    from .schema import SchemaUpgradeError
    permissions, members, grants, roles, audit, _ = _tables(connection)
    if connection.execute(sa.select(permissions.c.id).where(_retired(permissions.c.id))).first():
        raise SchemaUpgradeError('The retired permissions already exist.')
    for permission, description in PERMISSIONS.items():
        connection.execute(permissions.insert().values(id=permission, description=description))
    for role, granted in BUILTIN_ROWS:
        for permission in granted:
            connection.execute(members.insert().values(id=_identity('role_permission', role, permission),
                                                       role_id=role, permission_id=permission))
    recorded = None
    for text in connection.execute(sa.select(audit.c.details).where(audit.c.operation == OPERATION)
                                   .order_by(audit.c.created_at.desc(), audit.c.id.desc())).scalars():
        details = json.loads(text)
        if details.get('revision') == REVISION:
            recorded = details
            break
    if recorded is None:
        return
    resources, bindings = (frozen_metadata(dialect=connection.dialect.name).tables[name]
                           for name in ('access_resources', 'access_key_bindings'))
    for row in recorded.get('role_permissions', []):
        if _exists(connection, roles, row['role_id']) and not _exists(connection, members, row['id']):
            connection.execute(members.insert().values(id=row['id'], role_id=row['role_id'],
                                                       permission_id=row['permission_id']))
    for row in recorded.get('key_grants', []):
        if (_exists(connection, bindings, row['key_binding_id']) and _exists(connection, resources, row['resource_id'])
                and not _exists(connection, grants, row['id'])):
            connection.execute(grants.insert().values(id=row['id'], key_binding_id=row['key_binding_id'],
                                                      permission_id=row['permission_id'],
                                                      resource_id=row['resource_id']))
