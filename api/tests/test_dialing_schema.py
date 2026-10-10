"""Actual 0069 to 0068 upgrade: where Faxbot may dial (SQLite and PostgreSQL)."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_dialing
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 10, 9, 0)
# Chained after the integration head when merged: 0068 follows 0069_countries (merge order, not number order).
PRIOR = '0069_countries'


def test_dialing_follows_countries():
    assert schema_dialing.REVISION == '0068_dialing_guard' == schema.DIALING
    assert schema.COUNTRIES == PRIOR
    assert schema_dialing.TABLES <= schema.STRICT_TABLES and len(schema_dialing.TABLES) == 3


def test_0068_adds_empty_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(_table(database, 'fax_jobs').insert().values(
            id='job-1', to_number='+15555550123', file_name='note.txt', tiff_path='', status='queued', pages=1,
            backend='sip', created_at=NOW, updated_at=NOW))
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    for name in schema_dialing.ORDER:
        assert after[name] == [], name
    _downgrade(database, PRIOR)
    assert not (schema_dialing.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_dialing.TABLES <= _tables(database)


def test_the_downgrade_refuses_while_an_administrator_choice_is_kept_but_not_for_recorded_history(database):  # noqa: F811
    schema.upgrade_schema(database)
    delivered = {'id': 'c-1', 'class_key': 'country:GB', 'state': 'allowed', 'reason': 'delivered',
                 'first_delivered_at': NOW, 'created_at': NOW}
    with database.begin() as connection:
        connection.execute(_table(database, 'dialing_class_changes').insert().values(**delivered))
        connection.execute(_table(database, 'dialing_guard_state').insert().values(id='installation',
                                                                                   history_checked_at=NOW))
    _downgrade(database, PRIOR)
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'dialing_class_changes').insert().values(
            id='c-2', class_key='premium', state='allowed', reason='administrator', actor_name='Ada Admin',
            created_at=NOW))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert 'dialing_class_changes' in _tables(database)


def test_states_reasons_ceilings_and_the_singleton_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    change = {'class_key': 'premium', 'state': 'allowed', 'reason': 'administrator', 'created_at': NOW}
    bad = [
        ('dialing_class_changes', {**change, 'id': 'a', 'state': 'maybe'}),
        ('dialing_class_changes', {**change, 'id': 'a', 'reason': 'guess'}),
        ('dialing_class_changes', {**change, 'id': 'a', 'ceiling_micros': 250000}),          # no currency
        ('dialing_class_changes', {**change, 'id': 'a', 'ceiling_micros': -1, 'currency': 'USD'}),
        ('dialing_guard_state', {'id': 'other', 'history_checked_at': NOW}),
        ('dialing_guard_holds', {'id': 'missing-hold', 'job_id': 'missing-job', 'class_key': 'premium',
                                 'dialed_number': '+19005550100', 'why': 'fenced', 'created_at': NOW}),
    ]
    for table, row in bad:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                if connection.dialect.name == 'sqlite':
                    connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                connection.execute(_table(database, table).insert().values(**row))
