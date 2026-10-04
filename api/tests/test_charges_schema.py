"""Actual 0011 to 0012 upgrade: frozen carrier charge tables, added columns and namespace refusal."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_charges
from api.tests.test_schema import database, snapshot
from api.tests.test_access_schema import at_revision


NOW = datetime(2026, 10, 3, 12)


def test_carrier_charges_follow_work_items():
    assert schema.CHARGES == schema_charges.REVISION == '0012_carrier_charges'
    assert schema.WORK == '0011_work_items'
    assert schema_charges.TABLES <= schema.STRICT_TABLES


def _seed(engine):
    """A call record and a rate card as an 0011 installation stores them."""
    metadata = sa.MetaData()
    calls = sa.Table('sip_call_records', metadata, autoload_with=engine)
    cards = sa.Table('provider_rate_cards', metadata, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(calls.insert().values(
            id='call-1', direction='inbound', call_id='1700000000.1', did='+15555550123', caller='+15555550100',
            called='+15555550123', started_at=NOW, answered_at=NOW, ended_at=NOW, disposition='answered',
            connected_seconds=31, t38='yes', pages=1, fax_status='SUCCESS', fax_preference=0,
            created_at=NOW, updated_at=NOW))
        connection.execute(cards.insert().values(
            id='card-1', provider_id='sip-telnyx', direction='outbound', label='Telnyx', currency='USD',
            per_minute_micros=5000, per_page_micros=0, per_call_micros=0, billing_increment_seconds=60,
            minimum_seconds=60, source_url=None, captured_on=NOW, created_at=NOW))


def test_0012_upgrade_preserves_0011_rows_adds_empty_columns_and_validates_frozen_shape(database):
    at_revision(database, '0011_work_items')
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name in schema_charges.TABLES:
        assert after[name] == [], name
    added = {(table, column) for table, column, _ in schema_charges.ADDED_COLUMNS}
    for name, rows in before.items():
        if name == 'alembic_version':
            continue
        new = {column for table, column in added if table == name}
        # Existing rows keep every value; an added column starts empty.
        assert [{key: value for key, value in row.items() if key not in new} for row in after[name]] == rows, name
        assert all(row[column] is None for row in after[name] for column in new)
    metadata = schema_charges.frozen_metadata(dialect=database.dialect.name)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        for name in schema_charges.TABLES:
            table = metadata.tables[name]
            assert inspector.get_pk_constraint(name)['name'] == 'pk_' + name
            assert {c['name'] for c in inspector.get_check_constraints(name)} == {
                c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
            assert {f['name'] for f in inspector.get_foreign_keys(name)} == {
                f.name for f in table.foreign_key_constraints}
        indexes = {(i['name'], tuple(i['column_names']), bool(i['unique']))
                   for name in {table for _, table, _, _ in schema_charges.INDEXES} for i in inspector.get_indexes(name)
                   if i['name'] in {index for index, _, _, _ in schema_charges.INDEXES}}
        assert indexes == {(name, columns, unique) for name, _, columns, unique in schema_charges.INDEXES}
        for table, column, _ in schema_charges.ADDED_COLUMNS:
            assert {item['name']: item['nullable'] for item in inspector.get_columns(table)}[column] is True
    schema.upgrade_schema(database)
    assert snapshot(database) == after


@pytest.mark.parametrize('conflict', ['table', 'view', 'column'])
def test_reserved_charge_namespace_refuses_without_changes(database, conflict):
    at_revision(database, '0011_work_items')
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql('CREATE TABLE carrier_charges (private_data VARCHAR(40))')
        elif conflict == 'view':
            connection.exec_driver_sql('CREATE VIEW carrier_call_checks AS SELECT id FROM access_state')
        else:
            connection.exec_driver_sql('ALTER TABLE sip_call_records ADD COLUMN sip_call_id VARCHAR(100)')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


def test_partial_charge_ddl_failure_rolls_back_tables_and_columns(database, monkeypatch):
    at_revision(database, '0011_work_items')
    before = snapshot(database)
    original = schema_charges.upgrade_charges

    def fail(connection, operations):
        original(connection, operations)
        raise RuntimeError('synthetic interrupted migration')
    monkeypatch.setattr(schema_charges, 'upgrade_charges', fail)
    with pytest.raises(RuntimeError, match='synthetic interrupted migration'):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
    monkeypatch.setattr(schema_charges, 'upgrade_charges', original)
    schema.upgrade_schema(database)
    assert snapshot(database)['alembic_version'] == [{'version_num': schema.HEAD}]


def test_charge_checks_reject_bad_values_and_follow_the_call_record(database):
    schema.upgrade_schema(database)
    _seed(database)
    metadata = schema_charges.frozen_metadata(dialect=database.dialect.name)
    charges, checks = metadata.tables['carrier_charges'], metadata.tables['carrier_call_checks']
    charge = dict(call_record_id='call-1', provider_id='telnyx', record_id='record-1', version=1,
                  amount_micros=3200, raw_amount='0.0032', currency='USD', billed_seconds=60, call_seconds=31,
                  match_method='call_id', effective_at=NOW, observed_at=NOW, applied=1, is_final=0, created_at=NOW)
    with database.begin() as connection:
        connection.execute(charges.insert().values(id='charge-1', **charge))
        connection.execute(charges.insert().values(id='charge-2', **{**charge, 'version': 2, 'amount_micros': 3000,
                                                                     'supersedes_id': 'charge-1'}))
        connection.execute(checks.insert().values(id='call-1', provider_id='telnyx', state='matched', checks=1,
                                                  checked_at=NOW, next_check_at=NOW, created_at=NOW, updated_at=NOW))
    for change in ({'version': 0}, {'applied': 2}, {'is_final': -1}, {'billed_seconds': -1},
                   {'match_method': 'guess'}, {'version': 2}, {'call_record_id': 'missing'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(charges.insert().values(id='charge-x', **{**charge, **change}))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(checks.update().values(state='lost'))
    calls = metadata.tables['sip_call_records']
    with database.begin() as connection:
        connection.execute(calls.update().values(sip_call_id='3f0c5a8e-0000-4000-8000-000000000001'))
        connection.execute(calls.delete().where(calls.c.id == 'call-1'))
    with database.connect() as connection:
        for table in (charges, checks):
            assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
