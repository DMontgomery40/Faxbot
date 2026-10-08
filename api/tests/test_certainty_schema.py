"""Actual 0046 to 0047 upgrade: owned records for uncertain sent faxes (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_certainty
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)
# The migration chain follows merge order: 0047 comes after 0046 (notice faxes and repairs).
PRIOR = '0046_notice_repair'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _seed_item(engine):
    """One sent fax with an uncertain attempt and its item."""
    metadata = sa.MetaData()
    jobs, attempts, items = (sa.Table(name, metadata, autoload_with=engine)
                             for name in ('fax_jobs', 'outbound_attempts', 'certainty_items'))
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id='job-1', to_number='+15555550123', file_name='a.pdf',
                                                tiff_path='', status='queued', backend='phaxio', created_at=NOW,
                                                updated_at=NOW))
        connection.execute(attempts.insert().values(id='attempt-1', job_id='job-1', sequence=1, phase='uncertain',
                                                    error_category='transport_ambiguous', created_at=NOW))
        connection.execute(items.insert().values(
            id='item-1', job_id='job-1', attempt_id='attempt-1', category='transport_ambiguous', reference='K7Q4M2',
            state='open', due_hours=24, version=1, created_at=NOW, updated_at=NOW))


def test_certainty_follows_notice_repair():
    assert schema_certainty.REVISION == '0047_certainty'
    assert schema.NOTICE_REPAIR == PRIOR
    assert schema_certainty.TABLES == {'certainty_items', 'certainty_events', 'certainty_settings'}


def test_0047_adds_three_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    for name in schema_certainty.TABLES:
        assert after[name] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_certainty.TABLES & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_certainty.TABLES <= _tables(database)


def test_the_downgrade_refuses_while_an_item_is_recorded(database):  # noqa: F811
    schema.upgrade_schema(database)
    _seed_item(database)
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert schema_certainty.TABLES <= _tables(database)


def test_one_item_per_attempt_and_known_states_only(database):  # noqa: F811
    schema.upgrade_schema(database)
    _seed_item(database)
    items = sa.Table('certainty_items', sa.MetaData(), autoload_with=database)
    values = dict(job_id='job-1', attempt_id='attempt-1', category='worker_lost', reference='ZZZZZZ',
                  state='open', version=1, created_at=NOW, updated_at=NOW)
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(items.insert().values(id='item-2', **values))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(items.update().where(items.c.id == 'item-1').values(outcome='probably'))


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE certainty_settings (id VARCHAR(40) PRIMARY KEY)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == PRIOR
