"""Actual 0053 to 0055 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0055 adds your NPIs, the NPPES reads and the numbers they list (M18), and a carrier's US prices by jurisdiction
(``jurisdiction_rates``). It changes no stored row; the downgrade refuses while an NPI or a price you entered is
saved.
"""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_number_advice
from api.tests.test_access_schema import at_revision
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 12, 0)
# The migration chain follows merge order: 0055 comes after 0053 (setup plans).
PRIOR = '0053_setup_plans'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _npi(connection, **values):
    row = {'id': 'npi-1', 'npi': '1234567893', 'label': 'Denver clinic', 'added_at': NOW, 'added_by_name': 'Ada',
           'removed_at': None, 'removed_by_name': None, **values}
    connection.execute(sa.text(
        'INSERT INTO organization_npis (id, npi, label, added_at, added_by_name, removed_at, removed_by_name) '
        'VALUES (:id, :npi, :label, :added_at, :added_by_name, :removed_at, :removed_by_name)'), row)


def _read(connection, **values):
    row = {'id': 'read-1', 'npi': '1234567893', 'name': 'SYNTHETIC CLINIC', 'enumeration_type': 'NPI-2',
           'purpose': 'own', 'read_at': NOW, 'record': '{}', **values}
    connection.execute(sa.text(
        'INSERT INTO nppes_reads (id, npi, name, enumeration_type, purpose, read_at, record) '
        'VALUES (:id, :npi, :name, :enumeration_type, :purpose, :read_at, :record)'), row)


def _number(connection, **values):
    row = {'id': 'number-1', 'read_id': 'read-1', 'npi': '1234567893', 'number': '+13035550101', 'kind': 'fax',
           'address_purpose': 'location', 'address': '1 Example Way, DENVER, CO', 'state': 'CO', **values}
    connection.execute(sa.text(
        'INSERT INTO nppes_numbers (id, read_id, npi, number, kind, address_purpose, address, state) '
        'VALUES (:id, :read_id, :npi, :number, :kind, :address_purpose, :address, :state)'), row)


def _rate(connection, **values):
    row = {'id': 'rate-1', 'route': 'sip-anveo', 'prefix': '1303555', 'currency': 'USD', 'interstate_micros': 2160,
           'intrastate_micros': 2060, 'billing_increment_seconds': 1, 'minimum_seconds': 1, 'source_url': None,
           'captured_on': NOW, 'superseded_at': None, 'created_at': NOW, **values}
    connection.execute(sa.text(
        'INSERT INTO jurisdiction_rates (id, route, prefix, currency, interstate_micros, intrastate_micros, '
        'billing_increment_seconds, minimum_seconds, source_url, captured_on, superseded_at, created_at) VALUES '
        '(:id, :route, :prefix, :currency, :interstate_micros, :intrastate_micros, :billing_increment_seconds, '
        ':minimum_seconds, :source_url, :captured_on, :superseded_at, :created_at)'), row)


def test_0055_follows_setup_plans():
    assert schema_number_advice.REVISION == '0055_number_advice'
    assert schema.SETUP_PLANS == PRIOR
    assert schema_number_advice.TABLES <= schema.STRICT_TABLES


def test_0055_keeps_every_row_adds_its_tables_and_downgrades(database):
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO provider_rate_cards (id, provider_id, direction, label, currency, per_minute_micros, "
            "per_page_micros, per_call_micros, billing_increment_seconds, minimum_seconds, source_url, captured_on, "
            "created_at) VALUES ('card-anveo', 'sip-anveo', 'outbound', 'AnveoDirect', 'USD', 1860, 0, 0, 1, 1, "
            "NULL, :now, :now)"), {'now': NOW})
    before = snapshot(database)
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    after = snapshot(database)
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    for name in schema_number_advice.ORDER:
        assert after[name] == [], name
    # Reads that list numbers are evidence, not decisions: they go with the downgrade.
    with database.begin() as connection:
        _read(connection)
        _number(connection)
    _downgrade(database, PRIOR)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
        for name in schema_number_advice.ORDER:
            assert not sa.inspect(connection).has_table(name)
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_the_database_refuses_impossible_rows_and_drops_numbers_with_their_read(database):
    schema.upgrade_schema(database)
    with database.begin() as connection:
        _npi(connection)
        _read(connection)
        _number(connection)
        _rate(connection)
    for insert, change in ((_read, {'id': 'read-x', 'purpose': 'guess'}),
                           (_number, {'id': 'number-x', 'kind': 'pager'}),
                           (_number, {'id': 'number-y', 'address_purpose': 'home'}),
                           (_number, {'id': 'number-z', 'read_id': 'no-such-read'}),
                           (_rate, {'id': 'rate-x', 'interstate_micros': -1}),
                           (_rate, {'id': 'rate-y', 'billing_increment_seconds': 0})):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                if database.dialect.name == 'sqlite':
                    connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                insert(connection, **change)
    with database.begin() as connection:
        if database.dialect.name == 'sqlite':
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        connection.execute(sa.text("DELETE FROM nppes_reads WHERE id = 'read-1'"))
        assert connection.execute(sa.text('SELECT count(*) FROM nppes_numbers')).scalar() == 0


@pytest.mark.parametrize('insert, message', [(_npi, 'Your NPI numbers are saved'),
                                             (_rate, 'Prices by state are saved')])
def test_downgrade_refuses_while_something_you_entered_is_saved(database, insert, message):
    schema.upgrade_schema(database)
    with database.begin() as connection:
        insert(connection)
    stored = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match=message):
        _downgrade(database, PRIOR)
    assert snapshot(database) == stored


def test_the_upgrade_refuses_a_table_that_already_exists(database):
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE nppes_reads (private_data VARCHAR(40))')
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
