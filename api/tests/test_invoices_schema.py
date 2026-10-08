"""Actual 0033 to 0051 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0051 adds invoice totals, received-fax charges and the faxes a provider listed
that Faxbot has no record of. It changes no stored row, and the downgrade
refuses while any invoice is recorded.
"""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_invoices
from api.tests.test_access_schema import at_revision
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows merge order: 0051 comes after 0033 (trunks and sites).
PRIOR = '0033_trunks_sites'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _invoice(engine, **values):
    table = sa.Table('provider_invoices', sa.MetaData(), autoload_with=engine)
    row = dict(id='invoice-1', account_key='humblefax', provider_id='humblefax', first_day='2026-09-01',
               last_day='2026-09-30', period_start=datetime(2026, 9, 1, 6), period_end=datetime(2026, 10, 1, 6),
               total='13.20', currency='USD', version=1, created_at=NOW)
    row.update(values)
    with engine.begin() as connection:
        connection.execute(table.insert().values(**row))


def test_invoices_follow_trunks_and_sites():
    assert schema_invoices.REVISION == '0051_invoices'
    assert schema.TRUNKS_SITES == PRIOR
    assert schema_invoices.TABLES == {'provider_invoices', 'provider_received_charges', 'provider_received_checks',
                                      'provider_fax_sweeps', 'provider_unrecorded_faxes'}


def test_0051_keeps_every_row_adds_the_tables_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    assert schema_invoices.TABLES <= _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_invoices.TABLES & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    restored = snapshot(database)
    assert {name: rows for name, rows in restored.items() if name != 'alembic_version'} == \
        {name: rows for name, rows in before.items() if name != 'alembic_version'}
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_an_invoice_total_larger_than_an_integer_column_is_kept_exactly(database):  # noqa: F811
    schema.upgrade_schema(database)
    _invoice(database, total='987654321012.345678')
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT total FROM provider_invoices')).scalar_one() == '987654321012.345678'


def test_the_downgrade_refuses_while_an_invoice_is_recorded(database):  # noqa: F811
    schema.upgrade_schema(database)
    _invoice(database)
    stored = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='Invoices are recorded'):
        _downgrade(database, PRIOR)
    assert snapshot(database) == stored


def test_the_upgrade_refuses_a_table_that_already_exists(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE provider_invoices (id VARCHAR(40) PRIMARY KEY)'))
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)


def test_a_version_and_its_period_are_unique_per_account(database):  # noqa: F811
    schema.upgrade_schema(database)
    _invoice(database)
    with pytest.raises(sa.exc.IntegrityError):
        _invoice(database, id='invoice-2')
    _invoice(database, id='invoice-2', version=2, supersedes_id='invoice-1', total='13.40')
    _invoice(database, id='invoice-3', account_key='sinch-uk', provider_id='sinch')
