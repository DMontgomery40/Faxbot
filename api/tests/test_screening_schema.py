"""Actual 0023 to 0035 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0035 adds the blocked senders and the log of calls turned away before answer, and changes no stored row.
"""
from datetime import datetime

from alembic import command
from alembic.config import Config
import sqlalchemy as sa

from api.app import schema, schema_screening
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 12, 0)


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def test_screening_follows_negotiation():
    assert schema_screening.REVISION == '0035_junk_screening'
    assert schema.NEGOTIATION == '0023_negotiation'
    assert schema_screening.TABLES == frozenset({'screened_callers', 'screened_call_rejections'})
    assert schema_screening.TABLES <= schema.STRICT_TABLES


def test_0035_adds_empty_tables_keeps_every_row_and_downgrades(database):
    at_revision(database, '0023_negotiation')
    faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(faxes.insert().values(id='inbound-1', from_number='+13035550142', to_number='+13035550100',
                                                 status='received', backend='sip', created_at=NOW, received_at=NOW,
                                                 updated_at=NOW))
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert schema_screening.TABLES <= _tables(database)
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name
    assert after['screened_callers'] == [] and after['screened_call_rejections'] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    with database.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO screened_callers (id, number, reason, created_at, expires_at) "
            "VALUES ('entry-1', '+13035550142', 'Junk', :now, :now)"), {'now': NOW})
        # Removed by someone: removed_at is required with removed_by.
        try:
            with connection.begin_nested():
                connection.execute(sa.text(
                    "INSERT INTO screened_callers (id, number, reason, created_at, expires_at, removed_by) "
                    "VALUES ('entry-2', '+13035550142', 'Junk', :now, :now, 'p1')"), {'now': NOW})
            refused = False
        except sa.exc.IntegrityError:
            refused = True
        assert refused
    _downgrade(database, '0023_negotiation')
    assert not schema_screening.TABLES & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0023_negotiation'
    schema.upgrade_schema(database)
    assert schema_screening.TABLES <= _tables(database)


def test_a_same_name_table_before_0035_is_refused(database):
    at_revision(database, '0023_negotiation')
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE screened_callers (id VARCHAR(40) PRIMARY KEY)')
    try:
        schema.upgrade_schema(database)
        upgraded = True
    except schema.SchemaUpgradeError:
        upgraded = False
    assert not upgraded
