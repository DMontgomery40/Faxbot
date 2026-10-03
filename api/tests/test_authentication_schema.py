"""Actual 0005→0006 upgrade/rollback and frozen admission namespace checks."""
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from api.app import schema, schema_access, schema_authentication
from api.tests.test_schema import database, snapshot
from api.tests.test_access_schema import at_revision, add_key


HEAD = '0006_auth_admission'


def test_registered_head_is_authentication_admission():
    assert schema.HEAD == HEAD


def test_fresh_upgrade_and_repeat_preserve_admission_debt(database):
    schema.upgrade_schema(database)
    assert snapshot(database)['alembic_version'] == [{'version_num': HEAD}]
    table = schema_authentication.frozen_metadata(dialect=database.dialect.name).tables['access_auth_buckets']
    now = datetime(2026, 10, 3, 12)
    with database.begin() as connection:
        connection.execute(table.insert().values(id='a' * 64, next_at=now + timedelta(seconds=60), updated_at=now))
    before = snapshot(database)
    schema.upgrade_schema(database)
    assert snapshot(database) == before
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == HEAD


def test_real_0005_upgrade_preserves_existing_key_session_policy_and_audit(database):
    at_revision(database, '0004_outbound_delivery')
    with database.begin() as connection:
        add_key(connection, 'retained-key', '012345abcdef', 'fax:read')
    at_revision(database, '0005_access_control')
    metadata = schema_access.frozen_metadata(dialect=database.dialect.name)
    now = datetime(2026, 10, 3, 12)
    with database.begin() as connection:
        connection.execute(metadata.tables['access_sessions'].insert().values(
            id='retained-session', principal_id='bootstrap', source_kind='bootstrap',
            bootstrap_fingerprint='f' * 64, principal_security_version=1,
            token_hash='a' * 64, csrf_hash='b' * 64, created_at=now,
            last_used_at=now, expires_at=now + timedelta(hours=12)))
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': HEAD}]
    assert after['access_auth_buckets'] == []
    for name, rows in before.items():
        if name != 'alembic_version':
            assert after[name] == rows, name


def test_partial_admission_ddl_failure_rolls_back_version_and_tables(database, monkeypatch):
    at_revision(database, '0005_access_control')
    before = snapshot(database)
    original = schema_authentication.upgrade_authentication
    def fail(connection, operations):
        original(connection, operations)
        raise RuntimeError('synthetic interrupted migration')
    monkeypatch.setattr(schema_authentication, 'upgrade_authentication', fail)
    with pytest.raises(RuntimeError, match='synthetic interrupted migration'):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
    monkeypatch.setattr(schema_authentication, 'upgrade_authentication', original)
    schema.upgrade_schema(database)
    assert snapshot(database)['alembic_version'] == [{'version_num': HEAD}]


@pytest.mark.parametrize('conflict', ['table', 'view', 'index', 'primary_key_name'])
def test_reserved_authentication_namespace_refuses_without_changes(database, conflict):
    at_revision(database, '0005_access_control')
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql('CREATE TABLE access_auth_buckets (private_data VARCHAR(40))')
        elif conflict == 'view':
            connection.exec_driver_sql('CREATE VIEW access_auth_buckets AS SELECT id FROM access_state')
        else:
            connection.exec_driver_sql('CREATE TABLE unrelated_admission (id VARCHAR(64))')
            name = ('ix_access_auth_buckets_next_at' if conflict == 'index' else 'pk_access_auth_buckets')
            connection.exec_driver_sql(f'CREATE INDEX {name} ON unrelated_admission (id)')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


@pytest.mark.parametrize('change', ['missing_index', 'weak_check', 'wrong_pk_name', 'nullable_clock'])
def test_stamped_authentication_shape_cannot_bypass_validation(database, change):
    at_revision(database, '0005_access_control')
    table = schema_authentication.frozen_metadata(dialect=database.dialect.name).tables['access_auth_buckets']
    if change == 'missing_index':
        table.indexes.clear()
    elif change == 'weak_check':
        check = next(c for c in table.constraints if isinstance(c, sa.CheckConstraint))
        table.constraints.remove(check)
        table.append_constraint(sa.CheckConstraint('next_at >= updated_at OR next_at < updated_at', name=check.name))
    elif change == 'wrong_pk_name':
        table.primary_key.name = 'wrong_admission_pk'
    else:
        table.c.updated_at.nullable = True
    with database.begin() as connection:
        table.create(connection)
        connection.execute(sa.text('UPDATE alembic_version SET version_num=:head'), {'head': HEAD})
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


def test_admission_write_hook_is_rejected(database):
    schema.upgrade_schema(database)
    with database.begin() as connection:
        if database.dialect.name == 'sqlite':
            connection.exec_driver_sql('CREATE TRIGGER auth_hook AFTER INSERT ON access_auth_buckets BEGIN SELECT 1; END')
        else:
            connection.exec_driver_sql("CREATE FUNCTION auth_hook_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$")
            connection.exec_driver_sql('CREATE TRIGGER auth_hook BEFORE INSERT ON access_auth_buckets FOR EACH ROW EXECUTE FUNCTION auth_hook_fn()')
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
