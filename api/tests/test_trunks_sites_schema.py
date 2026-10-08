"""Actual 0032 to 0033 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0033 adds ``provider_rate_rows``: a rate card's prices by where a call starts and the number it calls
(origin-rated quotes, provider-rules design §3.7). The downgrade refuses while a row is saved.
"""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_trunks_sites
from api.tests.test_access_schema import at_revision
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture


NOW = datetime(2026, 10, 7, 12, 0)
# The migration chain follows merge order: 0033 comes after 0032 (rules in delivery).
PRIOR = '0032_rules_delivery'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _card(connection, identity='card-gamma'):
    connection.execute(sa.text(
        "INSERT INTO provider_rate_cards (id, provider_id, direction, label, currency, per_minute_micros, "
        "per_page_micros, per_call_micros, billing_increment_seconds, minimum_seconds, source_url, captured_on, "
        "created_at) VALUES (:id, 'sip-gamma', 'outbound', 'Gamma', 'GBP', 10000, 0, 0, 60, 60, NULL, :now, :now)"),
        {'id': identity, 'now': NOW})


def _row(connection, **values):
    row = {'id': 'row-1', 'card_id': 'card-gamma', 'origin': 'country:GB', 'destination_prefix': '44',
           'per_minute_micros': 9000, 'per_page_micros': 0, 'per_call_micros': 0, 'billing_increment_seconds': 60,
           'minimum_seconds': 60, 'source_url': None, 'captured_on': NOW, 'superseded_at': None, 'created_at': NOW,
           **values}
    connection.execute(sa.text(
        'INSERT INTO provider_rate_rows (id, card_id, origin, destination_prefix, per_minute_micros, per_page_micros, '
        'per_call_micros, billing_increment_seconds, minimum_seconds, source_url, captured_on, superseded_at, '
        'created_at) VALUES (:id, :card_id, :origin, :destination_prefix, :per_minute_micros, :per_page_micros, '
        ':per_call_micros, :billing_increment_seconds, :minimum_seconds, :source_url, :captured_on, :superseded_at, '
        ':created_at)'), row)


def test_0033_comes_after_rules_in_delivery():
    assert schema_trunks_sites.REVISION == schema.TRUNKS_SITES == '0033_trunks_sites'
    assert schema.RULES_DELIVERY == PRIOR
    assert schema_trunks_sites.TABLES <= schema.STRICT_TABLES


def test_0033_keeps_every_row_adds_the_rate_rows_and_downgrades(database):
    at_revision(database, PRIOR)
    with database.begin() as connection:
        _card(connection)
    before = snapshot(database)
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    after = snapshot(database)
    for name, rows in before.items():
        if name != 'alembic_version':
            assert after[name] == rows, name
    assert after['provider_rate_rows'] == []
    _downgrade(database, PRIOR)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
        assert not sa.inspect(connection).has_table('provider_rate_rows')
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_the_database_refuses_impossible_prices_and_removes_rows_with_their_card(database):
    schema.upgrade_schema(database)
    with database.begin() as connection:
        _card(connection)
        _row(connection)
    for change in ({'per_minute_micros': -1}, {'per_page_micros': 101_000_000}, {'billing_increment_seconds': 0},
                   {'minimum_seconds': 3601}, {'card_id': 'no-such-card'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                if database.dialect.name == 'sqlite':
                    connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                _row(connection, id='row-x', **change)
    with database.begin() as connection:
        if database.dialect.name == 'sqlite':
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        connection.execute(sa.text("DELETE FROM provider_rate_cards WHERE id = 'card-gamma'"))
        assert connection.execute(sa.text('SELECT count(*) FROM provider_rate_rows')).scalar() == 0


def test_downgrade_refuses_while_a_price_you_entered_is_saved(database):
    schema.upgrade_schema(database)
    with database.begin() as connection:
        _card(connection)
        _row(connection)
    stored = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='Prices by where calls start are saved'):
        _downgrade(database, PRIOR)
    assert snapshot(database) == stored


def test_the_upgrade_refuses_a_table_that_already_exists(database):
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE provider_rate_rows (private_data VARCHAR(40))')
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
