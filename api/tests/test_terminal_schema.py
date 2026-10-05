"""Actual 0017 to 0018 upgrade: the Terminal leaves the built-in Host Operator role (SQLite and PostgreSQL)."""
from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_terminal
from api.app.access.catalog import BUILTIN_ROLE_PERMISSIONS
from api.app.access.store import AccessStore
from api.tests.test_access_schema import at_revision
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture


def _host_operator(engine):
    members = sa.Table('access_role_permissions', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return set(connection.execute(sa.select(members.c.permission_id)
                                      .where(members.c.role_id == 'role_host_operator')).scalars())


def _ordered(rows):
    """A snapshot with each table's rows in a stable order."""
    return {name: sorted(items, key=repr) for name, items in rows.items()}


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def test_terminal_owner_only_is_head_after_fax_engine():
    assert schema.HEAD == schema_terminal.REVISION == '0018_terminal_owner_only'
    assert schema.FAX_ENGINE == '0017_fax_engine'
    assert schema_terminal.TABLES == frozenset()
    assert 'host:terminal' in BUILTIN_ROLE_PERMISSIONS['role_owner']
    assert 'host:terminal' not in BUILTIN_ROLE_PERMISSIONS['role_host_operator']


def test_0018_removes_only_the_host_operator_terminal_row_and_starts(database):
    at_revision(database, '0017_fax_engine')
    assert 'host:terminal' in _host_operator(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    removed = [row for row in before['access_role_permissions'] if row not in after['access_role_permissions']]
    assert removed == [{'id': schema_terminal.ROW_ID, 'role_id': 'role_host_operator', 'permission_id': 'host:terminal'}]
    assert len(after['access_role_permissions']) == len(before['access_role_permissions']) - 1
    for name, rows in before.items():
        if name not in {'alembic_version', 'access_role_permissions'}:
            assert after[name] == rows, name
    assert _host_operator(database) == set(BUILTIN_ROLE_PERMISSIONS['role_host_operator'])
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    # The access store checks its catalogue at start; an upgraded installation passes.
    AccessStore(database)
    schema.upgrade_schema(database)
    assert snapshot(database) == after


def test_0018_downgrade_puts_the_row_back_and_upgrades_again(database):
    at_revision(database, '0017_fax_engine')
    before = snapshot(database)
    schema.upgrade_schema(database)
    _downgrade(database, '0017_fax_engine')
    assert _ordered(snapshot(database)) == _ordered(before)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.FAX_ENGINE
    schema.upgrade_schema(database)
    assert 'host:terminal' not in _host_operator(database)


def test_0018_refuses_a_host_operator_role_that_is_not_as_left(database):
    at_revision(database, '0017_fax_engine')
    members = sa.Table('access_role_permissions', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(members.delete().where(members.c.id == schema_terminal.ROW_ID))
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
