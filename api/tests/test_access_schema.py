"""Frozen access migration contract, exercised on isolated real databases."""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app.schema import API_DIRECTORY, SchemaUpgradeError, upgrade_schema, validate_schema
from api.tests.test_schema import database, snapshot, schema_description
from api.tests.test_outbound_schema import add_job


PRIOR = '0004_outbound_delivery'
HEAD = '0005_access_control'
OLD = datetime(2025, 1, 2, 3, 4, 5, 123456)
NAMESPACE = uuid.UUID('f56f5b6b-2485-4af6-8840-a535b333a28c')
TABLES = {
    'access_state', 'access_principals', 'access_users', 'access_groups',
    'access_memberships', 'access_permissions', 'access_roles',
    'access_role_permissions', 'access_resources', 'access_assignments',
    'access_key_bindings', 'access_key_grants', 'access_sessions',
    'access_audit', 'access_mailbox_routes',
}
PERMISSIONS = set('fax:send fax:read fax:document fax:refresh fax:reconcile '
    'inbound:list inbound:read inbound:document keys:manage users:read users:manage '
    'groups:read groups:manage roles:read roles:manage grants:read grants:manage '
    'sessions:read sessions:revoke settings:read settings:write providers:read '
    'providers:write providers:install diagnostics:read logs:read audit:read '
    'tunnels:read tunnels:manage tunnels:pair host:restart host:actions '
    'host:terminal owner:recover mailboxes:read mailboxes:manage'.split())


