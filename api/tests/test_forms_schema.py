"""Actual 0023 to 0038 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0038 adds the registered form tables and changes no existing table.
"""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_forms
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def test_registered_forms_are_head_after_negotiation():
    assert schema.HEAD == schema_forms.REVISION == '0038_registered_forms'
    assert schema.NEGOTIATION == '0023_negotiation'
    assert schema_forms.TABLES == {'forms', 'form_versions', 'form_deliveries'}
    assert schema_forms.TABLES <= schema.STRICT_TABLES


def test_0038_adds_empty_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, '0023_negotiation')
    calls = sa.Table('fax_engine_calls', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(calls.insert().values(id='call-1', direction='outbound', call_key='attempt-1',
                                                 job_id='job-1', engine='hylafax', created_at=NOW, updated_at=NOW))
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name
    assert all(after[name] == [] for name in schema_forms.TABLES)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, '0023_negotiation')
    assert not schema_forms.TABLES & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0023_negotiation'
    schema.upgrade_schema(database)
    assert schema_forms.TABLES <= _tables(database)


def test_the_upgrade_refuses_a_same_name_table(database):  # noqa: F811
    at_revision(database, '0023_negotiation')
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE forms (id VARCHAR(40) PRIMARY KEY)')
    with pytest.raises(Exception):
        schema.upgrade_schema(database)


def test_states_routes_and_sources_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    tables = {name: sa.Table(name, sa.MetaData(), autoload_with=database) for name in schema_forms.ORDER}
    with database.begin() as connection:
        connection.execute(tables['forms'].insert().values(id='form-1', name='Referral', origin='local',
                                                           created_at=NOW))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(tables['forms'].insert().values(id='form-2', name='X', origin='elsewhere',
                                                               created_at=NOW))
    row = dict(id='delivery-1', direction='outbound', route='direct', form_address='a' * 64, renderer='r',
               resolution='fine', page_hashes='[]', pages=1, state='sending', version=1, created_at=NOW,
               updated_at=NOW)
    with database.begin() as connection:
        connection.execute(tables['form_deliveries'].insert().values(**row, message_id='m' * 32))
    for change in ({'state': 'lost'}, {'route': 'pigeon'}, {'pages': 0}, {'direction': 'sideways'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(tables['form_deliveries'].insert().values(**{**row, 'id': 'other', **change}))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(tables['form_deliveries'].insert().values(**{**row, 'id': 'twin'}, message_id='m' * 32))
