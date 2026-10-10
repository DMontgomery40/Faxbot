"""Actual upgrades and downgrades of 0069 (caller-ID prices, pins) and 0070 (closures, notices, country rules).

SQLite and PostgreSQL. The tables arrive empty and keep every earlier row; a downgrade refuses while something a
person entered is recorded (a confirmation, a pin, a notice) and succeeds once those tables are empty. Imported decks
and closure files never block a downgrade: they can be imported again.
"""
from datetime import datetime

import pytest

from api.app import schema, schema_closures, schema_countries
from api.app.schema import SchemaUpgradeError
from api.tests.test_access_schema import at_revision
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture


NOW = datetime(2026, 10, 10, 9, 0)
BEFORE_COUNTRIES = '0066_analysis'


def test_0069_and_0070_add_empty_tables_keep_every_row_and_downgrade(database):  # noqa: F811
    at_revision(database, BEFORE_COUNTRIES)
    with database.begin() as connection:
        connection.execute(_table(database, 'fax_jobs').insert().values(
            id='job-1', to_number='+15555550123', file_name='note.txt', tiff_path='', status='queued', pages=1,
            backend='sip', created_at=NOW, updated_at=NOW))
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}] and schema.HEAD == schema_closures.REVISION
    assert [row['id'] for row in after['fax_jobs']] == ['job-1']
    for name in schema_countries.ORDER + schema_closures.ORDER:
        assert after[name] == [], name
    _downgrade(database, schema.ROUTE_SELECTIONS)
    assert not (schema_closures.TABLES & _tables(database)) and schema_countries.TABLES <= _tables(database)
    _downgrade(database, BEFORE_COUNTRIES)
    assert not (schema_countries.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == BEFORE_COUNTRIES
    schema.upgrade_schema(database)
    assert (schema_countries.TABLES | schema_closures.TABLES) <= _tables(database)


@pytest.mark.parametrize('table, row', [
    ('line_notices', {'id': 'n-1', 'number': '+14165550100', 'carrier': 'Bell', 'closes_on': NOW, 'state': 'active',
                      'created_at': NOW}),
    ('service_eligibility', {'id': 'e-1', 'account': 'sip', 'country': 'AE', 'state': 'confirmed', 'evidence': 'x',
                             'created_at': NOW}),
])
def test_0070_refuses_to_downgrade_while_a_notice_or_confirmation_is_kept(database, table, row):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'copper_closures').insert().values(
            id='c-1', import_id='i-1', source='gouv', code_insee='75056', created_at=NOW))
        connection.execute(_table(database, table).insert().values(**row))
    with pytest.raises(SchemaUpgradeError):
        _downgrade(database, schema.ROUTE_SELECTIONS)
    with database.begin() as connection:
        connection.execute(_table(database, table).delete())
    _downgrade(database, schema.ROUTE_SELECTIONS)  # an imported closure file alone never blocks
    assert not (schema_closures.TABLES & _tables(database))


@pytest.mark.parametrize('table, row', [
    ('caller_id_eligibility', {'id': 'e-1', 'account': 'sip', 'caller_id': '+13035550100', 'state': 'confirmed',
                               'bought_here': 'no', 'created_at': NOW}),
    ('registered_sender_pins', {'id': 'p-1', 'recipient': '+902122220000', 'account': 'sip',
                                'caller_id': '+13035550100', 'state': 'active', 'created_at': NOW}),
    ('original_requests', {'id': 'o-1', 'job_id': 'job-1', 'recipient': '+902122220000', 'state': 'requested',
                           'created_at': NOW}),
])
def test_0069_refuses_to_downgrade_while_a_confirmation_pin_or_request_is_kept(database, table, row):  # noqa: F811
    schema.upgrade_schema(database)
    _downgrade(database, schema.COUNTRIES)
    with database.begin() as connection:
        connection.execute(_table(database, table).insert().values(**row))
    with pytest.raises(SchemaUpgradeError):
        _downgrade(database, BEFORE_COUNTRIES)
    with database.begin() as connection:
        connection.execute(_table(database, table).delete())
    _downgrade(database, BEFORE_COUNTRIES)
    assert not (schema_countries.TABLES & _tables(database))
