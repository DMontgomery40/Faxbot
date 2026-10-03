"""Frozen revision0005 access schema/enrollment, independent of runtime policy."""
from datetime import datetime, timezone
import json
import re
import uuid

import sqlalchemy as sa

from .schema_outbound import frozen_metadata as outbound_metadata

REVISION = '0005_access_control'
TABLES = frozenset({
    'access_state', 'access_principals', 'access_users', 'access_groups',
    'access_memberships', 'access_permissions', 'access_roles', 'access_role_permissions',
    'access_resources', 'access_assignments', 'access_key_bindings', 'access_key_grants',
    'access_sessions', 'access_audit', 'access_mailbox_routes',
})
IDENTITY_NAMESPACE = uuid.UUID('f56f5b6b-2485-4af6-8840-a535b333a28c')

# Literals belong to this revision, never an imported mutable runtime catalogue.
PERMISSIONS = {
    'fax:send': 'Send outbound faxes', 'fax:read': 'Read outbound fax metadata',
    'fax:document': 'Read outbound fax documents', 'fax:refresh': 'Refresh outbound fax status',
    'fax:reconcile': 'Reconcile outbound fax evidence', 'inbound:list': 'List inbound fax metadata',
    'inbound:read': 'Read inbound fax metadata', 'inbound:document': 'Read inbound fax documents',
    'keys:manage': 'Manage delegated integration credentials', 'users:read': 'Read users',
    'users:manage': 'Manage users', 'groups:read': 'Read groups', 'groups:manage': 'Manage groups',
    'roles:read': 'Read roles', 'roles:manage': 'Manage roles', 'grants:read': 'Read role assignments',
    'grants:manage': 'Manage role assignments', 'sessions:read': 'Read sessions',
    'sessions:revoke': 'Revoke sessions', 'settings:read': 'Read installation settings',
    'settings:write': 'Change installation settings', 'providers:read': 'Read provider settings',
    'providers:write': 'Change provider settings', 'providers:install': 'Install providers',
    'diagnostics:read': 'Read installation diagnostics', 'logs:read': 'Read installation logs',
    'audit:read': 'Read security audit', 'tunnels:read': 'Read tunnel settings',
    'tunnels:manage': 'Manage tunnels', 'tunnels:pair': 'Pair tunnel clients',
    'host:restart': 'Restart the host service', 'host:actions': 'Execute approved host actions',
    'host:terminal': 'Use the host terminal', 'owner:recover': 'Recover named Owner access',
    'mailboxes:read': 'Read mailbox management settings', 'mailboxes:manage': 'Manage mailboxes and routing',
}
BUILTIN_ROLES = (
    ('role_owner', 'Owner', 'owner', 'Full installation authority', tuple(PERMISSIONS)),
    ('role_administrator', 'Administrator', 'administrator', 'Manage installation and access',
     tuple(p for p in PERMISSIONS if p not in {'host:restart', 'host:actions', 'host:terminal', 'owner:recover'})),
    ('role_fax_operator', 'Fax Operator', 'fax operator', 'Send and review fax documents',
     ('fax:send', 'fax:read', 'fax:document', 'fax:refresh', 'inbound:list', 'inbound:read', 'inbound:document')),
    ('role_fax_viewer', 'Fax Viewer', 'fax viewer', 'Review fax documents',
     ('fax:read', 'fax:document', 'inbound:list', 'inbound:read', 'inbound:document')),
    ('role_auditor', 'Auditor', 'auditor', 'Review security audit and fax metadata',
     ('audit:read', 'fax:read', 'inbound:list', 'inbound:read')),
    ('role_host_operator', 'Host Operator', 'host operator', 'Operate installation host',
     ('host:restart', 'host:actions', 'host:terminal', 'diagnostics:read', 'settings:read')),
)
LEGACY_PERMISSIONS = frozenset({'fax:send', 'fax:read', 'inbound:list', 'inbound:read', 'keys:manage'})


