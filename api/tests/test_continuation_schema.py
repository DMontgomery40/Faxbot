"""Actual 0051 to 0050 upgrade: page reports and continuations of broken faxes (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_continuation
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows merge order: 0050 comes after 0051 (invoices).
PRIOR = '0051_invoices'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _continuation(engine, **values):
    table = sa.Table('fax_continuations', sa.MetaData(), autoload_with=engine)
    row = dict(id='c-1', job_id='job-1', attempt_id='attempt-1', continuation_job_id='job-2', first_page=8,
               last_page=20, confirmed_by='builtin', created_at=NOW)
    row.update(values)
    with engine.begin() as connection:
        connection.execute(table.insert().values(**row))


def test_continuation_follows_invoices():
    assert schema_continuation.REVISION == '0050_continuation'
    assert schema.INVOICES == PRIOR
    assert schema_continuation.TABLES == {'fax_page_reports', 'fax_continuations'}
    assert schema_continuation.TABLES <= schema.STRICT_TABLES


def test_0050_adds_two_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    for name in schema_continuation.TABLES:
        assert after[name] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_continuation.TABLES & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_continuation.TABLES <= _tables(database)


def test_the_downgrade_refuses_while_a_continuation_is_recorded(database):  # noqa: F811
    schema.upgrade_schema(database)
    _continuation(database)
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert schema_continuation.TABLES <= _tables(database)


def test_one_continuation_per_broken_attempt_and_sensible_pages_only(database):  # noqa: F811
    schema.upgrade_schema(database)
    _continuation(database)
    for values in ({'id': 'c-2', 'continuation_job_id': 'job-3'},  # the same broken attempt again
                   {'id': 'c-2', 'attempt_id': 'attempt-2'},  # the same new fax again
                   {'id': 'c-2', 'attempt_id': 'attempt-2', 'continuation_job_id': 'job-3', 'first_page': 1},
                   {'id': 'c-2', 'attempt_id': 'attempt-2', 'continuation_job_id': 'job-3', 'last_page': 7},
                   {'id': 'c-2', 'attempt_id': 'attempt-2', 'continuation_job_id': 'job-3', 'confirmed_by': 'phaxio'}):
        with pytest.raises(sa.exc.IntegrityError):
            _continuation(database, **values)
    reports = sa.Table('fax_page_reports', sa.MetaData(), autoload_with=database)
    row = dict(job_id='job-1', attempt_id='attempt-1', source='hylafax', clean_pages=6, flagged_page=7,
               created_at=NOW)
    with database.begin() as connection:
        connection.execute(reports.insert().values(id='r-1', **row))
    for values in ({'id': 'r-2'}, {'id': 'r-2', 'source': 'sinch', 'pages_sent': -1},
                   {'id': 'r-2', 'source': 'phaxio'}, {'id': 'r-2', 'source': 'documo', 'flagged_page': 0}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(reports.insert().values(**{**row, **values}))


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE fax_continuations (id VARCHAR(40) PRIMARY KEY)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == PRIOR
