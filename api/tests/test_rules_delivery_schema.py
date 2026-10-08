"""Actual 0043 to 0032 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0032 gives the built-in Owner and Administrator roles "Approve faxes", and
records on each delivery attempt whether its call ended before any fax data.
The downgrade refuses while a role or key the owner made holds the permission.
"""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_rules_delivery
from api.app.access.catalog import BUILTIN_ROLE_PERMISSIONS, PERMISSIONS
from api.app.schema_access import _identity
from api.tests.test_access_schema import at_revision
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 12, 0)
# The migration chain follows merge order: 0032 comes after 0043 (accounts).
PRIOR = '0043_accounts'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _rows(engine, query):
    with engine.connect() as connection:
        return connection.execute(sa.text(query)).all()


def test_approve_faxes_is_in_the_catalogue_and_held_by_owner_and_administrator():
    assert schema_rules_delivery.REVISION == '0032_rules_delivery'
    assert schema.ACCOUNTS == PRIOR
    assert 'fax:approve' in PERMISSIONS
    assert {role for role, granted in BUILTIN_ROLE_PERMISSIONS.items() if 'fax:approve' in granted} == {
        'role_owner', 'role_administrator'}
    assert schema_rules_delivery.ROW_IDS == {_identity('role_permission', role, 'fax:approve')
                                             for role in ('role_owner', 'role_administrator')}


def test_0032_keeps_every_row_adds_the_permission_and_downgrades(database):
    at_revision(database, PRIOR)
    before = snapshot(database)
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    after = snapshot(database)
    for name, rows in before.items():
        if name == 'alembic_version':
            continue
        rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
        assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    assert sorted(_rows(database, "SELECT role_id FROM access_role_permissions WHERE permission_id = 'fax:approve'")) \
        == [('role_administrator',), ('role_owner',)]
    with database.connect() as connection:
        columns = {column['name'] for column in sa.inspect(connection).get_columns('outbound_attempts')}
    assert 'ended_before_data' in columns
    _downgrade(database, PRIOR)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
        columns = {column['name'] for column in sa.inspect(connection).get_columns('outbound_attempts')}
    assert 'ended_before_data' not in columns
    assert _rows(database, "SELECT id FROM access_permissions WHERE id = 'fax:approve'") == []
    restored = snapshot(database)
    assert {name: rows for name, rows in restored.items() if name != 'alembic_version'} == \
        {name: rows for name, rows in before.items() if name != 'alembic_version'}
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_downgrade_refuses_while_a_role_you_made_holds_approve_faxes(database):
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO access_roles (id, name, normalized_name, description, kind, enabled, version, created_at, "
            "updated_at) VALUES ('role-approvers', 'Approvers', 'approvers', 'Approves held faxes', 'custom', 1, 1, :now, :now)"),
            {'now': NOW})
        connection.execute(sa.text("INSERT INTO access_role_permissions (id, role_id, permission_id) VALUES "
                                   "('grant-approvers', 'role-approvers', 'fax:approve')"))
    stored = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='holds Approve faxes'):
        _downgrade(database, PRIOR)
    assert snapshot(database) == stored


def test_the_upgrade_refuses_a_catalogue_that_already_has_the_permission(database):
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text("INSERT INTO access_permissions (id, description) VALUES ('fax:approve', 'x')"))
    with pytest.raises(schema.SchemaUpgradeError, match='not as the earlier revision left it'):
        schema.upgrade_schema(database)
