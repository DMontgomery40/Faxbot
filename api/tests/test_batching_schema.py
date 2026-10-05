"""Actual 0013 to 0014 upgrade: the frozen sending-together tables and namespace refusal."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_batching
from api.tests.test_schema import database, snapshot
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_terminal_change


NOW = datetime(2026, 10, 3, 12)


def test_sending_together_is_head_after_carrier_records():
    assert schema.BATCHING == schema_batching.REVISION == '0014_send_together'
    assert schema.RECORDS == '0013_carrier_records'
    assert schema_batching.TABLES <= schema.STRICT_TABLES


def test_0014_upgrade_preserves_0013_rows_and_validates_frozen_shape(database):
    at_revision(database, '0013_carrier_records')
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name in schema_batching.ORDER:
        assert after[name] == []
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_terminal_change(name, after[name]) == without_terminal_change(name, rows), name
    metadata = schema_batching.frozen_metadata(dialect=database.dialect.name)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        for name in schema_batching.ORDER:
            table = metadata.tables[name]
            assert inspector.get_pk_constraint(name)['name'] == f'pk_{name}'
            assert {c['name'] for c in inspector.get_check_constraints(name)} == {
                c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
            assert {f['name'] for f in inspector.get_foreign_keys(name)} == {
                f.name for f in table.foreign_key_constraints}
            assert {(i['name'], tuple(i['column_names']), bool(i['unique']))
                    for i in inspector.get_indexes(name)} == {
                (index, columns, unique) for index, owner, columns, unique in schema_batching.INDEXES
                if owner == name}
    schema.upgrade_schema(database)
    assert snapshot(database) == after


@pytest.mark.parametrize('name', schema_batching.ORDER)
@pytest.mark.parametrize('conflict', ['table', 'view'])
def test_reserved_batching_namespace_refuses_without_changes(database, conflict, name):
    at_revision(database, '0013_carrier_records')
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql(f'CREATE TABLE {name} (private_data VARCHAR(40))')
        else:
            connection.exec_driver_sql(f'CREATE VIEW {name} AS SELECT id FROM access_state')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


def test_batching_checks_reject_bad_values_and_a_removed_fax_removes_its_membership(database):
    schema.upgrade_schema(database)
    metadata = schema_batching.frozen_metadata(dialect=database.dialect.name)
    numbers, changes = metadata.tables['batching_numbers'], metadata.tables['batching_changes']
    members, jobs = metadata.tables['outbound_batch_members'], metadata.tables['fax_jobs']
    number = dict(phone_number='+15555550123', enabled=1, max_wait_seconds=600, max_pages=30, mixed_senders=0,
                  version=1, created_at=NOW, updated_at=NOW)
    with database.begin() as connection:
        connection.execute(numbers.insert().values(id='n-1', **number))
    for change in ({'enabled': 2}, {'max_wait_seconds': 59}, {'max_wait_seconds': 3601}, {'max_pages': 1},
                   {'max_pages': 201}, {'mixed_senders': -1}, {'version': 0}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(numbers.insert().values(
                    id='n-x', **{**number, 'phone_number': '+15555550999', **change}))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(numbers.insert().values(id='n-2', **number))  # one row per number
    change = dict(phone_number='+15555550123', action='on', actor='principal:p1', actor_name='Front desk',
                  recipient_agreed=1, max_wait_seconds=600, max_pages=30, mixed_senders=0, created_at=NOW)
    with database.begin() as connection:
        connection.execute(changes.insert().values(id='c-1', **change))
    for bad in ({'action': 'paused'}, {'recipient_agreed': 2}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(changes.insert().values(id='c-x', **{**change, **bad}))
    with database.begin() as connection:
        for job in ('job-1', 'job-2'):
            connection.execute(jobs.insert().values(id=job, to_number='+15555550123', status='queued', pages=2, file_name='a.pdf', tiff_path='',
                                                    backend='sip', created_at=NOW, updated_at=NOW))
    member = dict(id='job-1', phone_number='+15555550123', sender_scope='principal:p1', sender_name='Front desk',
                  pages=2, urgent=0, hold_until=NOW, state='waiting', created_at=NOW, updated_at=NOW)
    with database.begin() as connection:
        connection.execute(members.insert().values(**member))
    for bad in ({'state': 'lost'}, {'pages': 0}, {'urgent': 2}, {'document_number': 0},
                {'first_page': 3, 'last_page': 2}, {'id': 'missing-job'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(members.insert().values(**{**member, 'id': 'job-2', **bad}))
    with database.begin() as connection:
        connection.execute(jobs.delete().where(jobs.c.id == 'job-1'))
    with database.connect() as connection:
        assert connection.execute(sa.select(members)).all() == []
        assert len(connection.execute(sa.select(changes)).all()) == 1
