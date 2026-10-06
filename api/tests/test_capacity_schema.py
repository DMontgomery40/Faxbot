"""Actual 0020 to 0021 upgrade: urgent faxes and calls at once per number (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import sqlalchemy as sa

from api.app import schema, schema_capacity
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 6, 9, 0)


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _seed(engine):
    metadata = sa.MetaData()
    jobs = sa.Table('fax_jobs', metadata, autoload_with=engine)
    destinations = sa.Table('delivery_destinations', metadata, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id='job-1', to_number='+15555550123', file_name='note.txt',
                                                tiff_path='', status='queued', pages=1, backend='sip',
                                                created_at=NOW, updated_at=NOW))
        connection.execute(destinations.insert().values(id='destination-1', phone_number='+15555550123',
                                                        accepts_references=0, version=1, created_at=NOW,
                                                        updated_at=NOW))


def _columns(engine, table):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns(table)}


def test_capacity_is_head_after_local_delivery():
    assert schema.CAPACITY == schema_capacity.REVISION == '0021_capacity'
    assert schema.LOCAL_DELIVERY == '0020_local_delivery'
    assert schema_capacity.TABLES == frozenset()


def test_0021_adds_two_empty_columns_keeps_every_row_and_downgrades(database):
    at_revision(database, '0020_local_delivery')
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name
    assert [row['urgent'] for row in after['fax_jobs']] == [None]
    assert [row['max_calls'] for row in after['delivery_destinations']] == [None]
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, '0020_local_delivery')
    assert 'urgent' not in _columns(database, 'fax_jobs')
    assert 'max_calls' not in _columns(database, 'delivery_destinations')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0020_local_delivery'
    schema.upgrade_schema(database)
    assert {'urgent'} <= _columns(database, 'fax_jobs') and {'max_calls'} <= _columns(database, 'delivery_destinations')
