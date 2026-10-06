"""Actual 0016 to 0017 upgrade: the frozen fax engine record tables and namespace refusal (SQLite and PostgreSQL)."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_fax_engine
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 4, 23)


def test_fax_engine_follows_case_packets():
    assert schema.FAX_ENGINE == schema_fax_engine.REVISION == '0017_fax_engine'
    assert schema.CASE_PACKETS == '0016_case_packet_sends'
    assert schema_fax_engine.TABLES <= schema.STRICT_TABLES


def _seed(engine):
    metadata = sa.MetaData()
    jobs = sa.Table('fax_jobs', metadata, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id='job-1', to_number='+15555550123', status='queued', pages=2,
                                                file_name='fax.pdf', tiff_path='', backend='sip',
                                                created_at=NOW, updated_at=NOW))


def test_0017_upgrade_preserves_0016_rows_and_validates_frozen_shape(database):
    at_revision(database, '0016_case_packet_sends')
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name in schema_fax_engine.ORDER:
        assert after[name] == []
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name
    metadata = schema_fax_engine.frozen_metadata(dialect=database.dialect.name)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        for name in schema_fax_engine.ORDER:
            table = metadata.tables[name]
            assert inspector.get_pk_constraint(name)['name'] == f'pk_{name}'
            assert {c['name'] for c in inspector.get_check_constraints(name)} == {
                c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
            assert {(i['name'], tuple(i['column_names']), bool(i['unique']))
                    for i in inspector.get_indexes(name)} == {
                (index, columns, unique) for index, owner, columns, unique in schema_fax_engine.INDEXES
                if owner == name}
    schema.upgrade_schema(database)
    assert snapshot(database) == after


def test_0017_constraints_keep_engine_records_honest(database):
    schema.upgrade_schema(database)
    metadata = sa.MetaData()
    calls = sa.Table('fax_engine_calls', metadata, autoload_with=database)
    observations = sa.Table('sslfax_observations', metadata, autoload_with=database)
    settings = sa.Table('recipient_fax_settings', metadata, autoload_with=database)
    row = {'id': 'c1', 'direction': 'outbound', 'call_key': 'a' * 32, 'job_id': 'job-1', 'engine': 'hylafax',
           'engine_ref': 'e1:000000001', 'sslfax': 1, 'sslfax_offered': 1, 'transfer_seconds': 9,
           'session_seconds': 18, 'created_at': NOW, 'updated_at': NOW}
    with database.begin() as connection:
        connection.execute(calls.insert().values(**row))
        # The built-in engine's row carries only the reason; SSL Fax facts stay unknown.
        connection.execute(calls.insert().values(id='c2', direction='outbound', call_key='b' * 32,
                                                 engine='builtin', reason='Faxes sent together.',
                                                 created_at=NOW, updated_at=NOW))
        connection.execute(observations.insert().values(id='o1', number='+15555550199', direction='outbound',
                                                        accepts=1, source='e1:000000001', observed_at=NOW))
        connection.execute(settings.insert().values(id='s1', number='+15555550199', max_rate=9600, ecm=0,
                                                    updated_at=NOW))
    for table, values in ((calls, {**row, 'id': 'c3', 'engine_ref': 'e1:2', 'call_key': 'c' * 32, 'engine': 'x'}),
                          (calls, {**row, 'id': 'c4', 'engine_ref': 'e1:3', 'call_key': 'd' * 32, 'sslfax': 2}),
                          (calls, {**row, 'id': 'c5', 'call_key': 'e' * 32}),  # same engine reference twice
                          (calls, {**row, 'id': 'c6', 'engine_ref': 'e1:4'}),  # same call twice
                          (observations, {'id': 'o2', 'number': '+15555550199', 'direction': 'outbound',
                                          'accepts': 1, 'source': 'e1:000000001', 'observed_at': NOW}),
                          (settings, {'id': 's2', 'number': '+15555550100', 'max_rate': 12000, 'updated_at': NOW}),
                          (settings, {'id': 's3', 'number': '+15555550199', 'max_rate': 4800, 'updated_at': NOW})):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(table.insert().values(**values))


@pytest.mark.parametrize('conflict', ['table', 'view'])
def test_reserved_fax_engine_namespace_refuses_without_changes(database, conflict):
    at_revision(database, '0016_case_packet_sends')
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql('CREATE TABLE fax_engine_calls (private_data VARCHAR(40))')
        else:
            connection.exec_driver_sql('CREATE VIEW sslfax_observations AS SELECT id FROM access_state')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