def _identity(domain, *parts):
    return uuid.uuid5(IDENTITY_NAMESPACE, json.dumps([domain, *parts], ensure_ascii=True,
                                                   separators=(',', ':'))).hex


def frozen_metadata(*, dialect='sqlite'):
    metadata = outbound_metadata(dialect=dialect)

    def table(name, fields, *, checks=(), foreign_keys=(), indexes=()):
        columns = [sa.Column(field, kind, nullable=nullable) for field, kind, nullable in fields]
        constraints = [sa.PrimaryKeyConstraint('id', name='pk_' + name)]
        constraints.extend(sa.CheckConstraint(expression, name=check) for check, expression in checks)
        constraints.extend(sa.ForeignKeyConstraint(local, remote, name=fk, ondelete='RESTRICT',
                           use_alter=dialect == 'postgresql' and fk == 'fk_access_resources_parent')
                           for fk, local, remote in foreign_keys)
        result = sa.Table(name, metadata, *columns, *constraints)
        for index, keys, unique in indexes:
            sa.Index(index, *(result.c[key] for key in keys), unique=unique)
        return result

    def fields(*items):
        # A trailing '?' marks the only nullable fields. All primary keys and
        # other unmarked fields are explicitly NOT NULL, with no defaults.
        kinds = {'id': sa.String(40), 'int': sa.Integer(), 'time': sa.DateTime(), 'text': sa.Text()}
        return [(name.rstrip('?'), kinds[kind] if kind in kinds else sa.String(kind), name.endswith('?'))
                for name, kind in items]

    def fk(name, field, target):
        return (name, [field], [target])

    timestamps = (('created_at', 'time'), ('updated_at', 'time'))
    table('access_state', fields(('id', 'id'), ('policy_version', 'int'), ('catalogue_version', 'int'), ('updated_at', 'time')),
          checks=(('ck_access_state_singleton', "id = 'state'"),
                  ('ck_access_state_versions', 'policy_version >= 1 AND catalogue_version = 1')))
    table('access_principals', fields(('id', 'id'), ('kind', 16), ('display_name', 200), ('enabled', 'int'),
          ('security_version', 'int'), ('version', 'int'), *timestamps), checks=(
          ('ck_access_principals_kind', "kind = 'user' OR kind = 'integration' OR kind = 'bootstrap'"),
          ('ck_access_principals_enabled', 'enabled = 0 OR enabled = 1'),
          ('ck_access_principals_versions', 'security_version >= 1 AND version >= 1'),
          ('ck_access_principals_bootstrap', "(kind = 'bootstrap' AND id = 'bootstrap') OR (kind <> 'bootstrap' AND id <> 'bootstrap')")),
          indexes=(('ix_access_principals_kind_enabled', ('kind', 'enabled'), False),))
    table('access_users', fields(('id', 'id'), ('login', 100), ('normalized_login', 100), ('password_hash', 256),
          ('password_change_required', 'int'), ('password_version', 'int'), *timestamps), checks=(
          ('ck_access_users_password_change', 'password_change_required = 0 OR password_change_required = 1'),
          ('ck_access_users_password_version', 'password_version >= 1')),
          foreign_keys=(fk('fk_access_users_principal', 'id', 'access_principals.id'),),
          indexes=(('uq_access_users_normalized_login', ('normalized_login',), True),))
    table('access_groups', fields(('id', 'id'), ('name', 100), ('normalized_name', 100), ('description', 500),
          ('enabled', 'int'), ('version', 'int'), *timestamps), checks=(
          ('ck_access_groups_enabled', 'enabled = 0 OR enabled = 1'), ('ck_access_groups_version', 'version >= 1')),
          indexes=(('uq_access_groups_normalized_name', ('normalized_name',), True),))
    table('access_memberships', fields(('id', 'id'), ('group_id', 'id'), ('principal_id', 'id'),
          ('version', 'int'), *timestamps), checks=(('ck_access_memberships_version', 'version >= 1'),),
          foreign_keys=(fk('fk_access_memberships_group', 'group_id', 'access_groups.id'),
                        fk('fk_access_memberships_principal', 'principal_id', 'access_principals.id')),
          indexes=(('uq_access_memberships_group_principal', ('group_id', 'principal_id'), True),
                   ('ix_access_memberships_principal', ('principal_id',), False)))
    table('access_permissions', fields(('id', 64), ('description', 200)))
    table('access_roles', fields(('id', 'id'), ('name', 100), ('normalized_name', 100), ('description', 500),
          ('kind', 16), ('enabled', 'int'), ('version', 'int'), *timestamps), checks=(
          ('ck_access_roles_kind', "kind = 'builtin' OR kind = 'custom' OR kind = 'legacy'"),
          ('ck_access_roles_enabled', 'enabled = 0 OR enabled = 1'), ('ck_access_roles_version', 'version >= 1')),
          indexes=(('uq_access_roles_normalized_name', ('normalized_name',), True),))
    table('access_role_permissions', fields(('id', 'id'), ('role_id', 'id'), ('permission_id', 64)),
          foreign_keys=(fk('fk_access_role_permissions_role', 'role_id', 'access_roles.id'),
                        fk('fk_access_role_permissions_permission', 'permission_id', 'access_permissions.id')),
          indexes=(('uq_access_role_permissions_pair', ('role_id', 'permission_id'), True),
                   ('ix_access_role_permissions_permission', ('permission_id',), False)))
    shape = " OR ".join('(' + branch + ')' for branch in (
        "kind = 'installation' AND id = 'installation' AND parent_id IS NULL AND parent_kind IS NULL AND principal_id IS NULL AND mailbox_id IS NULL AND fax_job_id IS NULL AND inbound_fax_id IS NULL",
        "kind = 'legacy' AND id = 'legacy' AND parent_id IS NOT NULL AND parent_kind IS NOT NULL AND parent_id = 'installation' AND parent_kind = 'installation' AND principal_id IS NULL AND mailbox_id IS NULL AND fax_job_id IS NULL AND inbound_fax_id IS NULL",
        "kind = 'personal' AND parent_id IS NOT NULL AND parent_kind IS NOT NULL AND parent_id = 'installation' AND parent_kind = 'installation' AND principal_id IS NOT NULL AND mailbox_id IS NULL AND fax_job_id IS NULL AND inbound_fax_id IS NULL",
        "kind = 'mailbox' AND parent_id IS NOT NULL AND parent_kind IS NOT NULL AND parent_id = 'installation' AND parent_kind = 'installation' AND principal_id IS NULL AND mailbox_id IS NOT NULL AND fax_job_id IS NULL AND inbound_fax_id IS NULL",
        "kind = 'outbound' AND parent_id IS NOT NULL AND parent_kind IS NOT NULL AND (parent_kind = 'personal' OR parent_kind = 'legacy') AND principal_id IS NULL AND mailbox_id IS NULL AND fax_job_id IS NOT NULL AND inbound_fax_id IS NULL",
        "kind = 'inbound' AND parent_id IS NOT NULL AND parent_kind IS NOT NULL AND (parent_kind = 'mailbox' OR parent_kind = 'legacy') AND principal_id IS NULL AND mailbox_id IS NULL AND fax_job_id IS NULL AND inbound_fax_id IS NOT NULL",
    ))
    table('access_resources', fields(('id', 'id'), ('kind', 16), ('parent_id?', 'id'), ('parent_kind?', 16),
          ('principal_id?', 'id'), ('mailbox_id?', 'id'), ('fax_job_id?', 'id'), ('inbound_fax_id?', 'id'),
          ('enabled', 'int'), ('version', 'int'), *timestamps), checks=(
          ('ck_access_resources_enabled', 'enabled = 0 OR enabled = 1'),
          ('ck_access_resources_version', 'version >= 1'), ('ck_access_resources_shape', shape)),
          foreign_keys=(('fk_access_resources_parent', ['parent_id', 'parent_kind'], ['access_resources.id', 'access_resources.kind']),
                        fk('fk_access_resources_principal', 'principal_id', 'access_principals.id'),
                        fk('fk_access_resources_mailbox', 'mailbox_id', 'mailboxes.id'),
                        fk('fk_access_resources_fax_job', 'fax_job_id', 'fax_jobs.id'),
                        fk('fk_access_resources_inbound_fax', 'inbound_fax_id', 'inbound_faxes.id')),
          indexes=(('uq_access_resources_id_kind', ('id', 'kind'), True),
                   ('uq_access_resources_principal', ('principal_id',), True),
                   ('uq_access_resources_mailbox', ('mailbox_id',), True),
                   ('uq_access_resources_fax_job', ('fax_job_id',), True),
                   ('uq_access_resources_inbound_fax', ('inbound_fax_id',), True),
                   ('ix_access_resources_parent', ('parent_id',), False)))
    table('access_assignments', fields(('id', 'id'), ('principal_id?', 'id'), ('group_id?', 'id'),
          ('role_id', 'id'), ('resource_id', 'id'), ('version', 'int'), *timestamps), checks=(
          ('ck_access_assignments_subject', '(principal_id IS NOT NULL AND group_id IS NULL) OR (principal_id IS NULL AND group_id IS NOT NULL)'),
          ('ck_access_assignments_version', 'version >= 1')),
          foreign_keys=(fk('fk_access_assignments_principal', 'principal_id', 'access_principals.id'),
                        fk('fk_access_assignments_group', 'group_id', 'access_groups.id'),
                        fk('fk_access_assignments_role', 'role_id', 'access_roles.id'),
                        fk('fk_access_assignments_resource', 'resource_id', 'access_resources.id')),
          indexes=(('uq_access_assignments_principal', ('principal_id', 'role_id', 'resource_id'), True),
                   ('uq_access_assignments_group', ('group_id', 'role_id', 'resource_id'), True),
                   ('ix_access_assignments_role', ('role_id',), False),
                   ('ix_access_assignments_resource', ('resource_id',), False)))
    table('access_key_bindings', fields(('id', 'id'), ('principal_id', 'id'), ('state', 16),
          ('version', 'int'), ('security_version', 'int'), ('revoked_at?', 'time'), *timestamps), checks=(
          ('ck_access_key_bindings_state', "state = 'active' OR state = 'pending_review' OR state = 'revoked'"),
          ('ck_access_key_bindings_versions', 'version >= 1 AND security_version >= 1'),
          ('ck_access_key_bindings_revocation', "(state = 'revoked' AND revoked_at IS NOT NULL) OR (state <> 'revoked' AND revoked_at IS NULL)")),
          foreign_keys=(fk('fk_access_key_bindings_api_key', 'id', 'api_keys.id'),
                        fk('fk_access_key_bindings_principal', 'principal_id', 'access_principals.id')),
          indexes=(('ix_access_key_bindings_principal', ('principal_id',), False),
                   ('uq_access_key_bindings_id_principal', ('id', 'principal_id'), True)))
    table('access_key_grants', fields(('id', 'id'), ('key_binding_id', 'id'), ('permission_id', 64), ('resource_id', 'id')),
          foreign_keys=(fk('fk_access_key_grants_binding', 'key_binding_id', 'access_key_bindings.id'),
                        fk('fk_access_key_grants_permission', 'permission_id', 'access_permissions.id'),
                        fk('fk_access_key_grants_resource', 'resource_id', 'access_resources.id')),
          indexes=(('uq_access_key_grants_ceiling', ('key_binding_id', 'permission_id', 'resource_id'), True),
                   ('ix_access_key_grants_resource', ('resource_id',), False),
                   ('ix_access_key_grants_permission', ('permission_id',), False)))
    source = " OR ".join('(' + branch + ')' for branch in (
        "source_kind = 'password' AND bootstrap_fingerprint IS NULL AND source_key_id IS NULL AND source_key_version IS NULL AND password_version IS NOT NULL AND password_version >= 1 AND principal_id <> 'bootstrap'",
        "source_kind = 'key' AND bootstrap_fingerprint IS NULL AND source_key_id IS NOT NULL AND source_key_version IS NOT NULL AND source_key_version >= 1 AND password_version IS NULL AND principal_id <> 'bootstrap'",
        "source_kind = 'bootstrap' AND bootstrap_fingerprint IS NOT NULL AND source_key_id IS NULL AND source_key_version IS NULL AND password_version IS NULL AND principal_id = 'bootstrap'",
    ))
    table('access_sessions', fields(('id', 'id'), ('principal_id', 'id'), ('source_kind', 16),
          ('source_key_id?', 'id'), ('source_key_version?', 'int'), ('bootstrap_fingerprint?', 64),
          ('token_hash', 64), ('csrf_hash', 64), ('principal_security_version', 'int'),
          ('password_version?', 'int'), ('created_at', 'time'), ('last_used_at', 'time'),
          ('expires_at', 'time'), ('revoked_at?', 'time')), checks=(
          ('ck_access_sessions_versions', 'principal_security_version >= 1'),
          ('ck_access_sessions_times', 'created_at <= last_used_at AND last_used_at <= expires_at'),
          ('ck_access_sessions_source', source)),
          foreign_keys=(fk('fk_access_sessions_principal', 'principal_id', 'access_principals.id'),
                        ('fk_access_sessions_source_key', ['source_key_id', 'principal_id'], ['access_key_bindings.id', 'access_key_bindings.principal_id'])),
          indexes=(('uq_access_sessions_token_hash', ('token_hash',), True),
                   ('ix_access_sessions_principal', ('principal_id',), False),
                   ('ix_access_sessions_source_key', ('source_key_id',), False),
                   ('ix_access_sessions_expires_at', ('expires_at',), False)))
    table('access_audit', fields(('id', 'id'), ('actor_principal_id?', 'id'), ('actor_key_binding_id?', 'id'),
          ('actor_session_id?', 'id'), ('operation', 64), ('target_kind', 32), ('target_id?', 100),
          ('policy_version_before', 'int'), ('policy_version_after', 'int'), ('outcome', 16),
          ('details', 'text'), ('created_at', 'time')), checks=(
          ('ck_access_audit_versions', 'policy_version_before >= 0 AND policy_version_after >= policy_version_before'),
          ('ck_access_audit_outcome', "outcome = 'allowed' OR outcome = 'denied' OR outcome = 'migrated'"),
          ('ck_access_audit_actor', 'actor_principal_id IS NOT NULL OR (actor_key_binding_id IS NULL AND actor_session_id IS NULL)')),
          foreign_keys=(fk('fk_access_audit_principal', 'actor_principal_id', 'access_principals.id'),
                        fk('fk_access_audit_key_binding', 'actor_key_binding_id', 'access_key_bindings.id'),
                        fk('fk_access_audit_session', 'actor_session_id', 'access_sessions.id')),
          indexes=(('ix_access_audit_created_at', ('created_at',), False),
                   ('ix_access_audit_actor_created', ('actor_principal_id', 'created_at'), False),
                   ('ix_access_audit_target', ('target_kind', 'target_id'), False)))
    table('access_mailbox_routes', fields(('id', 'id'), ('mailbox_id', 'id'), ('version', 'int'), *timestamps),
          checks=(('ck_access_mailbox_routes_version', 'version >= 1'),),
          foreign_keys=(fk('fk_access_mailbox_routes_rule', 'id', 'inbound_rules.id'),
                        fk('fk_access_mailbox_routes_mailbox', 'mailbox_id', 'mailboxes.id')),
          indexes=(('ix_access_mailbox_routes_mailbox', ('mailbox_id',), False),))
    return metadata


