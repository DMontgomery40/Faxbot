"""Actual 0065 to 0063 upgrade and downgrade: per-number lossless tuning choices and what the engine reported tuning
on each call (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_encoder_tuning
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows the recovered integration order.
PRIOR = '0065_fact_advice'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _call(**changes):
    return {'id': 'a' * 32, 'call_key': 'b' * 32, 'job_id': 'c' * 32, 'number': '+15555550123', 'coding': 'JBIG',
            'pages': 3, 'tuned_pages': 2, 'tuned_bytes': 5629, 'plain_bytes': 29003, 'settings': '8/32,72/8',
            'digests': None, 'sslfax': 1, 'refused': 0, 'reason': None, 'created_at': NOW, **changes}


def test_encoder_tuning_follows_integrated_predecessor():
    assert schema_encoder_tuning.REVISION == '0063_encoder_tuning' == schema.ENCODER_TUNING
    assert schema.FACT_ADVICE == PRIOR
    assert schema_encoder_tuning.TABLES == frozenset({'recipient_coding_tuning', 'coding_tuning_calls',
                                                     'partner_fax_engines'})
    assert schema_encoder_tuning.TABLES <= schema.STRICT_TABLES


def test_0063_adds_the_tuning_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    assert all(after[name] == [] for name in schema_encoder_tuning.TABLES)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_encoder_tuning.TABLES & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_encoder_tuning.TABLES <= _tables(database)


def test_the_downgrade_refuses_while_choices_or_engine_reports_exist(database):  # noqa: F811
    schema.upgrade_schema(database)
    calls = sa.Table('coding_tuning_calls', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(calls.insert().values(**_call()))
    with pytest.raises(Exception, match='coding tuning records'):
        _downgrade(database, PRIOR)
    with database.begin() as connection:
        connection.execute(calls.delete())
    choices = sa.Table('recipient_coding_tuning', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(choices.insert().values(id='d' * 32, number='+15555550123', tune=None, tune_jbig=1,
                                                   created_at=NOW))
    with pytest.raises(Exception, match='coding tuning records'):
        _downgrade(database, PRIOR)


def test_only_known_codings_flags_and_one_report_per_call_and_coding(database):  # noqa: F811
    schema.upgrade_schema(database)
    calls = sa.Table('coding_tuning_calls', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(calls.insert().values(**_call()))
        connection.execute(calls.insert().values(**_call(id='e' * 32, coding='MR')))
    for bad in ({'id': 'f' * 32}, {'id': 'f' * 32, 'call_key': 'g' * 32, 'coding': 'MMR'},
                {'id': 'f' * 32, 'call_key': 'g' * 32, 'refused': 2}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(calls.insert().values(**_call(**bad)))
    choices = sa.Table('recipient_coding_tuning', sa.MetaData(), autoload_with=database)
    for bad in ({'tune': 1, 'tune_jbig': 0}, {'tune': None, 'tune_jbig': 2}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(choices.insert().values(id='h' * 32, number='+15555550123', created_at=NOW, **bad))


def test_a_same_name_table_before_the_migration_is_refused(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE coding_tuning_calls (id VARCHAR(40) PRIMARY KEY)')
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
