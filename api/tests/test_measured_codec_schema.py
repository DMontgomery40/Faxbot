"""Actual 0032 to 0056 upgrade: the fax coding each attempt asked for (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_measured_codec
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows merge order: 0056 comes after 0032 (rules in delivery).
PRIOR = '0032_rules_delivery'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _row(**changes):
    return {'id': 'c' * 32, 'job_id': 'a' * 32, 'attempt_id': 'b' * 32, 'number': '+15555550123', 'route': 'sip',
            'requested': 'MH', 'measured': 1, 'compared': 'MMR', 'pages': 1, 'bits': '{"MH": 698656, "MMR": 873336}',
            'receiver_known': 0, 'reason': 'MH: 20% shorter than MMR for these pages.', 'created_at': NOW,
            **changes}


def test_measured_codec_follows_rules_in_delivery():
    assert schema_measured_codec.REVISION == '0056_measured_codec' == schema.MEASURED_CODEC  # 0058 follows it
    assert schema.RULES_DELIVERY == PRIOR
    assert schema_measured_codec.TABLES == frozenset({'fax_coding_choices'})
    assert schema_measured_codec.TABLES <= schema.STRICT_TABLES


def test_0056_adds_one_empty_table_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    assert after['fax_coding_choices'] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_measured_codec.TABLES & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_measured_codec.TABLES <= _tables(database)


def test_one_row_per_attempt_and_only_known_codings(database):  # noqa: F811
    schema.upgrade_schema(database)
    table = sa.Table('fax_coding_choices', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(table.insert().values(**_row()))
    for bad in ({'id': 'd' * 32}, {'id': 'e' * 32, 'attempt_id': 'f' * 32, 'requested': 'T6'},
                {'id': 'e' * 32, 'attempt_id': 'f' * 32, 'compared': 'G4'},
                {'id': 'e' * 32, 'attempt_id': 'f' * 32, 'measured': 2},
                {'id': 'e' * 32, 'attempt_id': 'f' * 32, 'pages': 0}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(table.insert().values(**_row(**bad)))
    with database.begin() as connection:  # no comparison (one usable coding) is allowed
        connection.execute(table.insert().values(**_row(id='e' * 32, attempt_id='f' * 32, compared=None)))


def test_a_same_name_table_before_the_migration_is_refused(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE fax_coding_choices (id VARCHAR(40) PRIMARY KEY)')
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
