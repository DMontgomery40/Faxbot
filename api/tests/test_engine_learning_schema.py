"""Actual 0044 to 0048 upgrade: engine learning epochs, per-number memory and what each call used (SQLite and
PostgreSQL)."""
from datetime import datetime, timedelta

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_engine_learning
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)
# The migration chain follows merge order: 0048 comes after 0044 (partner relay).
PRIOR = '0044_partner_relay'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _columns(engine, table):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns(table)}


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _seed(engine):
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id='job-1', to_number='+15555550123', file_name='note.txt',
                                                tiff_path='', status='queued', pages=1, backend='sip',
                                                created_at=NOW, updated_at=NOW))


def test_engine_learning_follows_partner_relay():
    assert schema_engine_learning.REVISION == '0048_engine_learning'
    assert schema.PARTNER_RELAY == PRIOR
    assert schema_engine_learning.TABLES == frozenset({'fax_learning_epochs', 'fax_destination_memory',
                                                       'fax_call_choices'})
    assert schema_engine_learning.TABLES <= schema.STRICT_TABLES


def test_0048_adds_three_empty_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    for name in schema_engine_learning.TABLES:
        assert after[name] == [], name
    # The far end's internet address frames, whole: two new columns, empty for every call recorded before.
    assert {'csa_full', 'tsa_full'} <= _columns(database, 'fax_call_frames')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_engine_learning.TABLES & _tables(database)
    assert not {'csa_full', 'tsa_full'} & _columns(database, 'fax_call_frames')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_engine_learning.TABLES <= _tables(database)


def _memory(**extra):
    row = {'id': 'm-1', 'number': '+15555550123', 'direction': 'outbound', 'kind': 't38_failed', 'engine': 'builtin',
           'evidence': 'call-1', 'epoch_id': 'e-1', 'learned_at': NOW, 'expires_at': NOW + timedelta(days=30),
           'created_at': NOW}
    row.update(extra)
    return row


def _choice(**extra):
    row = {'id': 'c-1', 'attempt_id': 'attempt-1', 'job_id': 'job-1', 'number': '+15555550123', 'engine': 'builtin',
           'audio': 1, 't38_now': 0, 'max_rate': None, 'compression': None, 'ecm_on': 0, 'iaf': None,
           'reasons': 'Fax over IP (T.38) to this number failed on 6 October.', 'created_at': NOW}
    row.update(extra)
    return row


def test_directions_kinds_flags_and_one_row_per_call_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    metadata = sa.MetaData()
    memory = sa.Table('fax_destination_memory', metadata, autoload_with=database)
    choices = sa.Table('fax_call_choices', metadata, autoload_with=database)
    with database.begin() as connection:
        connection.execute(memory.insert().values(**_memory()))
        connection.execute(choices.insert().values(**_choice()))
    for table, bad in ((memory, _memory(id='m-2', evidence='call-2', direction='sideways')),
                       (memory, _memory(id='m-2', evidence='call-2', kind='slow')),
                       (memory, _memory(id='m-2', evidence='call-2', forgotten_by='u-1')),
                       # The same call teaches the same thing once.
                       (memory, _memory(id='m-2')),
                       (choices, _choice(id='c-2', attempt_id='attempt-2', audio=2)),
                       (choices, _choice(id='c-2', attempt_id='attempt-2', engine='modem')),
                       # One row per attempt.
                       (choices, _choice(id='c-2'))):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(table.insert().values(**bad))


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE fax_call_choices (id VARCHAR(40) PRIMARY KEY)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == PRIOR
