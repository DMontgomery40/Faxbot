"""Actual 0040 to 0041 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0041 adds intake connectors, their email senders and the items each one saw.
The downgrade refuses while any of them is recorded.
"""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_intake_sources, schema_receiving_rules
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)
# The migration chain follows merge order: 0041 comes after 0040 (recipient schedules).
PRIOR = '0040_destination_schedule'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _table(engine, name):
    return sa.Table(name, sa.MetaData(), autoload_with=engine)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _seed(engine):
    """Rows the tables around 0041 already had: a received fax with its import, and an SMTP connector."""
    faxes, imports = _table(engine, 'inbound_faxes'), _table(engine, 'inbound_imports')
    connectors = _table(engine, 'intake_connectors')
    with engine.begin() as connection:
        connection.execute(faxes.insert().values(id='inbound-1', from_number='+15555550100',
                                                 to_number='+15555550123', status='received', backend='import',
                                                 created_at=NOW, received_at=NOW, updated_at=NOW))
        connection.execute(imports.insert().values(
            id='import-1', source='import', account='import:connector:source-1', operation_id='m:' + 'a' * 64,
            revision='1', state='received', attempts=0, imported_at=NOW, acquired_at=NOW,
            artifact_digest='b' * 64, artifact_size=10, artifact_media_type='application/pdf',
            inbound_fax_id='inbound-1', created_at=NOW, updated_at=NOW))
        connection.execute(connectors.insert().values(id='connector-1', kind='email', name='Front desk email',
                                                      enabled=1, settings='{}', version=1, created_at=NOW,
                                                      updated_at=NOW))


def _write_connector_records(engine):
    with engine.begin() as connection:
        insert = lambda table, **values: connection.execute(_table(engine, table).insert().values(**values))  # noqa: E731
        insert('intake_sources', id='source-1', kind='email', direction='receive', name='Scans mailbox',
               normalized_name='scans mailbox', enabled=1, settings='{}', version=1, created_at=NOW, updated_at=NOW)
        insert('intake_sources', id='source-2', kind='email', direction='send', name='Email to fax',
               normalized_name='email to fax', enabled=0, settings='{}', key_id='abcdef012345',
               key_binding_id='binding-1', key_principal_id='integration-1', paused_reason='Paused by Jane Smith.',
               version=2, created_at=NOW, updated_at=NOW)
        insert('intake_source_senders', id='sender-1', source_id='source-2', address='Jane@Example.com',
               normalized_address='jane@example.com', principal_id='person-1', created_at=NOW)
        insert('intake_source_items', id='item-1', source_id='source-1', direction='receive',
               operation_id='m:' + 'a' * 64, part='1', reference='<scan-1@example.com>', state='imported',
               import_id='import-1', inbound_fax_id='inbound-1', duplicates=2, reply_state='none',
               reply_attempts=0, version=1, created_at=NOW, updated_at=NOW)
        insert('intake_source_items', id='item-2', source_id='source-2', direction='send',
               operation_id='m:' + 'c' * 64, part='', sender='jane@example.com', sender_principal_id='person-1',
               sender_name='Jane Smith', to_number='+13035550100', state='sent', fax_job_id='job-1',
               duplicates=0, reply_to='jane@example.com', reply_state='waiting', reply_kind='result',
               reply_attempts=0, version=1, created_at=NOW, updated_at=NOW)


def test_intake_connectors_follow_destination_schedules():
    assert schema.RECEIVING_RULES == schema_receiving_rules.REVISION == '0030_receiving_accounts'
    assert schema_intake_sources.REVISION == '0041_intake_connectors'
    assert schema.DESTINATION_SCHEDULE == PRIOR
    assert schema_intake_sources.TABLES == frozenset({'intake_sources', 'intake_source_senders',
                                                      'intake_source_items'})
    assert schema_intake_sources.TABLES <= schema.STRICT_TABLES


def test_the_revision_before_0041_alone_is_a_valid_schema(database):
    at_revision(database, PRIOR)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    assert not schema_intake_sources.TABLES & _tables(database)
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_0041_keeps_every_row_and_downgrades_when_empty(database):
    at_revision(database, PRIOR)
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name == 'alembic_version':
            continue
        # Each earlier table keeps every row on its original columns; later revisions may add columns.
        rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
        assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    for name in schema_intake_sources.TABLES:
        assert after[name] == [], name

    _downgrade(database, PRIOR)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    assert not schema_intake_sources.TABLES & _tables(database)
    restored = snapshot(database)
    assert {name: rows for name, rows in restored.items() if name != 'alembic_version'} == \
        {name: rows for name, rows in before.items() if name != 'alembic_version'}
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_downgrade_refuses_while_connectors_or_items_are_recorded(database):
    at_revision(database, PRIOR)
    _seed(database)
    schema.upgrade_schema(database)
    _write_connector_records(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    stored = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='Intake connectors or their items are recorded'):
        _downgrade(database, PRIOR)
    assert snapshot(database) == stored


def test_constraints_refuse_what_connectors_cannot_say(database):
    at_revision(database, PRIOR)
    _seed(database)
    schema.upgrade_schema(database)
    _write_connector_records(database)
    refused = [
        # One item per source identity: a second copy only counts as a duplicate.
        "INSERT INTO intake_source_items (id, source_id, direction, operation_id, part, state, duplicates, "
        "reply_state, reply_attempts, version, created_at, updated_at) VALUES ('item-x', 'source-1', 'receive', "
        "'m:" + 'a' * 64 + "', '1', 'imported', 0, 'none', 0, 1, :now, :now)",
        "INSERT INTO intake_source_items (id, source_id, direction, operation_id, part, state, duplicates, "
        "reply_state, reply_attempts, version, created_at, updated_at) VALUES ('item-x', 'source-1', 'receive', "
        "'x', '', 'maybe', 0, 'none', 0, 1, :now, :now)",
        "INSERT INTO intake_source_items (id, source_id, direction, operation_id, part, state, duplicates, "
        "reply_state, reply_attempts, version, created_at, updated_at) VALUES ('item-x', 'missing', 'receive', "
        "'x', '', 'imported', 0, 'none', 0, 1, :now, :now)",
        "UPDATE intake_source_items SET reply_state = 'later'",
        "UPDATE intake_source_items SET reply_kind = 'thanks'",
        "UPDATE intake_source_items SET duplicates = -1",
        "UPDATE intake_sources SET kind = 'fax'",
        "UPDATE intake_sources SET direction = 'both'",
        "UPDATE intake_sources SET enabled = 2",
        "UPDATE intake_sources SET normalized_name = 'scans mailbox'",
        "INSERT INTO intake_source_senders (id, source_id, address, normalized_address, principal_id, created_at) "
        "VALUES ('sender-x', 'source-2', 'JANE@example.com', 'jane@example.com', 'person-2', :now)",
        "DELETE FROM intake_sources WHERE id = 'source-1'",
    ]
    for statement in refused:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                if connection.dialect.name == 'sqlite':
                    connection.exec_driver_sql('PRAGMA foreign_keys = ON')
                connection.execute(sa.text(statement), {'now': NOW})
    # Removing an email sender's connector takes its senders; a connector with items is kept, only marked removed.
    with database.begin() as connection:
        if connection.dialect.name == 'sqlite':
            connection.exec_driver_sql('PRAGMA foreign_keys = ON')
        connection.execute(sa.text("DELETE FROM intake_source_items WHERE source_id = 'source-2'"))
        connection.execute(sa.text("DELETE FROM intake_sources WHERE id = 'source-2'"))
    assert snapshot(database)['intake_source_senders'] == []
