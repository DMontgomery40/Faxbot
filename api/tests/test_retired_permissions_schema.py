"""Actual 0018 to 0019 upgrade: three permissions that guard nothing leave the catalogue (SQLite and PostgreSQL)."""
from datetime import datetime
import json

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_retired_permissions as retired
from api.app.access.catalog import BUILTIN_ROLE_PERMISSIONS, PERMISSIONS
from api.app.access.store import AccessStore
from api.tests.test_access_schema import add_key, at_revision
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture


NOW = datetime(2026, 10, 5, 9)
RETIRED = set(retired.PERMISSIONS)


def _ordered(rows):
    return {name: sorted(items, key=repr) for name, items in rows.items()}


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _seed(engine):
    """An owner's own role holding host:actions, and two integration keys: one limited to tunnels:read only."""
    stamp = {'created_at': NOW, 'updated_at': NOW}
    with engine.begin() as connection:
        def insert(table, **values):
            connection.execute(sa.Table(table, sa.MetaData(), autoload_with=connection).insert().values(**values))
        insert('access_roles', id='role-ops', name='Ops', normalized_name='ops', description='', kind='custom',
               enabled=1, version=3, **stamp)
        for permission in ('host:actions', 'diagnostics:read'):
            insert('access_role_permissions', id='rp-ops-' + permission, role_id='role-ops', permission_id=permission)
        insert('access_principals', id='robot', kind='integration', display_name='Robot', enabled=1,
               security_version=1, version=1, **stamp)
        for key, permissions in (('key-only-tunnel', ('tunnels:read',)), ('key-mixed', ('tunnels:manage', 'fax:read'))):
            add_key(connection, key, key + '-public', '')
            insert('access_key_bindings', id=key, principal_id='robot', state='active', version=1,
                   security_version=1, revoked_at=None, **stamp)
            for permission in permissions:
                insert('access_key_grants', id=f'grant-{key}-{permission}', key_binding_id=key,
                       permission_id=permission, resource_id='installation')


def _rows(engine, table, **where):
    meta = sa.Table(table, sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        query = sa.select(meta)
        for column, value in where.items():
            query = query.where(meta.c[column] == value)
        return [dict(row) for row in connection.execute(query).mappings()]


def test_retired_permissions_is_head_after_terminal_owner_only():
    assert schema.RETIRED == retired.REVISION == '0019_retired_permissions'
    assert schema.TERMINAL == '0018_terminal_owner_only'
    assert retired.TABLES == frozenset()
    assert not RETIRED & PERMISSIONS and 'tunnels:pair' in PERMISSIONS
    assert all(not RETIRED & set(granted) for granted in BUILTIN_ROLE_PERMISSIONS.values())


def test_0019_removes_the_permissions_every_row_naming_them_and_records_what_it_removed(database):
    at_revision(database, '0018_terminal_owner_only')
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    assert {row['id'] for row in after['access_permissions']} == PERMISSIONS
    members = {}
    for row in after['access_role_permissions']:
        members.setdefault(row['role_id'], set()).add(row['permission_id'])
    assert members == {**{role: set(granted) for role, granted in BUILTIN_ROLE_PERMISSIONS.items()},
                       'role-ops': {'diagnostics:read'}}
    # The key limited to tunnels:read keeps its binding and has no limit left; the other keeps fax:read.
    assert {row['id'] for row in after['access_key_bindings']} == {'key-only-tunnel', 'key-mixed'}
    assert [(row['key_binding_id'], row['permission_id']) for row in after['access_key_grants']] == [
        ('key-mixed', 'fax:read')]
    # Versions stay as they were.
    assert after['access_state'] == before['access_state']
    assert [row['version'] for row in after['access_roles'] if row['id'] == 'role-ops'] == [3]
    # One audit row records the removed rows of owners' own roles and of key limits.
    (record,) = [row for row in after['access_audit'] if row['operation'] == retired.OPERATION]
    assert (record['outcome'], record['actor_principal_id'], record['target_kind']) == ('migrated', None, 'installation')
    assert record['policy_version_before'] == record['policy_version_after'] == before['access_state'][0]['policy_version']
    details = json.loads(record['details'])
    assert details['revision'] == retired.REVISION and details['permissions'] == sorted(RETIRED)
    assert details['role_permissions'] == [{'id': 'rp-ops-host:actions', 'role_id': 'role-ops',
                                            'permission_id': 'host:actions'}]
    assert {(row['key_binding_id'], row['permission_id']) for row in details['key_grants']} == {
        ('key-only-tunnel', 'tunnels:read'), ('key-mixed', 'tunnels:manage')}
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    AccessStore(database)  # the startup catalogue check passes
    schema.upgrade_schema(database)
    assert snapshot(database) == after


def test_0019_downgrade_restores_every_removed_row_and_keeps_the_audit_record(database):
    at_revision(database, '0018_terminal_owner_only')
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    _downgrade(database, '0018_terminal_owner_only')
    restored = snapshot(database)
    assert len([row for row in restored['access_audit'] if row['operation'] == retired.OPERATION]) == 1
    restored['access_audit'] = [row for row in restored['access_audit'] if row['operation'] != retired.OPERATION]
    assert _ordered(restored) == _ordered(before)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.TERMINAL
    schema.upgrade_schema(database)
    assert not RETIRED & {row['id'] for row in _rows(database, 'access_permissions')}
    assert len(_rows(database, 'access_audit', operation=retired.OPERATION)) == 2


def test_0019_on_an_installation_with_only_built_in_roles_writes_no_audit_record(database):
    at_revision(database, '0018_terminal_owner_only')
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['access_audit'] == before['access_audit']
    assert {row['id'] for row in after['access_permissions']} == PERMISSIONS
    _downgrade(database, '0018_terminal_owner_only')
    assert _ordered(snapshot(database)) == _ordered(before)


def test_0019_refuses_a_catalogue_that_is_not_as_left(database):
    at_revision(database, '0018_terminal_owner_only')
    members = sa.Table('access_role_permissions', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(members.delete().where(members.c.role_id == 'role_administrator',
                                                  members.c.permission_id == 'tunnels:read'))
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
