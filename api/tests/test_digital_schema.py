"""Actual 0032 to 0052 upgrade: digital route tables (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_digital_routes
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows merge order: 0052 comes after 0032 (rules in delivery).
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


def _table(engine, name):
    return sa.Table(name, sa.MetaData(), autoload_with=engine)


def test_digital_routes_follow_rules_in_delivery():
    assert schema_digital_routes.REVISION == '0052_digital_routes'
    assert schema.HEAD == schema_digital_routes.REVISION
    assert schema.RULES_DELIVERY == PRIOR
    assert schema_digital_routes.TABLES <= schema.STRICT_TABLES
    assert len(schema_digital_routes.TABLES) == 5


def test_0052_adds_empty_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    jobs = _table(database, 'fax_jobs')
    with database.begin() as connection:
        connection.execute(jobs.insert().values(id='job-1', to_number='+15555550123', file_name='note.txt',
                                                tiff_path='', status='queued', pages=1, backend='sip',
                                                created_at=NOW, updated_at=NOW))
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    for name in schema_digital_routes.ORDER:
        assert after[name] == [], name
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not (schema_digital_routes.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_digital_routes.TABLES <= _tables(database)


@pytest.mark.parametrize('table, row', [
    ('digital_address_events', {'id': 'e-1', 'address_id': 'a-1', 'action': 'confirmed', 'created_at': NOW}),
    ('digital_messages', {'id': 'm-1', 'direction': 'out', 'kind': 'direct', 'account_key': 'hisp',
                          'message_id': '<a@b>', 'message_key': 'k' * 64, 'counterpart': 'records@example.net',
                          'state': 'submitted', 'created_at': NOW, 'updated_at': NOW}),
])
def test_the_downgrade_refuses_while_an_address_was_confirmed_or_a_message_kept(database, table, row):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, table).insert().values(**row))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert table in _tables(database)


def test_a_suggestion_alone_does_not_stop_the_downgrade(database):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'digital_address_events').insert().values(
            id='e-1', address_id='a-1', action='suggested', created_at=NOW))
    _downgrade(database, PRIOR)
    assert not (schema_digital_routes.TABLES & _tables(database))


def test_kinds_states_and_identities_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    address = {'phone_number': '+15555550123', 'kind': 'direct', 'address': 'records@example.net',
               'source': 'entered', 'created_at': NOW}
    message = {'direction': 'out', 'kind': 'direct', 'account_key': 'hisp', 'message_id': '<a@b>',
               'counterpart': 'records@example.net', 'state': 'submitted', 'created_at': NOW, 'updated_at': NOW}
    bad = [
        ('digital_addresses', {**address, 'id': 'a', 'kind': 'fax'}),
        ('digital_addresses', {**address, 'id': 'a', 'source': 'guess'}),
        ('digital_address_events', {'id': 'e', 'address_id': 'a', 'action': 'approved', 'created_at': NOW}),
        ('digital_messages', {**message, 'id': 'm', 'message_key': 'a' * 64, 'state': 'lost'}),
        ('digital_messages', {**message, 'id': 'm', 'message_key': 'a' * 64, 'direction': 'sideways'}),
        ('digital_messages', {**message, 'id': 'm', 'message_key': 'a' * 64, 'security': 'none'}),
        ('digital_trust_bundles', {'id': 't', 'account_key': 'hisp', 'content': '', 'sha256': 'x', 'anchors': -1,
                                   'created_at': NOW}),
    ]
    for table, row in bad:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(_table(database, table).insert().values(**row))
    with database.begin() as connection:
        connection.execute(_table(database, 'digital_addresses').insert().values(id='a1', **address))
        connection.execute(_table(database, 'digital_messages').insert().values(id='m1', message_key='b' * 64,
                                                                                **message))
    for table, row in (('digital_addresses', {**address, 'id': 'a2'}),
                       ('digital_messages', {**message, 'id': 'm2', 'message_key': 'b' * 64})):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(_table(database, table).insert().values(**row))


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE digital_messages (id VARCHAR(40) PRIMARY KEY)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == PRIOR
