"""Actual 0023 to 0025 upgrade: one index page for a shared call (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_shared_manifest
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)
PRIOR = '0023_negotiation'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _seed(engine):
    metadata = sa.MetaData()
    jobs = sa.Table('fax_jobs', metadata, autoload_with=engine)
    numbers = sa.Table('batching_numbers', metadata, autoload_with=engine)
    changes = sa.Table('batching_changes', metadata, autoload_with=engine)
    members = sa.Table('outbound_batch_members', metadata, autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id='job-1', to_number='+15555550123', file_name='note.pdf',
                                                tiff_path='', status='queued', pages=2, backend='sip',
                                                created_at=NOW, updated_at=NOW))
        connection.execute(numbers.insert().values(id='number-1', phone_number='+15555550123', enabled=1,
                                                   max_wait_seconds=600, max_pages=30, mixed_senders=0, version=1,
                                                   created_at=NOW, updated_at=NOW))
        connection.execute(changes.insert().values(id='change-1', phone_number='+15555550123', action='on',
                                                   actor='principal:p1', actor_name='Owner', recipient_agreed=1,
                                                   max_wait_seconds=600, max_pages=30, mixed_senders=0,
                                                   created_at=NOW))
        connection.execute(members.insert().values(id='job-1', phone_number='+15555550123', sender_scope='key:front',
                                                   pages=2, urgent=0, hold_until=NOW, state='together',
                                                   batch_id='attempt-1', attempt_id='attempt-1', document_number=1,
                                                   documents=2, first_page=1, last_page=3, created_at=NOW,
                                                   updated_at=NOW))


def _columns(engine, table):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns(table)}


def test_index_page_is_head_after_negotiation():
    assert schema.HEAD == schema_shared_manifest.REVISION == '0025_shared_manifest'
    assert schema.NEGOTIATION == PRIOR and schema_shared_manifest.TABLES == frozenset()


def test_0025_adds_empty_columns_keeps_every_row_and_downgrades(database):
    at_revision(database, PRIOR)
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    added = {}
    for table, name, _ in schema_shared_manifest.COLUMNS:
        added.setdefault(table, set()).add(name)
    for name, rows in before.items():
        if name in added:
            # Every earlier row keeps its values; the new columns stay empty (a call before 0025 used separators).
            assert [{key: value for key, value in item.items() if key not in added[name]} for item in after[name]] == rows
            assert all(item[column] is None for item in after[name] for column in added[name])
        elif name != 'alembic_version':
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    for table, names in added.items():
        assert not names & _columns(database, table)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    for table, names in added.items():
        assert names <= _columns(database, table)


@pytest.mark.parametrize('table, name', [(table, name) for table, name, _ in schema_shared_manifest.COLUMNS])
def test_a_column_that_already_exists_refuses_the_upgrade_without_changes(database, table, name):
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN {name} VARCHAR(16)')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