def at_revision(engine, revision=PRIOR):
    config = Config(str(API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.upgrade(config, revision)


def migrated_id(domain, *parts):
    return uuid.uuid5(NAMESPACE, json.dumps([domain, *parts], ensure_ascii=True, separators=(',', ':'))).hex


def test_clean_access_upgrade_enrolls_exact_catalogue_without_owner_or_sessions(database, monkeypatch):
    monkeypatch.setenv('FAXBOT_API_KEY', 'must-not-be-read-or-persisted')
    upgrade_schema(database)
    rows = snapshot(database)
    assert TABLES <= rows.keys()
    assert rows['alembic_version'] == [{'version_num': HEAD}]
    assert {r['id'] for r in rows['access_permissions']} == PERMISSIONS
    assert len(PERMISSIONS) == 36
    assert {r['id'] for r in rows['access_roles']} == {
        'role_owner', 'role_administrator', 'role_fax_operator',
        'role_fax_viewer', 'role_auditor', 'role_host_operator',
    }
    memberships = {}
    for row in rows['access_role_permissions']:
        memberships.setdefault(row['role_id'], set()).add(row['permission_id'])
    assert memberships == {
        'role_owner': PERMISSIONS,
        'role_administrator': PERMISSIONS - {'host:restart', 'host:actions', 'host:terminal', 'owner:recover'},
        'role_fax_operator': {'fax:send', 'fax:read', 'fax:document', 'fax:refresh', 'inbound:list', 'inbound:read', 'inbound:document'},
        'role_fax_viewer': {'fax:read', 'fax:document', 'inbound:list', 'inbound:read', 'inbound:document'},
        'role_auditor': {'audit:read', 'fax:read', 'inbound:list', 'inbound:read'},
        'role_host_operator': {'host:restart', 'host:actions', 'host:terminal', 'diagnostics:read', 'settings:read'},
    }
    assert rows['access_principals'][0]['id'] == 'bootstrap'
    assert rows['access_principals'][0]['kind'] == 'bootstrap'
    for table in ['access_users', 'access_groups', 'access_memberships', 'access_assignments',
                  'access_key_bindings', 'access_key_grants', 'access_sessions', 'access_mailbox_routes']:
        assert rows[table] == []
    resources = {r['id']: r for r in rows['access_resources']}
    assert resources['installation']['parent_id'] is None
    assert resources['legacy']['parent_id'] == 'installation'
    assert len(resources) == 3
    assert next(r for r in resources.values() if r['kind'] == 'personal')['principal_id'] == 'bootstrap'
    assert rows['access_state'][0]['policy_version'] == rows['access_state'][0]['catalogue_version'] == 1
    assert 'must-not-be-read-or-persisted' not in repr(rows)
    with database.connect() as connection:
        assert validate_schema(connection, require_version=True) == HEAD
    upgrade_schema(database)
    assert snapshot(database) == rows


def add_key(connection, identity, token, scopes, *, revoked=None, expired=None):
    connection.execute(sa.text('''
        INSERT INTO api_keys (id,key_id,key_hash,name,owner,scopes,created_at,expires_at,revoked_at,note)
        VALUES (:id,:token,'unchanged-hash','misleading name','same owner',:scopes,:now,:expiry,:revoked,'private note')
    '''), {'id': identity, 'token': token, 'scopes': scopes, 'now': OLD, 'expiry': expired, 'revoked': revoked})


def test_0004_enrollment_preserves_all_rows_and_narrows_unproven_authority(database):
    at_revision(database)
    cases = [
        ('db-alpha', 'token-alpha', 'fax:read,fax:send,fax:read, inbound:read, keys:manage', None, None),
        ('db-beta', 'token-beta', 'fax:read,unknown-private-scope, fax:document,FAX:READ', None, None),
        ('db-empty', 'token-empty', ' , ', None, None),
        ('db-wild', 'token-wild', 'fax:send,*', None, None),
        ('db-env', 'env', 'fax:read', None, None),
        ('db-revoked', 'token-revoked', 'fax:read,*', OLD, None),
        ('db-expired', 'token-expired', 'fax:read', None, OLD),
        ('db-unsafe', 'line\nbreak', 'inbound:list', None, None),
    ]
    with database.begin() as conn:
        for identity, token, scopes, revoked, expired in cases:
            add_key(conn, identity, token, scopes, revoked=revoked, expired=expired)
        for identity, scope in [('owned', 'key:token-alpha'), ('wild-owned', 'key:token-wild'),
                                ('wrong-db-id', 'key:db-alpha'), ('reserved', 'key:env'),
                                ('unknown', 'key:unmatched'), ('unbound', None)]:
            add_job(conn, identity, 'queued')
            conn.execute(sa.text('''INSERT INTO outbound_deliveries
                (id,dispatch_mode,state,version,principal_scope,request_fingerprint,idempotency_digest,created_at,updated_at)
                VALUES (:id,'normal','failed',3,:scope,'fingerprint','digest',:now,:now)'''),
                {'id': identity, 'scope': scope, 'now': OLD})
        conn.execute(sa.text("INSERT INTO mailboxes (id,label,allowed_scopes,created_at,updated_at) VALUES ('mailbox','Shared','*',:now,:now)"), {'now': OLD})
        conn.execute(sa.text("INSERT INTO inbound_rules (id,to_number,mailbox_label,created_at) VALUES ('match','+12025550123','Shared',:now), ('unmatch','+12025550124','shared',:now)"), {'now': OLD})
        conn.execute(sa.text("INSERT INTO inbound_faxes (id,status,backend,mailbox_label,created_at,received_at,updated_at) VALUES ('inbound','received','sip','Shared',:now,:now,:now)"), {'now': OLD})
    before = snapshot(database)
    upgrade_schema(database)
    after = snapshot(database)
    for name, rows in before.items():
        if name != 'alembic_version':
            assert after[name] == rows
    bindings = {r['id']: r for r in after['access_key_bindings']}
    assert set(bindings) == {case[0] for case in cases}
    assert len({r['principal_id'] for r in bindings.values()}) == len(cases)
    assert {k: v['state'] for k, v in bindings.items()} == {
        'db-alpha': 'active', 'db-beta': 'active', 'db-empty': 'active',
        'db-wild': 'pending_review', 'db-env': 'pending_review', 'db-revoked': 'revoked',
        'db-expired': 'active', 'db-unsafe': 'active',
    }
    grants = {}
    for row in after['access_key_grants']:
        assert row['resource_id'] == 'installation'
        grants.setdefault(row['key_binding_id'], set()).add(row['permission_id'])
    assert grants == {'db-alpha': {'fax:read', 'fax:send', 'inbound:read', 'keys:manage'},
                      'db-beta': {'fax:read'}, 'db-expired': {'fax:read'}, 'db-unsafe': {'inbound:list'}}
    role_permissions = {}
    for row in after['access_role_permissions']:
        role_permissions.setdefault(row['role_id'], set()).add(row['permission_id'])
    assignments = after['access_assignments']
    assert len(assignments) == len(grants)
    for assignment in assignments:
        key = next(k for k, v in bindings.items() if v['principal_id'] == assignment['principal_id'])
        assert assignment['group_id'] is None
        assert assignment['resource_id'] == 'installation'
        assert role_permissions[assignment['role_id']] == grants[key]
    resources = after['access_resources']
    personal = {r['principal_id']: r['id'] for r in resources if r['kind'] == 'personal'}
    outbound = {r['fax_job_id']: r['parent_id'] for r in resources if r['kind'] == 'outbound'}
    assert outbound == {'owned': personal[bindings['db-alpha']['principal_id']],
                        'wild-owned': personal[bindings['db-wild']['principal_id']],
                        'wrong-db-id': 'legacy', 'reserved': 'legacy', 'unknown': 'legacy', 'unbound': 'legacy'}
    assert next(r for r in resources if r['kind'] == 'inbound')['parent_id'] == 'legacy'
    assert [(r['id'], r['mailbox_id']) for r in after['access_mailbox_routes']] == [('match', 'mailbox')]
    audits = [json.loads(r['details']) for r in after['access_audit']]
    assert len(audits) == len(cases) + 1
    audit_text = repr(audits)
    for secret in ['unknown-private-scope', 'same owner', 'unchanged-hash', 'private note', '+12025550123', 'fingerprint']:
        assert secret not in audit_text
    summary = next(details for details in audits if 'keys_total' in details)
    assert summary['keys_total'] == 8
    assert summary['unknown_scopes'] == 3
    assert summary['wildcard_keys'] == 2
    assert summary['reserved_keys'] == 1
    assert summary['inbound_document_narrowed_keys'] == 1
    assert summary['mailbox_routes_unmatched'] == 1
    unsafe_principal = next(r for r in after['access_principals'] if r['id'] == bindings['db-unsafe']['principal_id'])
    assert '\n' not in unsafe_principal['display_name']
    upgrade_schema(database)
    assert snapshot(database) == after


# Independent contract literals; deliberately do not build expected structure
# from the production metadata that the migration itself consumes.
COLUMNS = {
    'access_state': 'id:S40 policy_version:I catalogue_version:I updated_at:T',
    'access_principals': 'id:S40 kind:S16 display_name:S200 enabled:I security_version:I version:I created_at:T updated_at:T',
    'access_users': 'id:S40 login:S100 normalized_login:S100 password_hash:S256 password_change_required:I password_version:I created_at:T updated_at:T',
    'access_groups': 'id:S40 name:S100 normalized_name:S100 description:S500 enabled:I version:I created_at:T updated_at:T',
    'access_memberships': 'id:S40 group_id:S40 principal_id:S40 version:I created_at:T updated_at:T',
    'access_permissions': 'id:S64 description:S200',
    'access_roles': 'id:S40 name:S100 normalized_name:S100 description:S500 kind:S16 enabled:I version:I created_at:T updated_at:T',
    'access_role_permissions': 'id:S40 role_id:S40 permission_id:S64',
    'access_resources': 'id:S40 kind:S16 parent_id:S40? parent_kind:S16? principal_id:S40? mailbox_id:S40? fax_job_id:S40? inbound_fax_id:S40? enabled:I version:I created_at:T updated_at:T',
    'access_assignments': 'id:S40 principal_id:S40? group_id:S40? role_id:S40 resource_id:S40 version:I created_at:T updated_at:T',
    'access_key_bindings': 'id:S40 principal_id:S40 state:S16 version:I security_version:I revoked_at:T? created_at:T updated_at:T',
    'access_key_grants': 'id:S40 key_binding_id:S40 permission_id:S64 resource_id:S40',
    'access_sessions': 'id:S40 principal_id:S40 source_kind:S16 source_key_id:S40? source_key_version:I? bootstrap_fingerprint:S64? token_hash:S64 csrf_hash:S64 principal_security_version:I password_version:I? created_at:T last_used_at:T expires_at:T revoked_at:T?',
    'access_audit': 'id:S40 actor_principal_id:S40? actor_key_binding_id:S40? actor_session_id:S40? operation:S64 target_kind:S32 target_id:S100? policy_version_before:I policy_version_after:I outcome:S16 details:X created_at:T',
    'access_mailbox_routes': 'id:S40 mailbox_id:S40 version:I created_at:T updated_at:T',
}
CHECKS = {
    'access_state': 'singleton versions', 'access_principals': 'kind enabled versions bootstrap',
    'access_users': 'password_change password_version', 'access_groups': 'enabled version',
    'access_memberships': 'version', 'access_permissions': '', 'access_roles': 'kind enabled version',
    'access_role_permissions': '', 'access_resources': 'enabled version shape',
    'access_assignments': 'subject version', 'access_key_bindings': 'state versions revocation',
    'access_key_grants': '', 'access_sessions': 'versions times source',
    'access_audit': 'versions outcome actor', 'access_mailbox_routes': 'version',
}
INDEXES = {
    'access_state': {},
    'access_principals': {'ix_access_principals_kind_enabled': (('kind', 'enabled'), False)},
    'access_users': {'uq_access_users_normalized_login': (('normalized_login',), True)},
    'access_groups': {'uq_access_groups_normalized_name': (('normalized_name',), True)},
    'access_memberships': {'uq_access_memberships_group_principal': (('group_id', 'principal_id'), True), 'ix_access_memberships_principal': (('principal_id',), False)},
    'access_permissions': {},
    'access_roles': {'uq_access_roles_normalized_name': (('normalized_name',), True)},
    'access_role_permissions': {'uq_access_role_permissions_pair': (('role_id', 'permission_id'), True), 'ix_access_role_permissions_permission': (('permission_id',), False)},
    'access_resources': {'uq_access_resources_id_kind': (('id', 'kind'), True), 'uq_access_resources_principal': (('principal_id',), True), 'uq_access_resources_mailbox': (('mailbox_id',), True), 'uq_access_resources_fax_job': (('fax_job_id',), True), 'uq_access_resources_inbound_fax': (('inbound_fax_id',), True), 'ix_access_resources_parent': (('parent_id',), False)},
    'access_assignments': {'uq_access_assignments_principal': (('principal_id', 'role_id', 'resource_id'), True), 'uq_access_assignments_group': (('group_id', 'role_id', 'resource_id'), True), 'ix_access_assignments_role': (('role_id',), False), 'ix_access_assignments_resource': (('resource_id',), False)},
    'access_key_bindings': {'ix_access_key_bindings_principal': (('principal_id',), False), 'uq_access_key_bindings_id_principal': (('id', 'principal_id'), True)},
    'access_key_grants': {'uq_access_key_grants_ceiling': (('key_binding_id', 'permission_id', 'resource_id'), True), 'ix_access_key_grants_resource': (('resource_id',), False), 'ix_access_key_grants_permission': (('permission_id',), False)},
    'access_sessions': {'uq_access_sessions_token_hash': (('token_hash',), True), 'ix_access_sessions_principal': (('principal_id',), False), 'ix_access_sessions_source_key': (('source_key_id',), False), 'ix_access_sessions_expires_at': (('expires_at',), False)},
    'access_audit': {'ix_access_audit_created_at': (('created_at',), False), 'ix_access_audit_actor_created': (('actor_principal_id', 'created_at'), False), 'ix_access_audit_target': (('target_kind', 'target_id'), False)},
    'access_mailbox_routes': {'ix_access_mailbox_routes_mailbox': (('mailbox_id',), False)},
}
FOREIGN_KEYS = {
    'access_state': {}, 'access_principals': {}, 'access_groups': {}, 'access_permissions': {}, 'access_roles': {},
    'access_users': {'principal': (('id',), 'access_principals', ('id',))},
    'access_memberships': {'group': (('group_id',), 'access_groups', ('id',)), 'principal': (('principal_id',), 'access_principals', ('id',))},
    'access_role_permissions': {'role': (('role_id',), 'access_roles', ('id',)), 'permission': (('permission_id',), 'access_permissions', ('id',))},
    'access_resources': {'parent': (('parent_id', 'parent_kind'), 'access_resources', ('id', 'kind')), 'principal': (('principal_id',), 'access_principals', ('id',)), 'mailbox': (('mailbox_id',), 'mailboxes', ('id',)), 'fax_job': (('fax_job_id',), 'fax_jobs', ('id',)), 'inbound_fax': (('inbound_fax_id',), 'inbound_faxes', ('id',))},
    'access_assignments': {'principal': (('principal_id',), 'access_principals', ('id',)), 'group': (('group_id',), 'access_groups', ('id',)), 'role': (('role_id',), 'access_roles', ('id',)), 'resource': (('resource_id',), 'access_resources', ('id',))},
    'access_key_bindings': {'api_key': (('id',), 'api_keys', ('id',)), 'principal': (('principal_id',), 'access_principals', ('id',))},
    'access_key_grants': {'binding': (('key_binding_id',), 'access_key_bindings', ('id',)), 'permission': (('permission_id',), 'access_permissions', ('id',)), 'resource': (('resource_id',), 'access_resources', ('id',))},
    'access_sessions': {'principal': (('principal_id',), 'access_principals', ('id',)), 'source_key': (('source_key_id', 'principal_id'), 'access_key_bindings', ('id', 'principal_id'))},
    'access_audit': {'principal': (('actor_principal_id',), 'access_principals', ('id',)), 'key_binding': (('actor_key_binding_id',), 'access_key_bindings', ('id',)), 'session': (('actor_session_id',), 'access_sessions', ('id',))},
    'access_mailbox_routes': {'rule': (('id',), 'inbound_rules', ('id',)), 'mailbox': (('mailbox_id',), 'mailboxes', ('id',))},
}


def test_exact_access_structure_and_reflected_frozen_constraints(database):
    upgrade_schema(database)
    with database.connect() as conn:
        inspector = sa.inspect(conn)
        assert {name for name in inspector.get_table_names() if name.startswith('access_')} == TABLES
        for table, contract in COLUMNS.items():
            expected = dict(field.split(':') for field in contract.split())
            columns = inspector.get_columns(table)
            assert {c['name'] for c in columns} == set(expected)
            for column in columns:
                shape = expected[column['name']]
                assert column['nullable'] == shape.endswith('?')
                shape = shape.rstrip('?')
                kind = sa.String if shape.startswith('S') else {'I': sa.Integer, 'T': sa.DateTime, 'X': sa.Text}[shape]
                assert isinstance(column['type'], sa.Text) if kind is sa.Text else column['type']._type_affinity == kind
                assert getattr(column['type'], 'length', None) == (int(shape[1:]) if shape.startswith('S') else None)
                assert not getattr(column['type'], 'timezone', False)
                assert column.get('default') is None
                assert not column.get('identity') and not column.get('computed')
            pk = inspector.get_pk_constraint(table)
            assert pk['constrained_columns'] == ['id'] and pk['name'] == 'pk_' + table
            assert {c['name'] for c in inspector.get_check_constraints(table)} == {'ck_' + table + '_' + suffix for suffix in CHECKS[table].split()}
            assert not inspector.get_unique_constraints(table)
            assert {i['name']: (tuple(i['column_names']), bool(i['unique'])) for i in inspector.get_indexes(table)} == INDEXES[table]
            expected_fks = {'fk_' + table + '_' + suffix: (*shape, 'RESTRICT') for suffix, shape in FOREIGN_KEYS[table].items()}
            assert {fk['name']: (tuple(fk['constrained_columns']), fk['referred_table'], tuple(fk['referred_columns']), fk.get('options', {}).get('ondelete')) for fk in inspector.get_foreign_keys(table)} == expected_fks


def insert_row(connection, table_name, **values):
    table = sa.Table(table_name, sa.MetaData(), autoload_with=connection)
    connection.execute(table.insert().values(**values))


def add_principal(connection, identity, kind='user'):
    insert_row(connection, 'access_principals', id=identity, kind=kind, display_name=identity,
               enabled=1, security_version=1, version=1, created_at=OLD, updated_at=OLD)


def add_resource(connection, identity='candidate', **overrides):
    values = dict(id=identity, kind='outbound', parent_id='legacy', parent_kind='legacy',
                  fax_job_id='new-job', enabled=1, version=1, created_at=OLD, updated_at=OLD)
    values.update(overrides)
    insert_row(connection, 'access_resources', **values)


@pytest.mark.parametrize('overrides', [
    {'parent_id': None}, {'parent_kind': None}, {'parent_id': None, 'parent_kind': None},
    {'parent_id': 'installation', 'parent_kind': 'personal'}, {'parent_id': 'legacy', 'parent_kind': 'personal'},
    {'fax_job_id': None}, {'fax_job_id': 'missing'}, {'principal_id': 'bootstrap'}, {'parent_id': 'candidate'},
    {'kind': 'personal', 'parent_id': None, 'parent_kind': 'installation', 'principal_id': 'new-user', 'fax_job_id': None},
    {'kind': 'personal', 'parent_id': 'installation', 'parent_kind': None, 'principal_id': 'new-user', 'fax_job_id': None},
    {'kind': 'mailbox', 'parent_id': None, 'parent_kind': None, 'mailbox_id': 'new-mailbox', 'fax_job_id': None},
    {'kind': 'legacy', 'parent_id': None, 'parent_kind': None, 'fax_job_id': None},
    {'kind': 'installation', 'parent_id': None, 'parent_kind': None, 'fax_job_id': None},
    {'kind': 'inbound', 'parent_id': 'legacy', 'parent_kind': 'legacy', 'fax_job_id': None, 'inbound_fax_id': None},
    {'kind': 'inbound', 'parent_id': None, 'parent_kind': 'legacy', 'fax_job_id': None, 'inbound_fax_id': 'new-inbound'},
    {'kind': 'outbound', 'parent_id': 'new-mailbox-resource', 'parent_kind': 'mailbox'},
])
def test_resource_shape_fk_and_null_unknown_bypasses_are_rejected(database, overrides):
    upgrade_schema(database)
    with database.begin() as conn:
        add_principal(conn, 'new-user')
        add_job(conn, 'new-job', 'queued')
        insert_row(conn, 'mailboxes', id='new-mailbox', label='box', created_at=OLD, updated_at=OLD)
        insert_row(conn, 'inbound_faxes', id='new-inbound', status='received', backend='sip', created_at=OLD, received_at=OLD, updated_at=OLD)
        add_resource(conn, 'new-mailbox-resource', kind='mailbox', parent_id='installation', parent_kind='installation', fax_job_id=None, mailbox_id='new-mailbox')
    with pytest.raises(sa.exc.IntegrityError), database.begin() as conn:
        add_resource(conn, **overrides)


def test_resource_nullable_uniqueness_allows_valid_shapes_and_restricts_identity_deletion(database):
    upgrade_schema(database)
    with database.begin() as conn:
        add_principal(conn, 'person')
        add_principal(conn, 'other')
        add_resource(conn, 'person-container', kind='personal', parent_id='installation', parent_kind='installation', fax_job_id=None, principal_id='person')
        add_resource(conn, 'other-container', kind='personal', parent_id='installation', parent_kind='installation', fax_job_id=None, principal_id='other')
        add_job(conn, 'new-job', 'queued')
        add_resource(conn, parent_id='person-container', parent_kind='personal')
    for statement in ["DELETE FROM fax_jobs WHERE id='new-job'", "DELETE FROM access_principals WHERE id='person'",
                      "UPDATE access_resources SET parent_id='candidate',parent_kind='outbound' WHERE id='person-container'"]:
        with pytest.raises(sa.exc.IntegrityError), database.begin() as conn:
            conn.exec_driver_sql(statement)
    with pytest.raises(sa.exc.IntegrityError), database.begin() as conn:
        add_resource(conn, 'duplicate-personal', kind='personal', parent_id='installation', parent_kind='installation', fax_job_id=None, principal_id='person')


@pytest.mark.parametrize('subject', [{}, {'principal_id': 'bootstrap', 'group_id': 'group'}])
def test_assignment_subject_requires_exactly_one_principal_or_group(database, subject):
    upgrade_schema(database)
    with database.begin() as conn:
        insert_row(conn, 'access_groups', id='group', name='Group', normalized_name='group', description='', enabled=1, version=1, created_at=OLD, updated_at=OLD)
    with pytest.raises(sa.exc.IntegrityError), database.begin() as conn:
        insert_row(conn, 'access_assignments', id='assignment', role_id='role_fax_viewer', resource_id='installation', version=1, created_at=OLD, updated_at=OLD, **subject)


@pytest.mark.parametrize('table,values', [
    ('access_groups', dict(name='Another', normalized_name='collision', description='', enabled=1, version=1)),
    ('access_roles', dict(name='Another', normalized_name='collision', description='', kind='custom', enabled=1, version=1)),
    ('access_users', dict(login='Another', normalized_login='collision', password_hash='synthetic', password_change_required=1, password_version=1)),
])
def test_stored_normalized_names_remain_unique(database, table, values):
    upgrade_schema(database)
    with database.begin() as conn:
        add_principal(conn, 'first')
        add_principal(conn, 'second')
        insert_row(conn, table, id='first', created_at=OLD, updated_at=OLD, **values)
    with pytest.raises(sa.exc.IntegrityError), database.begin() as conn:
        insert_row(conn, table, id='second', created_at=OLD, updated_at=OLD, **values)


def session_values(**overrides):
    values = dict(id='session', principal_id='user', source_kind='password', password_version=1,
        token_hash='synthetic-token-hash', csrf_hash='synthetic-csrf-hash', principal_security_version=1,
        created_at=OLD, last_used_at=OLD, expires_at=OLD)
    values.update(overrides)
    return values


@pytest.mark.parametrize('overrides', [
    {'password_version': None}, {'password_version': 0}, {'source_kind': 'unexpected'},
    {'source_kind': 'password', 'bootstrap_fingerprint': 'fingerprint'},
    {'source_kind': 'key', 'password_version': None, 'source_key_id': None, 'source_key_version': 1},
    {'source_kind': 'key', 'password_version': None, 'source_key_id': 'db-key', 'source_key_version': None},
    {'source_kind': 'key', 'password_version': None, 'source_key_id': 'db-key', 'source_key_version': 0},
    {'source_kind': 'key', 'password_version': None, 'source_key_id': 'db-key', 'source_key_version': 1},
    {'source_kind': 'bootstrap', 'password_version': None, 'principal_id': 'bootstrap'},
    {'source_kind': 'bootstrap', 'password_version': None, 'bootstrap_fingerprint': 'fingerprint'},
    {'source_kind': 'password', 'principal_id': 'bootstrap'},
    {'source_kind': 'bootstrap', 'password_version': None, 'principal_id': 'bootstrap', 'bootstrap_fingerprint': 'fingerprint', 'source_key_version': 1},
    {'principal_security_version': 0}, {'last_used_at': datetime(2025, 1, 1)},
    {'expires_at': datetime(2025, 1, 1)},
])
def test_session_source_shape_versions_times_and_cross_principal_keys_are_rejected(database, overrides):
    at_revision(database)
    with database.begin() as conn:
        add_key(conn, 'db-key', 'token', 'fax:read')
    upgrade_schema(database)
    with database.begin() as conn:
        add_principal(conn, 'user')
    with pytest.raises(sa.exc.IntegrityError), database.begin() as conn:
        insert_row(conn, 'access_sessions', **session_values(**overrides))


def test_session_password_key_and_bootstrap_nullable_branches_are_valid(database):
    at_revision(database)
    with database.begin() as conn:
        add_key(conn, 'db-key', 'token', 'fax:read')
    upgrade_schema(database)
    principal = snapshot(database)['access_key_bindings'][0]['principal_id']
    with database.begin() as conn:
        add_principal(conn, 'user')
        insert_row(conn, 'access_sessions', **session_values())
        insert_row(conn, 'access_sessions', **session_values(id='key-session', principal_id=principal,
            token_hash='different', source_kind='key', password_version=None, source_key_id='db-key', source_key_version=1))
        insert_row(conn, 'access_sessions', **session_values(id='bootstrap-session', principal_id='bootstrap',
            token_hash='bootstrap-hash', source_kind='bootstrap', password_version=None, bootstrap_fingerprint='keyed-fingerprint'))
    assert len(snapshot(database)['access_sessions']) == 3


def rewrite_sqlite_table(connection, name, transform):
    """Rebuild only the targeted isolated fixture table to mutate its contract.

    macOS SQLite uses defensive mode and refuses writable_schema edits. This
    test-only rebuild preserves rows/indexes, disables FK checks only for the
    fixture corruption, and restores enforcement before the migration runs.
    """
    source = connection.execute(sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"), {'name': name}).scalar_one()
    changed = transform(source)
    assert changed != source
    indexes = connection.execute(sa.text("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=:name AND sql IS NOT NULL"), {'name': name}).scalars().all()
    raw = connection.connection.driver_connection
    raw.commit()
    connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
    temporary = 'fixture_corruption_' + name
    changed = changed.replace('CREATE TABLE ' + name, 'CREATE TABLE ' + temporary, 1)
    columns = ','.join(c['name'] for c in sa.inspect(connection).get_columns(name))
    try:
        connection.exec_driver_sql(changed)
        connection.exec_driver_sql('INSERT INTO ' + temporary + ' (' + columns + ') SELECT ' + columns + ' FROM ' + name)
        connection.exec_driver_sql('DROP TABLE ' + name)
        connection.exec_driver_sql('ALTER TABLE ' + temporary + ' RENAME TO ' + name)
        for definition in indexes:
            connection.exec_driver_sql(definition)
        raw.commit()
    finally:
        raw.rollback()
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
    assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1


def replace_check(connection, table, name, expression):
    if connection.dialect.name == 'postgresql':
        connection.exec_driver_sql('ALTER TABLE ' + table + ' DROP CONSTRAINT ' + name)
        if expression is not None:
            connection.exec_driver_sql('ALTER TABLE ' + table + ' ADD CONSTRAINT ' + name + ' CHECK (' + expression + ')')
    else:
        old = next(c['sqltext'] for c in sa.inspect(connection).get_check_constraints(table) if c['name'] == name)
        needle = 'CONSTRAINT ' + name + ' CHECK (' + old + ')'
        def transform(source):
            assert needle in source
            return source.replace(needle, 'CONSTRAINT ' + name + ' CHECK (' + expression + ')' if expression is not None else 'CHECK (1 = 1)')
        rewrite_sqlite_table(connection, table, transform)


@pytest.mark.parametrize('expression', [
    'policy_version >= 0 AND catalogue_version = 1',
    'policy_version >= 1 OR catalogue_version = 1',
    '(policy_version >= 1 AND catalogue_version = 1) OR true',
    'policy_version >= 1 AND catalogue_version >= 1', None,
])
def test_same_named_weakened_extra_or_missing_checks_fail_without_mutation(database, expression):
    upgrade_schema(database)
    with database.begin() as conn:
        replace_check(conn, 'access_state', 'ck_access_state_versions', expression)
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError, match='check'):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


def test_changed_literal_case_is_not_equivalent(database):
    upgrade_schema(database)
    with database.begin() as conn:
        conn.exec_driver_sql('DELETE FROM access_state')
        replace_check(conn, 'access_state', 'ck_access_state_singleton', "id = 'STATE'")
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError, match='check'):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


def test_changed_parentheses_cannot_weaken_principal_bootstrap_shape(database):
    upgrade_schema(database)
    # Last AND would then apply to both OR alternatives, rather than only the
    # non-bootstrap branch. This is semantically distinct even if names match.
    with database.begin() as conn:
        expression = "kind = 'bootstrap' AND (id = 'bootstrap' OR kind <> 'bootstrap') OR id <> 'bootstrap'"
        replace_check(conn, 'access_principals', 'ck_access_principals_bootstrap', expression)
    with pytest.raises(SchemaUpgradeError, match='check'):
        upgrade_schema(database)


@pytest.mark.parametrize('damage', ['extra_check', 'unnamed_check', 'rename_pk', 'rename_fk', 'rename_index', 'extra_index', 'missing_index', 'extra_column', 'missing_table'])
def test_access_frozen_names_inventory_and_structure_are_enforced(database, damage):
    upgrade_schema(database)
    with database.begin() as conn:
        if damage in {'extra_check', 'unnamed_check'}:
            named = 'CONSTRAINT unexpected_check ' if damage == 'extra_check' else ''
            if database.dialect.name == 'postgresql':
                conn.exec_driver_sql('ALTER TABLE access_state ADD ' + named + 'CHECK (policy_version >= 1)')
            else:
                rewrite_sqlite_table(conn, 'access_state', lambda sql: sql.rstrip().removesuffix(')') + ', ' + named + 'CHECK (policy_version >= 1)\n)')
        elif damage in {'rename_pk', 'rename_fk'}:
            table = 'access_state' if damage == 'rename_pk' else 'access_key_grants'
            name = 'pk_access_state' if damage == 'rename_pk' else 'fk_access_key_grants_binding'
            if database.dialect.name == 'postgresql':
                conn.exec_driver_sql('ALTER TABLE ' + table + ' RENAME CONSTRAINT ' + name + ' TO changed_name')
            else:
                rewrite_sqlite_table(conn, table, lambda sql: sql.replace(name, 'changed_name'))
        elif damage in {'rename_index', 'missing_index'}:
            conn.exec_driver_sql('DROP INDEX ix_access_sessions_expires_at')
            if damage == 'rename_index':
                conn.exec_driver_sql('CREATE INDEX same_shape_other_name ON access_sessions(expires_at)')
        elif damage == 'extra_index':
            conn.exec_driver_sql('CREATE INDEX extra_index ON access_sessions(expires_at)')
        elif damage == 'extra_column':
            conn.exec_driver_sql('ALTER TABLE access_state ADD COLUMN future_value INTEGER')
        else:
            conn.exec_driver_sql('DROP TABLE access_mailbox_routes')
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


@pytest.mark.parametrize('reserved', ['table', 'view', 'table_index', 'planned_index', 'planned_pk_index', 'planned_table', 'planned_view', 'planned_unique_constraint', 'planned_primary_constraint'])
def test_pre_access_reserved_namespace_conflicts_are_rejected_before_ddl(database, reserved, monkeypatch):
    at_revision(database)
    with database.begin() as conn:
        if reserved == 'table':
            conn.exec_driver_sql('CREATE TABLE access_state (id VARCHAR(40) PRIMARY KEY)')
        elif reserved == 'view':
            conn.exec_driver_sql('CREATE VIEW access_state AS SELECT id FROM fax_jobs')
        elif reserved == 'planned_table':
            conn.exec_driver_sql('CREATE TABLE ix_access_sessions_expires_at (value INTEGER)')
        elif reserved == 'planned_view':
            conn.exec_driver_sql('CREATE VIEW ix_access_sessions_expires_at AS SELECT id FROM fax_jobs')
        elif reserved in {'planned_unique_constraint', 'planned_primary_constraint'}:
            if database.dialect.name == 'sqlite':
                pytest.skip('SQLite does not assign explicit constraint names to backing indexes')
            clause = ('CONSTRAINT uq_access_sessions_token_hash UNIQUE(id)' if reserved == 'planned_unique_constraint'
                      else 'CONSTRAINT pk_access_state PRIMARY KEY(id)')
            conn.exec_driver_sql('CREATE TABLE auxiliary (id VARCHAR(40) NOT NULL, ' + clause + ')')
        else:
            conn.exec_driver_sql('CREATE TABLE auxiliary (id VARCHAR(40) PRIMARY KEY)')
            index = {'table_index': 'access_state', 'planned_index': 'ix_access_sessions_expires_at', 'planned_pk_index': 'pk_access_state'}[reserved]
            conn.exec_driver_sql('CREATE INDEX ' + index + ' ON auxiliary(id)')
    from api.app import schema_access
    def forbidden_ddl(*args):
        pytest.fail('Preflight did not reject reserved namespace before DDL')
    monkeypatch.setattr(schema_access, 'upgrade_access', forbidden_ddl)
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


@pytest.mark.parametrize('stage', ['after_seed', 'during_key', 'during_resource', 'after_migration'])
def test_access_upgrade_rolls_back_ddl_seed_enrollment_audit_and_version(database, monkeypatch, stage):
    at_revision(database)
    with database.begin() as conn:
        add_key(conn, 'db-key', 'token', 'fax:read')
        add_job(conn, 'history-job', 'queued')
    before, definitions = snapshot(database), schema_description(database)
    from api.app import schema_access
    if stage == 'after_migration':
        original = schema_access.upgrade_access
        def injected(connection, operations):
            original(connection, operations)
            raise RuntimeError('injected migration failure')
        monkeypatch.setattr(schema_access, 'upgrade_access', injected)
    else:
        def injected(connection, cursor, statement, parameters, context, executemany):
            table = {'after_seed': 'access_state', 'during_key': 'access_key_bindings', 'during_resource': 'access_resources'}[stage]
            if statement.startswith('INSERT INTO ' + table):
                if stage != 'during_resource' or context.compiled_parameters[0].get('kind') == 'outbound':
                    raise RuntimeError('injected migration failure')
        sa.event.listen(database, 'after_cursor_execute', injected)
    try:
        with pytest.raises(RuntimeError, match='injected migration failure'):
            upgrade_schema(database)
    finally:
        if stage != 'after_migration':
            sa.event.remove(database, 'after_cursor_execute', injected)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


@pytest.mark.parametrize('revision', ['0001_initial', '0002_schema_foundation', '0003_configuration', PRIOR])
def test_old_frozen_revisions_still_validate_and_reject_any_check(database, revision):
    at_revision(database, revision)
    with database.connect() as conn:
        assert validate_schema(conn, require_version=True) == revision
    with database.begin() as conn:
        if database.dialect.name == 'postgresql':
            conn.exec_driver_sql('ALTER TABLE fax_jobs ADD CONSTRAINT nonfrozen CHECK (id IS NOT NULL)')
        else:
            rewrite_sqlite_table(conn, 'fax_jobs', lambda sql: sql.rstrip().removesuffix(')') + ', CONSTRAINT nonfrozen CHECK (id IS NOT NULL)\n)')
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError, match='check'):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


