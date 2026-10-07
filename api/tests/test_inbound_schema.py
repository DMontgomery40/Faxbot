"""Actual 0009 to 0010 upgrade, frozen inbound import shape and namespace refusal."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_inbound, schema_receiving_rules
from api.tests.test_schema import database, snapshot
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes, without_work_catalogue


NOW = datetime(2026, 10, 3, 12)


def test_inbound_imports_are_head_after_sip_call_records():
    assert schema.INBOUND == schema_inbound.REVISION == '0010_inbound_imports'
    assert schema.SIP == '0009_sip_call_records'
    assert schema_inbound.TABLES <= schema.STRICT_TABLES


def _seed_received_fax(engine):
    inbound = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(inbound.insert().values(
            id='inbound-1', from_number='+15555550100', to_number='+15555550123', status='received',
            backend='phaxio', provider_sid='9001', pages=1, size_bytes=1024, sha256='a' * 64,
            pdf_path='/data/inbound-1.pdf', created_at=NOW, received_at=NOW, updated_at=NOW))


def test_0010_upgrade_preserves_0009_state_and_validates_frozen_shape(database):
    at_revision(database, '0009_sip_call_records')
    _seed_received_fax(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    assert after['inbound_imports'] == []
    for name, rows in before.items():
        if name != 'alembic_version':
            assert (without_later_access_changes(name, without_work_catalogue(name, after[name]))
                    == without_later_access_changes(name, rows)), name
    metadata = schema_inbound.frozen_metadata(dialect=database.dialect.name)
    table = metadata.tables['inbound_imports']
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        assert inspector.get_pk_constraint('inbound_imports')['name'] == 'pk_inbound_imports'
        assert {c['name'] for c in inspector.get_check_constraints('inbound_imports')} == {
            c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
        assert {f['name'] for f in inspector.get_foreign_keys('inbound_imports')} == {
            'fk_inbound_imports_inbound_fax'}
        # 0030 adds an index on the receiving account.
        assert {(i['name'], tuple(i['column_names']), bool(i['unique']))
                for i in inspector.get_indexes('inbound_imports')} == {
            (name, columns, unique) for name, columns, unique in schema_inbound.INDEXES} | {
            (name, columns, unique) for name, table, columns, unique in schema_receiving_rules.ADDED_INDEXES
            if table == 'inbound_imports'}
        columns = {c['name']: c for c in inspector.get_columns('inbound_imports')}
        # 0030 adds the receiving account.
        assert set(columns) == {column.name for column in table.columns} | {'account_key'}
        assert not columns['imported_at']['nullable'] and columns['source_received_at']['nullable']
        assert not columns['inbound_fax_id']['nullable'] and not columns['revision']['nullable']
    schema.upgrade_schema(database)
    assert snapshot(database) == after


@pytest.mark.parametrize('conflict', ['table', 'view'])
def test_reserved_inbound_namespace_refuses_without_changes(database, conflict):
    at_revision(database, '0009_sip_call_records')
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql('CREATE TABLE inbound_imports (private_data VARCHAR(40))')
        else:
            connection.exec_driver_sql('CREATE VIEW inbound_imports AS SELECT id FROM access_state')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


def test_index_name_owned_by_another_table_refuses_before_ddl(database):
    at_revision(database, '0009_sip_call_records')
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE unrelated_private (id VARCHAR(40))')
        connection.exec_driver_sql('CREATE INDEX ix_inbound_imports_due ON unrelated_private (id)')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


def test_partial_inbound_ddl_failure_rolls_back_version_and_tables(database, monkeypatch):
    at_revision(database, '0009_sip_call_records')
    before = snapshot(database)
    original = schema_inbound.upgrade_inbound

    def fail(connection, operations):
        original(connection, operations)
        raise RuntimeError('synthetic interrupted migration')
    monkeypatch.setattr(schema_inbound, 'upgrade_inbound', fail)
    with pytest.raises(RuntimeError, match='synthetic interrupted migration'):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
    monkeypatch.setattr(schema_inbound, 'upgrade_inbound', original)
    schema.upgrade_schema(database)
    assert snapshot(database)['alembic_version'] == [{'version_num': schema.HEAD}]


def _import_row(**changes):
    row = dict(id='import-1', source='phaxio', account='phaxio:0123456789ab', operation_id='9001', revision='',
               state='pending', attempts=0, imported_at=NOW, inbound_fax_id='inbound-1',
               created_at=NOW, updated_at=NOW)
    row.update(changes)
    return row


def test_inbound_import_checks_reject_unknown_values_and_unbacked_receipts(database):
    schema.upgrade_schema(database)
    _seed_received_fax(database)
    table = sa.Table('inbound_imports', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(table.insert().values(**_import_row()))
    for index, change in enumerate((
            {'source': 'fax'}, {'state': 'waiting'}, {'attempts': -1}, {'reported_pages': -1},
            {'artifact_size': -1, 'state': 'failed'},
            {'state': 'received'},
            {'state': 'conflict', 'artifact_digest': 'b' * 64, 'artifact_size': 10},
            {'inbound_fax_id': 'missing-fax'})):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(table.insert().values(**_import_row(
                    id=f'bad-{index}', operation_id=f'op-{index}', **change)))
    received = _import_row(id='import-2', operation_id='9002', state='received', artifact_digest='b' * 64,
                           artifact_size=10, artifact_media_type='application/pdf', acquired_at=NOW)
    with database.begin() as connection:
        connection.execute(table.insert().values(**received))


def test_source_identity_is_unique_per_account_and_revision(database):
    schema.upgrade_schema(database)
    _seed_received_fax(database)
    table = sa.Table('inbound_imports', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(table.insert().values(**_import_row()))
        connection.execute(table.insert().values(**_import_row(id='import-2', account='phaxio:ba9876543210')))
        connection.execute(table.insert().values(**_import_row(id='import-3', revision='2')))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(table.insert().values(**_import_row(id='import-4')))


def test_inbound_import_follows_inbound_fax_retention(database):
    schema.upgrade_schema(database)
    _seed_received_fax(database)
    imports = sa.Table('inbound_imports', sa.MetaData(), autoload_with=database)
    inbound = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(imports.insert().values(**_import_row()))
    with database.begin() as connection:
        connection.execute(inbound.delete().where(inbound.c.id == 'inbound-1'))
    with database.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(imports)).scalar_one() == 0
