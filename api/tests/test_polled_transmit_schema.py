"""Actual 0063 to 0062 upgrade and downgrade: faxes held for another site to collect, their collections, and
the polling password and timetable on poll_sources (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_polled_transmit
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows the recovered integration order.
PRIOR = '0063_encoder_tuning'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _columns(engine, table):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns(table)}


def _source(**changes):
    return {'id': 'a' * 32, 'number': '+15555550123', 'enabled': 1, 'created_at': NOW, **changes}


def test_polled_transmit_follows_integrated_predecessor():
    assert schema_polled_transmit.REVISION == '0062_polled_transmit' == schema.POLLED_TRANSMIT
    assert schema.ENCODER_TUNING == PRIOR
    assert schema_polled_transmit.TABLES == frozenset({'poll_held', 'poll_collections'})
    assert schema_polled_transmit.TABLES <= schema.STRICT_TABLES


def test_0062_adds_the_held_fax_tables_and_five_columns_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    sources = sa.Table('poll_sources', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(sources.insert().values(**_source()))
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    kept = after['poll_sources'][0]
    assert all(kept[column] is None for _, column in schema_polled_transmit.ADDED_COLUMNS)
    assert all(after[name] == [] for name in schema_polled_transmit.TABLES)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_polled_transmit.TABLES & _tables(database)
    assert not {column for _, column in schema_polled_transmit.ADDED_COLUMNS} & _columns(database, 'poll_sources')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_polled_transmit.TABLES <= _tables(database)


def test_the_downgrade_refuses_while_held_faxes_collections_or_settings_exist(database):  # noqa: F811
    schema.upgrade_schema(database)
    held = sa.Table('poll_held', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(held.insert().values(id='c' * 32, number='+15555550123', document='pollq/faxhold-c.tif',
                                                pages=2, held_at=NOW))
    with pytest.raises(Exception, match='held fax records'):
        _downgrade(database, PRIOR)
    with database.begin() as connection:
        connection.execute(held.delete())
    sources = sa.Table('poll_sources', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(sources.insert().values(**_source(direction='hold')))
    with pytest.raises(Exception, match='direction, password or timetable'):
        _downgrade(database, PRIOR)


def test_only_known_collection_outcomes_and_one_row_per_engine_report(database):  # noqa: F811
    schema.upgrade_schema(database)
    collections = sa.Table('poll_collections', sa.MetaData(), autoload_with=database)
    row = {'id': 'd' * 32, 'held_id': 'c' * 32, 'outcome': 'sent', 'sentence': 'Collected by +15555550199.',
           'engine_ref': '0123456789abcdef:7-1', 'created_at': NOW}
    with database.begin() as connection:
        connection.execute(collections.insert().values(**row))
    for bad in ({'id': 'e' * 32}, {'id': 'e' * 32, 'engine_ref': 'x:1', 'outcome': 'maybe'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(collections.insert().values(**{**row, **bad}))
    # Withdrawals carry no engine reference, and several may exist.
    with database.begin() as connection:
        for suffix in 'fg':
            connection.execute(collections.insert().values(**{**row, 'id': suffix * 32, 'outcome': 'withdrawn',
                                                              'engine_ref': None}))
    held = sa.Table('poll_held', sa.MetaData(), autoload_with=database)
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(held.insert().values(id='h' * 32, number='+15555550123', document='x', pages=-1,
                                                    held_at=NOW))


def test_a_same_name_table_before_the_migration_is_refused(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE poll_held (id VARCHAR(40) PRIMARY KEY)')
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