def upgrade_access(connection, operations):
    metadata = frozen_metadata(dialect=connection.dialect.name)
    for table in metadata.sorted_tables:
        if table.name in TABLES:
            table.create(connection)
    if connection.dialect.name == 'postgresql':
        # PostgreSQL requires the composite self-reference's unique index to
        # exist before the FK. Table.create builds its indexes after CREATE TABLE.
        parent = next(constraint for constraint in metadata.tables['access_resources'].foreign_key_constraints
                      if constraint.name == 'fk_access_resources_parent')
        connection.execute(sa.schema.AddConstraint(parent))
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def insert(table, **values):
        connection.execute(metadata.tables[table].insert().values(**values))

    def principal(identity, kind, display):
        insert('access_principals', id=identity, kind=kind, display_name=display, enabled=1,
               security_version=1, version=1, created_at=now, updated_at=now)

    def resource(identity, kind, parent_id=None, parent_kind=None, **links):
        insert('access_resources', id=identity, kind=kind, parent_id=parent_id, parent_kind=parent_kind,
               enabled=1, version=1, created_at=now, updated_at=now, **links)

    def audit(identity, target_kind, target_id, details):
        insert('access_audit', id=identity, actor_principal_id=None, actor_key_binding_id=None,
               actor_session_id=None, operation='access_migration', target_kind=target_kind, target_id=target_id,
               policy_version_before=0, policy_version_after=1, outcome='migrated',
               details=json.dumps(details, ensure_ascii=True, separators=(',', ':')), created_at=now)

    for permission, description in PERMISSIONS.items():
        insert('access_permissions', id=permission, description=description)
    for identity, name, normalized, description, permissions in BUILTIN_ROLES:
        insert('access_roles', id=identity, name=name, normalized_name=normalized, description=description,
               kind='builtin', enabled=1, version=1, created_at=now, updated_at=now)
        for permission in permissions:
            insert('access_role_permissions', id=_identity('role_permission', identity, permission),
                   role_id=identity, permission_id=permission)
    principal('bootstrap', 'bootstrap', 'Installation bootstrap')
    resource('installation', 'installation')
    resource('legacy', 'legacy', 'installation', 'installation')
    resource(_identity('personal_resource', 'bootstrap'), 'personal', 'installation', 'installation', principal_id='bootstrap')
    insert('access_state', id='state', policy_version=1, catalogue_version=1, updated_at=now)

    counts = dict(keys_total=0, unknown_scopes=0, wildcard_keys=0, reserved_keys=0,
                  inbound_document_narrowed_keys=0, mailbox_resources=0, mailbox_routes_bound=0,
                  mailbox_routes_unmatched=0, outbound_personal=0, outbound_legacy=0, inbound_legacy=0)
    keys = metadata.tables['api_keys']
    # Owner/name/hash/note are not selected and cannot enter identity/audit data.
    rows = connection.execute(sa.select(keys.c.id, keys.c.key_id, keys.c.scopes, keys.c.revoked_at)
                              .order_by(keys.c.id).execution_options(stream_results=True))
    try:
        for row in rows:
            principal_id = _identity('key_principal', row.id)
            token_safe = bool(re.fullmatch(r'[A-Za-z0-9_-]{1,32}', row.key_id))
            display = 'Legacy integration ' + (row.key_id if token_safe else principal_id[:12])
            principal(principal_id, 'integration', display)
            resource(_identity('personal_resource', principal_id), 'personal', 'installation', 'installation', principal_id=principal_id)
            scopes = {scope.strip() for scope in (row.scopes or '').split(',') if scope.strip()}
            recognized = sorted(scopes & LEGACY_PERMISSIONS)
            wildcard, reserved = '*' in scopes, row.key_id == 'env'
            unknown = len(scopes - LEGACY_PERMISSIONS - {'*'})
            state = 'revoked' if row.revoked_at is not None else 'pending_review' if wildcard or reserved else 'active'
            insert('access_key_bindings', id=row.id, principal_id=principal_id, state=state,
                   version=1, security_version=1, revoked_at=row.revoked_at, created_at=now, updated_at=now)
            if recognized and not wildcard and not reserved:
                role_id = _identity('legacy_role', row.id)
                # The reserved legacy namespace is identity-derived, not mutable labels.
                role_name = 'Legacy integration ' + principal_id
                insert('access_roles', id=role_id, name=role_name, normalized_name=role_name.lower(),
                       description='Frozen recognized legacy key permissions', kind='legacy', enabled=1,
                       version=1, created_at=now, updated_at=now)
                insert('access_assignments', id=_identity('assignment', principal_id, role_id, 'installation'),
                       principal_id=principal_id, group_id=None, role_id=role_id, resource_id='installation',
                       version=1, created_at=now, updated_at=now)
                for permission in recognized:
                    insert('access_role_permissions', id=_identity('role_permission', role_id, permission), role_id=role_id, permission_id=permission)
                    insert('access_key_grants', id=_identity('key_grant', row.id, permission, 'installation'),
                           key_binding_id=row.id, permission_id=permission, resource_id='installation')
            narrowed = 'inbound:read' in scopes
            reason = 'reserved_identity' if reserved else 'wildcard_review' if wildcard else 'recognized_scopes' if recognized else 'no_recognized_scopes'
            audit(_identity('key_migration_audit', row.id), 'key_binding', row.id,
                  dict(binding_state=state, reason=reason, recognized_permissions=len(recognized),
                       unknown_scopes=unknown, wildcard_review=wildcard, reserved_identity=reserved,
                       inbound_document_narrowed=narrowed))
            counts['keys_total'] += 1
            counts['unknown_scopes'] += unknown
            counts['wildcard_keys'] += int(wildcard)
            counts['reserved_keys'] += int(reserved)
            counts['inbound_document_narrowed_keys'] += int(narrowed)
    finally:
        rows.close()

    # Exact equality on the immutable replay token proves personal attribution.
    # Reserved bootstrap identity is excluded even if a DB key has that key_id.
    jobs, deliveries = metadata.tables['fax_jobs'], metadata.tables['outbound_deliveries']
    rows = connection.execute(sa.select(jobs.c.id, keys.c.id.label('key_id'))
        .select_from(jobs.outerjoin(deliveries, jobs.c.id == deliveries.c.id)
                     .outerjoin(keys, sa.and_(deliveries.c.principal_scope == sa.literal('key:') + keys.c.key_id,
                                             keys.c.key_id != 'env')))
        .order_by(jobs.c.id).execution_options(stream_results=True))
    try:
        for row in rows:
            parent = _identity('personal_resource', _identity('key_principal', row.key_id)) if row.key_id is not None else 'legacy'
            resource(_identity('outbound_resource', row.id), 'outbound', parent,
                     'personal' if row.key_id is not None else 'legacy', fax_job_id=row.id)
            counts['outbound_personal' if row.key_id is not None else 'outbound_legacy'] += 1
    finally:
        rows.close()
    for table_name, link, kind in [('mailboxes', 'mailbox_id', 'mailbox'), ('inbound_faxes', 'inbound_fax_id', 'inbound')]:
        table = metadata.tables[table_name]
        rows = connection.execute(sa.select(table.c.id).order_by(table.c.id).execution_options(stream_results=True))
        try:
            for identity in rows.scalars():
                resource(_identity(kind + '_resource', identity), kind,
                         'installation' if kind == 'mailbox' else 'legacy',
                         'installation' if kind == 'mailbox' else 'legacy', **{link: identity})
                counts['mailbox_resources' if kind == 'mailbox' else 'inbound_legacy'] += 1
        finally:
            rows.close()
    rules, mailboxes = metadata.tables['inbound_rules'], metadata.tables['mailboxes']
    rows = connection.execute(sa.select(rules.c.id, mailboxes.c.id.label('mailbox_id'))
        .select_from(rules.outerjoin(mailboxes, rules.c.mailbox_label == mailboxes.c.label))
        .order_by(rules.c.id).execution_options(stream_results=True))
    try:
        for row in rows:
            if row.mailbox_id is None:
                counts['mailbox_routes_unmatched'] += 1
            else:
                insert('access_mailbox_routes', id=row.id, mailbox_id=row.mailbox_id, version=1,
                       created_at=now, updated_at=now)
                counts['mailbox_routes_bound'] += 1
    finally:
        rows.close()
    audit(_identity('installation_migration_audit', REVISION), 'installation', 'installation', counts)
