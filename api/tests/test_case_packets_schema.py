"""Actual 0015 to 0016 upgrade: the frozen case packet send table and namespace refusal."""
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_case_packets
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_terminal_change


NOW = datetime(2026, 10, 4, 12)
API_DIRECTORY = Path(schema.__file__).resolve().parents[1]


def test_case_packet_sends_follows_inbound_sources():
    assert schema.CASE_PACKETS == schema_case_packets.REVISION == '0016_case_packet_sends'
    assert schema.INBOUND_SOURCES == '0015_inbound_sources'
    assert schema_case_packets.TABLES <= schema.STRICT_TABLES


def _seed(engine):
    metadata = sa.MetaData()
    jobs = sa.Table('fax_jobs', metadata, autoload_with=engine)
    documents = sa.Table('case_documents', metadata, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id='job-1', to_number='+15555550123', status='queued', pages=41,
                                                file_name='case.pdf', tiff_path='', backend='phaxio',
                                                created_at=NOW, updated_at=NOW))
        connection.execute(documents.insert().values(
            id='doc-1', case_id='claim-1', recipient='+15555550123', digest='a' * 64, title='Medical record',
            page_count=40, first_page=1, last_page=40, source_job_id='job-1', accepted_at=None, created_at=NOW))


def test_0016_upgrade_preserves_0015_rows_and_validates_frozen_shape(database):
    at_revision(database, '0015_inbound_sources')
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    assert after['case_packet_sends'] == []
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_terminal_change(name, after[name]) == without_terminal_change(name, rows), name
    metadata = schema_case_packets.frozen_metadata(dialect=database.dialect.name)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        for name in schema_case_packets.ORDER:
            table = metadata.tables[name]
            assert inspector.get_pk_constraint(name)['name'] == f'pk_{name}'
            assert {c['name'] for c in inspector.get_check_constraints(name)} == {
                c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
            assert {f['name'] for f in inspector.get_foreign_keys(name)} == {
                f.name for f in table.foreign_key_constraints}
            assert {(i['name'], tuple(i['column_names']), bool(i['unique']))
                    for i in inspector.get_indexes(name)} == {
                (index, columns, unique) for index, owner, columns, unique in schema_case_packets.INDEXES
                if owner == name}
    schema.upgrade_schema(database)
    assert snapshot(database) == after


@pytest.mark.parametrize('conflict', ['table', 'view'])
def test_reserved_case_packet_namespace_refuses_without_changes(database, conflict):
    at_revision(database, '0015_inbound_sources')
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql('CREATE TABLE case_packet_sends (private_data VARCHAR(40))')
        else:
            connection.exec_driver_sql('CREATE VIEW case_packet_sends AS SELECT id FROM access_state')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


def test_case_packet_checks_reject_bad_values_and_a_removed_fax_removes_its_record(database):
    schema.upgrade_schema(database)
    _seed(database)
    metadata = schema_case_packets.frozen_metadata(dialect=database.dialect.name)
    sends, jobs = metadata.tables['case_packet_sends'], metadata.tables['fax_jobs']
    send = dict(id='job-1', case_id='claim-1', recipient='+15555550123', pages_sent=5, pages_left_out=40,
                documents_left_out=1, created_at=NOW)
    with database.begin() as connection:
        connection.execute(sends.insert().values(**send))
    for bad in ({'pages_sent': 0}, {'pages_left_out': -1}, {'documents_left_out': -1}, {'id': 'missing-job'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(sends.insert().values(**{**send, 'id': 'job-2', **bad}))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(sends.insert().values(**send))  # one record per packet fax
    with database.begin() as connection:
        connection.execute(jobs.delete().where(jobs.c.id == 'job-1'))
    with database.connect() as connection:
        assert connection.execute(sa.select(sends)).all() == []


def test_downgrade_is_refused_and_deletes_nothing(database):
    at_revision(database, '0015_inbound_sources')
    _seed(database)
    schema.upgrade_schema(database)
    before = snapshot(database)
    config = Config(str(API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(API_DIRECTORY / 'alembic'))
    with database.connect() as connection:
        config.attributes['connection'] = connection
        with pytest.raises(RuntimeError, match='Destructive schema downgrade is not supported'):
            command.downgrade(config, '0015_inbound_sources')
    assert snapshot(database) == before
