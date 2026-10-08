"""Actual 0056 to 0059 upgrade and downgrade: the codings each receiving machine takes, and polling (SQLite and
PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_routing_learning
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows merge order: 0059 comes after 0056 (measured codings).
PRIOR = '0056_measured_codec'


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


def _observation(**changes):
    return {'id': 'a' * 32, 'number': '+15555550123', 'max_length': 'a4', 'engine': 'hylafax', 'source': 'b' * 32,
            'observed_at': NOW, **changes}


def test_routing_learning_follows_measured_codings():
    assert schema_routing_learning.REVISION == '0059_routing_learning' == schema.HEAD
    assert schema.MEASURED_CODEC == PRIOR
    assert schema_routing_learning.TABLES == frozenset({'poll_sources', 'poll_requests', 'poll_results'})
    assert schema_routing_learning.TABLES <= schema.STRICT_TABLES


def test_0059_adds_the_polling_tables_and_one_column_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    observations = sa.Table('page_capability_observations', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(observations.insert().values(**_observation()))
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    assert after['page_capability_observations'][0]['codings'] is None
    assert all(after[name] == [] for name in schema_routing_learning.TABLES)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_routing_learning.TABLES & _tables(database)
    assert 'codings' not in _columns(database, 'page_capability_observations')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_routing_learning.TABLES <= _tables(database)


def test_the_downgrade_refuses_while_polling_rows_or_reported_codings_exist(database):  # noqa: F811
    schema.upgrade_schema(database)
    requests = sa.Table('poll_requests', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(requests.insert().values(id='c' * 32, number='+15555550123', requested_at=NOW))
    with pytest.raises(Exception, match='polling records'):
        _downgrade(database, PRIOR)
    with database.begin() as connection:
        connection.execute(requests.delete())
    observations = sa.Table('page_capability_observations', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(observations.insert().values(**_observation(codings='MH,MR,MMR,JBIG')))
    with pytest.raises(Exception, match='codings'):
        _downgrade(database, PRIOR)


def test_only_known_poll_outcomes_and_one_result_per_request(database):  # noqa: F811
    schema.upgrade_schema(database)
    results = sa.Table('poll_results', sa.MetaData(), autoload_with=database)
    row = {'id': 'd' * 32, 'request_id': 'c' * 32, 'outcome': 'received', 'sentence': 'Collected 2 pages.',
           'created_at': NOW}
    with database.begin() as connection:
        connection.execute(results.insert().values(**row))
    for bad in ({'id': 'e' * 32}, {'id': 'e' * 32, 'request_id': 'f' * 32, 'outcome': 'maybe'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(results.insert().values(**{**row, **bad}))
    sources = sa.Table('poll_sources', sa.MetaData(), autoload_with=database)
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(sources.insert().values(id='a' * 32, number='+15555550123', enabled=2, created_at=NOW))


def test_a_same_name_table_before_the_migration_is_refused(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE poll_requests (id VARCHAR(40) PRIMARY KEY)')
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
