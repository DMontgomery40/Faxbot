"""Actual 0012 to 0013 upgrade: the frozen carrier record table and namespace refusal."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_records
from api.tests.test_schema import database, snapshot
from api.tests.test_access_schema import at_revision


NOW = datetime(2026, 10, 3, 12)


def test_carrier_records_are_head_after_carrier_charges():
    assert schema.RECORDS == schema_records.REVISION == '0013_carrier_records'
    assert schema.CHARGES == '0012_carrier_charges'
    assert schema_records.TABLES <= schema.STRICT_TABLES


def test_0013_upgrade_preserves_0012_rows_and_validates_frozen_shape(database):
    at_revision(database, '0012_carrier_charges')
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    assert after['carrier_records'] == []
    for name, rows in before.items():
        if name != 'alembic_version':
            assert after[name] == rows, name
    metadata = schema_records.frozen_metadata(dialect=database.dialect.name)
    table = metadata.tables['carrier_records']
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        assert inspector.get_pk_constraint('carrier_records')['name'] == 'pk_carrier_records'
        assert {c['name'] for c in inspector.get_check_constraints('carrier_records')} == {
            c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
        assert {f['name'] for f in inspector.get_foreign_keys('carrier_records')} == {
            f.name for f in table.foreign_key_constraints}
        assert {(i['name'], tuple(i['column_names']), bool(i['unique']))
                for i in inspector.get_indexes('carrier_records')} == {
            (name, columns, unique) for name, _, columns, unique in schema_records.INDEXES}
    schema.upgrade_schema(database)
    assert snapshot(database) == after


@pytest.mark.parametrize('conflict', ['table', 'view'])
def test_reserved_record_namespace_refuses_without_changes(database, conflict):
    at_revision(database, '0012_carrier_charges')
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql('CREATE TABLE carrier_records (private_data VARCHAR(40))')
        else:
            connection.exec_driver_sql('CREATE VIEW carrier_records AS SELECT id FROM access_state')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


def test_record_checks_reject_bad_values_and_a_removed_fax_only_detaches(database):
    schema.upgrade_schema(database)
    metadata = schema_records.frozen_metadata(dialect=database.dialect.name)
    records, faxes = metadata.tables['carrier_records'], metadata.tables['inbound_faxes']
    with database.begin() as connection:
        connection.execute(faxes.insert().values(
            id='fax-1', from_number='+15555550100', to_number='+15555550123', status='received', backend='sip',
            pages=1, created_at=NOW, received_at=NOW, updated_at=NOW))
    row = dict(provider_id='telnyx', record_id='record-1', version=1, direction='inbound', calling='+15555550100',
               called='+15555550123', started_at=NOW, answered_at=NOW, finished_at=NOW, amount_micros=3200,
               raw_amount='0.0032', currency='USD', billed_seconds=60, call_seconds=25, inbound_fax_id='fax-1',
               effective_at=NOW, observed_at=NOW, applied=1, created_at=NOW)
    with database.begin() as connection:
        connection.execute(records.insert().values(id='r-1', **row))
    for change in ({'version': 0}, {'applied': 2}, {'direction': 'sideways'}, {'billed_seconds': -1},
                   {'version': 1}, {'inbound_fax_id': 'missing'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(records.insert().values(id='r-x', **{**row, **change}))
    with database.begin() as connection:
        connection.execute(faxes.delete().where(faxes.c.id == 'fax-1'))
    with database.connect() as connection:
        kept = connection.execute(sa.select(records)).mappings().one()
    assert kept['amount_micros'] == 3200 and kept['inbound_fax_id'] is None
