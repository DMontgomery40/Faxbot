"""Actual 0040 to 0045 upgrade: partner discovery tables (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_discovery
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)
# The migration chain follows merge order: 0045 comes after 0040 (destination schedule).
PRIOR = '0040_destination_schedule'


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


def test_discovery_follows_the_destination_schedule():
    assert schema_discovery.REVISION == '0045_discovery'
    assert schema.DESTINATION_SCHEDULE == PRIOR
    assert schema.HEAD == schema_discovery.REVISION
    assert schema_discovery.TABLES <= schema.STRICT_TABLES
    assert len(schema_discovery.TABLES) == 7


def test_0045_adds_empty_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
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
    for name in schema_discovery.ORDER:
        assert after[name] == [], name
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not (schema_discovery.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_discovery.TABLES <= _tables(database)


@pytest.mark.parametrize('table, row', [
    ('direct_introduction_consents', {'id': 'c-1', 'peer_id': 'p-1', 'allowed': 1, 'created_at': NOW}),
    ('direct_introductions', {'id': 'i-1', 'first_peer_id': 'p-1', 'second_peer_id': 'p-2', 'first_outcome': 'told',
                              'second_outcome': 'told', 'created_at': NOW}),
    ('direct_dns_publications', {'id': 'd-1', 'number': '+15555550123', 'directory': 'directory.example',
                                 'record_name': '_faxbot.3.2.1.0.5.5.5.5.5.5.1.directory.example',
                                 'record_value': 'v=faxbot1', 'expires_at': NOW, 'created_at': NOW}),
])
def test_the_downgrade_refuses_while_people_decided_something(database, table, row):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, table).insert().values(**row))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert table in _tables(database)


def test_flags_ports_and_kinds_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    bad = [
        ('direct_discovery_settings', {'id': 's', 'well_known': 2, 'from_calls': 1, 'directories': '', 'created_at': NOW}),
        ('direct_discovery_settings', {'id': 's', 'well_known': 1, 'from_calls': -1, 'directories': '', 'created_at': NOW}),
        ('direct_discovery_hints', {'id': 'h', 'source': 'guess', 'source_ref': 'r', 'direction': 'out', 'host': 'a.example',
                                    'created_at': NOW}),
        ('direct_discovery_hints', {'id': 'h', 'source': 'frames', 'source_ref': 'r', 'direction': 'out',
                                    'host': 'a.example', 'port': 70000, 'created_at': NOW}),
        ('direct_discovery_lookups', {'id': 'l', 'kind': 'probe', 'host': 'a.example', 'url': 'https://a.example/',
                                      'outcome': 'faxbot', 'started_at': NOW, 'expires_at': NOW}),
        ('direct_discovery_suggestions', {'id': 'g', 'number': '+15555550123', 'source': 'call', 'organization': 'X',
                                          'signing_key': 'k', 'endpoint': 'https://a.example', 'created_at': NOW,
                                          'enrolled_peer_id': 'p-1'}),
        ('direct_introduction_consents', {'id': 'c', 'peer_id': 'p', 'allowed': 3, 'created_at': NOW}),
    ]
    for table, row in bad:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(_table(database, table).insert().values(**row))
    hints = _table(database, 'direct_discovery_hints')
    row = {'source': 'frames', 'source_ref': 'out:a1', 'direction': 'out', 'host': 'a.example', 'created_at': NOW}
    with database.begin() as connection:
        connection.execute(hints.insert().values(id='h1', **row))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(hints.insert().values(id='h2', **row))


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE direct_discovery_hints (id VARCHAR(40) PRIMARY KEY)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == PRIOR
