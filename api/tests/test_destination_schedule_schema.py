"""Actual 0030 to 0040 upgrade: a fax's send-by time and recipient schedules (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_destination_schedule
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _seed(engine):
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id='job-1', to_number='+15555550123', file_name='note.txt',
                                                tiff_path='', status='queued', pages=1, backend='sip',
                                                created_at=NOW, updated_at=NOW))


def _columns(engine, table):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns(table)}


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def test_schedule_follows_receiving_accounts():
    assert schema.HEAD == schema_destination_schedule.REVISION == '0040_destination_schedule'
    assert schema.RECEIVING == '0030_receiving_accounts'
    assert schema_destination_schedule.TABLES == frozenset({'destination_schedules'})
    assert schema_destination_schedule.TABLES <= schema.STRICT_TABLES


def test_0040_adds_an_empty_column_and_table_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, '0030_receiving_accounts')
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name
    # A fax accepted before 0040 has no deadline: NULL, never a guessed time.
    assert [row['send_by'] for row in after['fax_jobs']] == [None]
    assert after['destination_schedules'] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, '0030_receiving_accounts')
    assert 'send_by' not in _columns(database, 'fax_jobs')
    assert 'destination_schedules' not in _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0030_receiving_accounts'
    schema.upgrade_schema(database)
    assert 'send_by' in _columns(database, 'fax_jobs') and 'destination_schedules' in _tables(database)


def test_the_downgrade_refuses_while_a_schedule_or_send_by_time_is_recorded(database):  # noqa: F811
    schema.upgrade_schema(database)
    _seed(database)
    metadata = sa.MetaData()
    jobs = sa.Table('fax_jobs', metadata, autoload_with=database)
    with database.begin() as connection:
        connection.execute(jobs.update().values(send_by=NOW))
    with pytest.raises(Exception):
        _downgrade(database, '0030_receiving_accounts')
    assert 'send_by' in _columns(database, 'fax_jobs')
    with database.begin() as connection:
        connection.execute(jobs.update().values(send_by=None))
    schedules = sa.Table('destination_schedules', metadata, autoload_with=database)
    with database.begin() as connection:
        connection.execute(schedules.insert().values(id='s-1', phone_number='+15555550123', days='mon,tue',
                                                     start_minute=480, end_minute=1080, learn_busy=1,
                                                     created_at=NOW))
    with pytest.raises(Exception):
        _downgrade(database, '0030_receiving_accounts')
    assert 'destination_schedules' in _tables(database)


def test_the_minutes_and_learning_flag_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    schedules = sa.Table('destination_schedules', sa.MetaData(), autoload_with=database)
    for bad in ({'start_minute': -1}, {'end_minute': 0}, {'end_minute': 1441}, {'learn_busy': 2}):
        row = {'id': 'bad', 'phone_number': '+15555550123', 'learn_busy': 1, 'created_at': NOW, **bad}
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(schedules.insert().values(**row))


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, '0030_receiving_accounts')
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE destination_schedules (id VARCHAR(40) PRIMARY KEY)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == \
            '0030_receiving_accounts'
