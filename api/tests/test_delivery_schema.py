"""Actual 0007 to 0008 upgrade, frozen delivery table shape and namespace refusal."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_delivery
from api.tests.test_schema import database, snapshot
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_work_catalogue


def test_delivery_revision_is_head_after_capabilities():
    assert schema.DELIVERY == schema_delivery.REVISION == '0008_delivery_routes'
    assert schema.CAPABILITIES == '0007_access_capabilities'
    assert schema_delivery.TABLES <= schema.STRICT_TABLES


def test_0008_upgrade_preserves_0007_state_and_validates_frozen_shape(database):
    at_revision(database, '0007_access_capabilities')
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name in schema_delivery.TABLES:
        assert after[name] == [], name
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_work_catalogue(name, after[name]) == rows, name
    metadata = schema_delivery.frozen_metadata(dialect=database.dialect.name)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        for name in schema_delivery.TABLES:
            table = metadata.tables[name]
            assert inspector.get_pk_constraint(name)['name'] == 'pk_' + name
            assert {c['name'] for c in inspector.get_check_constraints(name)} == {
                c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
            assert {f['name'] for f in inspector.get_foreign_keys(name)} == {
                f.name for f in table.foreign_key_constraints}
        indexes = {(i['name'], tuple(i['column_names']), bool(i['unique']))
                   for name in schema_delivery.TABLES for i in inspector.get_indexes(name)}
        assert indexes == {(name, columns, unique) for name, _, columns, unique in schema_delivery.INDEXES}
    schema.upgrade_schema(database)
    assert snapshot(database) == after


@pytest.mark.parametrize('conflict', ['table', 'view'])
def test_reserved_delivery_namespace_refuses_without_changes(database, conflict):
    at_revision(database, '0007_access_capabilities')
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql('CREATE TABLE intake_items (private_data VARCHAR(40))')
        else:
            connection.exec_driver_sql('CREATE VIEW direct_peers AS SELECT id FROM access_state')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


def test_partial_delivery_ddl_failure_rolls_back_version_and_tables(database, monkeypatch):
    at_revision(database, '0007_access_capabilities')
    before = snapshot(database)
    original = schema_delivery.upgrade_delivery

    def fail(connection, operations):
        original(connection, operations)
        raise RuntimeError('synthetic interrupted migration')
    monkeypatch.setattr(schema_delivery, 'upgrade_delivery', fail)
    with pytest.raises(RuntimeError, match='synthetic interrupted migration'):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
    monkeypatch.setattr(schema_delivery, 'upgrade_delivery', original)
    schema.upgrade_schema(database)
    assert snapshot(database)['alembic_version'] == [{'version_num': schema.HEAD}]


def test_delivery_checks_reject_unknown_states_and_negative_rates(database):
    schema.upgrade_schema(database)
    metadata = schema_delivery.frozen_metadata(dialect=database.dialect.name)
    now = datetime(2026, 10, 3, 12)
    cards, items = metadata.tables['provider_rate_cards'], metadata.tables['intake_items']
    card = dict(id='card-1', provider_id='sip', direction='outbound', label='SIP trunk', currency='USD',
                per_minute_micros=5000, per_page_micros=0, per_call_micros=0, billing_increment_seconds=60,
                minimum_seconds=60, captured_on=now, created_at=now)
    with database.begin() as connection:
        connection.execute(cards.insert().values(**card))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(cards.insert().values(**{**card, 'id': 'card-2', 'per_page_micros': -1}))
    item = dict(id='item-1', source='fax', received_at=now, state='received', attempts=0, version=1,
                created_at=now, updated_at=now)
    with database.begin() as connection:
        connection.execute(items.insert().values(**item))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(items.insert().values(**{**item, 'id': 'item-2', 'state': 'lost'}))


def test_intake_item_follows_inbound_fax_retention(database):
    schema.upgrade_schema(database)
    metadata = schema_delivery.frozen_metadata(dialect=database.dialect.name)
    now = datetime(2026, 10, 3, 12)
    inbound, items = metadata.tables['inbound_faxes'], metadata.tables['intake_items']
    with database.begin() as connection:
        connection.execute(inbound.insert().values(id='inbound-1', status='received', backend='sip',
                                                   created_at=now, received_at=now, updated_at=now))
        connection.execute(items.insert().values(id='item-1', source='fax', inbound_fax_id='inbound-1',
            received_at=now, state='received', attempts=0, version=1, created_at=now, updated_at=now))
    with database.begin() as connection:
        connection.execute(inbound.delete().where(inbound.c.id == 'inbound-1'))
    with database.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(items)).scalar_one() == 0
