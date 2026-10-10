"""Actual 0073 to 0074 upgrade: the station check (SQLite and PostgreSQL)."""
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from api.app import schema, schema_station_check
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision


NOW = datetime(2026, 10, 10, 9, 0)
PRIOR = '0070_closures'  # re-chained at merge (was 0073_header_notice)


def test_station_check_follows_the_header_notice_and_is_the_only_head():
    assert schema_station_check.REVISION == '0074_station_check' == schema.HEAD
    assert schema.CLOSURES == PRIOR
    assert schema_station_check.TABLES <= schema.STRICT_TABLES and len(schema_station_check.TABLES) == 3
    from pathlib import Path
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config()
    config.set_main_option('script_location', str(Path(schema.__file__).resolve().parents[1] / 'alembic'))
    assert ScriptDirectory.from_config(config).get_heads() == [schema.HEAD]


def test_0074_adds_empty_tables_downgrades_and_refuses_while_a_row_is_kept(database):  # noqa: F811
    at_revision(database, PRIOR)
    schema.upgrade_schema(database)
    assert schema_station_check.TABLES <= _tables(database)
    _downgrade(database, PRIOR)
    assert not (schema_station_check.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'station_check_settings').insert().values(
            id='s-1', scope='recipient', scope_key='+13035550150', mode='refuse', created_at=NOW))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert 'station_check_settings' in _tables(database)


def test_sources_scopes_modes_and_outcomes_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    station = {'phone_number': '+13035550150', 'station': '17205550199', 'source': 'call', 'seen_at': NOW,
               'expires_at': NOW + timedelta(days=180)}
    setting = {'scope': 'recipient', 'scope_key': '+13035550150', 'mode': 'warn', 'created_at': NOW}
    result = {'job_id': 'missing-job', 'phone_number': '+13035550150', 'station': '17205550199',
              'outcome': 'refused', 'engine': 'builtin', 'created_at': NOW}
    bad = [
        ('recipient_stations', {**station, 'id': 'a', 'source': 'guess'}),
        ('station_check_settings', {**setting, 'id': 'b', 'scope': 'team'}),
        ('station_check_settings', {**setting, 'id': 'b', 'mode': 'ignore'}),
        ('station_check_results', {**result, 'id': 'c'}),
    ]
    for table, row in bad:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                if connection.dialect.name == 'sqlite':
                    connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                connection.execute(_table(database, table).insert().values(**row))