@pytest.mark.parametrize('setting', ['foreign_keys=OFF', 'ignore_check_constraints=ON'])
def test_misconfigured_sqlite_enforcement_is_refused_without_silently_enabling(database, setting):
    if database.dialect.name != 'sqlite':
        pytest.skip('SQLite enforcement flags')
    at_revision(database)
    with database.connect() as conn:
        conn.exec_driver_sql('PRAGMA ' + setting)
        conn.rollback()
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError, match='enforcement is disabled'):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions
    with database.connect() as conn:
        key, value = setting.split('=')
        assert conn.exec_driver_sql('PRAGMA ' + key).scalar_one() == (0 if value == 'OFF' else 1)


@pytest.mark.parametrize('semantics', ['NOT VALID', 'NO INHERIT'])
def test_postgres_unvalidated_or_noninherited_check_is_not_the_frozen_constraint(database, semantics):
    if database.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL constraint flags')
    upgrade_schema(database)
    with database.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE access_state DROP CONSTRAINT ck_access_state_versions')
        conn.exec_driver_sql('ALTER TABLE access_state ADD CONSTRAINT ck_access_state_versions CHECK (policy_version >= 1 AND catalogue_version = 1) ' + semantics)
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


def test_two_process_0004_access_migrations_converge_to_one_seed_and_audit_set(database):
    at_revision(database)
    with database.begin() as conn:
        add_key(conn, 'db-key', 'token', 'fax:read')
    with database.connect() as conn:
        namespace = conn.exec_driver_sql('SELECT current_schema()').scalar_one() if database.dialect.name == 'postgresql' else ''
    script = '''
import os
from api.app.schema import create_database_engine, upgrade_schema
args = {'options': '-csearch_path='+os.environ['SCHEMA']} if os.environ['SCHEMA'] else {}
engine = create_database_engine(os.environ['TARGET'], connect_args=args)
try:
    upgrade_schema(engine)
finally:
    engine.dispose()
'''
    root = Path(__file__).resolve().parents[2]
    env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(root),
           'TARGET': database.url.render_as_string(hide_password=False), 'SCHEMA': namespace}
    processes = [subprocess.Popen([sys.executable, '-c', script], cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stdout + stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    rows = snapshot(database)
    assert rows['alembic_version'] == [{'version_num': HEAD}]
    assert len(rows['access_principals']) == 2
    assert len(rows['access_key_bindings']) == len(rows['access_assignments']) == len(rows['access_key_grants']) == 1
    assert len(rows['access_audit']) == 2


def test_sqlite_overlength_or_control_key_labels_are_not_copied_to_display_or_audit(database):
    if database.dialect.name != 'sqlite':
        pytest.skip('SQLite preserves historical overlength VARCHAR data')
    at_revision(database)
    with database.begin() as conn:
        add_key(conn, 'db-key', 'x' * 5000, 'fax:read')
    before = snapshot(database)
    upgrade_schema(database)
    after = snapshot(database)
    assert after['api_keys'] == before['api_keys']
    assert all(len(r['display_name']) <= 200 for r in after['access_principals'])
    assert 'x' * 100 not in repr(after['access_audit'])


def test_revoked_and_expired_ordinary_keys_preserve_identity_and_explicit_ceiling_without_reviving_them(database):
    at_revision(database)
    with database.begin() as conn:
        add_key(conn, 'revoked', 'revoked-token', 'fax:read', revoked=OLD, expired=OLD)
    before = snapshot(database)
    upgrade_schema(database)
    rows = snapshot(database)
    assert rows['api_keys'] == before['api_keys']
    assert rows['access_key_bindings'][0]['state'] == 'revoked'
    assert rows['access_key_bindings'][0]['revoked_at'] == rows['api_keys'][0]['revoked_at']
    assert len(rows['access_assignments']) == 1
    assert [(r['permission_id'], r['resource_id']) for r in rows['access_key_grants']] == [('fax:read', 'installation')]
    assert rows['access_key_bindings'][0]['principal_id'] == migrated_id('key_principal', 'revoked')


@pytest.mark.parametrize('table,values', [
    ('access_principals', dict(id='impostor', kind='bootstrap', display_name='Impostor', enabled=1, security_version=1, version=1, created_at=OLD, updated_at=OLD)),
    ('access_audit', dict(id='event', actor_key_binding_id='db-key', operation='test', target_kind='key', policy_version_before=0, policy_version_after=1, outcome='allowed', details='{}', created_at=OLD)),
    ('access_audit', dict(id='event', operation='test', target_kind='key', policy_version_before=2, policy_version_after=1, outcome='allowed', details='{}', created_at=OLD)),
    ('access_audit', dict(id='event', operation='test', target_kind='key', policy_version_before=0, policy_version_after=1, outcome='unknown', details='{}', created_at=OLD)),
    ('access_key_bindings', dict(id='db-key', principal_id='bootstrap', state='active', version=1, security_version=1, revoked_at=OLD, created_at=OLD, updated_at=OLD)),
    ('access_key_bindings', dict(id='db-key', principal_id='bootstrap', state='revoked', version=1, security_version=1, revoked_at=None, created_at=OLD, updated_at=OLD)),
    ('access_state', dict(id='other', policy_version=1, catalogue_version=1, updated_at=OLD)),
])
def test_principal_binding_audit_and_state_checks_reject_invalid_rows(database, table, values):
    at_revision(database)
    with database.begin() as conn:
        add_key(conn, 'db-key', 'token', '')
    upgrade_schema(database)
    with database.begin() as conn:
        if table == 'access_key_bindings':
            conn.exec_driver_sql("DELETE FROM access_key_bindings WHERE id='db-key'")
    with pytest.raises(sa.exc.IntegrityError), database.begin() as conn:
        insert_row(conn, table, **values)


def test_reserved_bootstrap_principal_cannot_change_to_user_kind(database):
    upgrade_schema(database)
    with pytest.raises(sa.exc.IntegrityError, match='ck_access_principals_bootstrap'), database.begin() as conn:
        # Updating the existing row exercises the reserved-ID converse without
        # a duplicate primary key that could mask a weakened CHECK expression.
        conn.exec_driver_sql("UPDATE access_principals SET kind='user' WHERE id='bootstrap'")
    with database.connect() as conn:
        assert conn.exec_driver_sql("SELECT kind FROM access_principals WHERE id='bootstrap'").scalar_one() == 'bootstrap'


def test_valid_principal_and_group_assignments_and_session_audit_keep_history(database):
    at_revision(database)
    with database.begin() as conn:
        add_key(conn, 'db-key', 'token', 'fax:read')
    upgrade_schema(database)
    principal = snapshot(database)['access_key_bindings'][0]['principal_id']
    with database.begin() as conn:
        insert_row(conn, 'access_groups', id='group', name='Group', normalized_name='group', description='', enabled=1, version=1, created_at=OLD, updated_at=OLD)
        insert_row(conn, 'access_assignments', id='principal-assignment', principal_id=principal, role_id='role_fax_viewer', resource_id='installation', version=1, created_at=OLD, updated_at=OLD)
        insert_row(conn, 'access_assignments', id='group-assignment', group_id='group', role_id='role_fax_viewer', resource_id='legacy', version=1, created_at=OLD, updated_at=OLD)
        insert_row(conn, 'access_sessions', **session_values(principal_id=principal, source_kind='key', password_version=None, source_key_id='db-key', source_key_version=1))
        insert_row(conn, 'access_audit', id='session-audit', actor_principal_id=principal, actor_key_binding_id='db-key', actor_session_id='session', operation='test', target_kind='session', policy_version_before=1, policy_version_after=1, outcome='allowed', details='{}', created_at=OLD)
    for statement in ["DELETE FROM access_sessions WHERE id='session'", "DELETE FROM api_keys WHERE id='db-key'", "DELETE FROM access_resources WHERE id='legacy'"]:
        with pytest.raises(sa.exc.IntegrityError), database.begin() as conn:
            conn.exec_driver_sql(statement)
