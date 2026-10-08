"""Actual 0052 to 0064 upgrade: expected fax tables (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_expected_faxes
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows merge order: 0064 comes after 0052 (digital routes) on this branch.
PRIOR = '0052_digital_routes'


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


EXPECTATION = {'code': 'ACD34', 'reference': 'PO 483', 'reference_key': 'po 483', 'kind': 'Acknowledgement',
               'mailbox_id': 'front', 'source_key': 'entered', 'operation_id': 'PO 483', 'revision': '',
               'state': 'open', 'window_start': NOW, 'version': 1, 'created_at': NOW, 'updated_at': NOW}


def test_expected_faxes_follow_digital_routes():
    assert schema_expected_faxes.REVISION == '0064_expected_faxes' == schema.HEAD
    assert schema.DIGITAL_ROUTES == PRIOR
    assert schema_expected_faxes.TABLES <= schema.STRICT_TABLES
    assert len(schema_expected_faxes.TABLES) == 9


def test_0064_adds_empty_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
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
    for name in schema_expected_faxes.ORDER:
        assert after[name] == [], name
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not (schema_expected_faxes.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_expected_faxes.TABLES <= _tables(database)


@pytest.mark.parametrize('table, row', [
    ('work_expectations', {'id': 'x-1', **EXPECTATION}),
    ('work_outages', {'id': 'o-1', 'code': 'QRT34', 'source_id': 's-1', 'started_at': NOW, 'version': 1,
                      'created_at': NOW, 'updated_at': NOW}),
    ('work_expectation_imports', {'id': 'i-1', 'source_id': 's-1', 'file_digest': 'a' * 64, 'full_export': 1,
                                  'state': 'complete', 'rows_total': 0, 'created_count': 0, 'unchanged_count': 0,
                                  'revised_count': 0, 'conflict_count': 0, 'missing_count': 0, 'problem_count': 0,
                                  'created_at': NOW}),
])
def test_the_downgrade_refuses_while_anything_people_recorded_is_kept(database, table, row):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, table).insert().values(**row))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert table in _tables(database)


def test_states_kinds_and_identities_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    event = {'expectation_id': 'x-1', 'kind': 'created', 'source': 'person', 'details': '{}', 'occurred_at': NOW,
             'created_at': NOW}
    link = {'expectation_id': 'x-1', 'inbound_fax_id': 'fax-1', 'strength': 'strong', 'signal': 'subaddress',
            'state': 'automatic', 'evidence': '{}', 'version': 1, 'created_at': NOW, 'updated_at': NOW}
    bad = [
        ('work_expectations', {**EXPECTATION, 'id': 'x', 'state': 'received'}),
        ('work_expectations', {**EXPECTATION, 'id': 'x', 'due_source': 'guess'}),
        ('work_expectation_events', {**event, 'id': 'e', 'kind': 'deleted'}),
        ('work_expectation_events', {**event, 'id': 'e', 'source': 'ocr'}),
        ('work_expectation_links', {**link, 'id': 'l', 'signal': 'timing'}),
        ('work_expectation_links', {**link, 'id': 'l', 'state': 'closed'}),
        ('work_outage_actions', {'id': 'a', 'outage_id': 'o', 'operation_id': 'PO 1', 'revision': '',
                                 'action': 'Sent by fax', 'channel': 'pigeon', 'outcome': 'done',
                                 'occurred_at': NOW, 'created_at': NOW}),
    ]
    for table, row in bad:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(_table(database, table).insert().values(**row))
    with database.begin() as connection:
        connection.execute(_table(database, 'work_expectations').insert().values(id='x-1', **EXPECTATION))
        connection.execute(_table(database, 'work_expectation_events').insert().values(
            id='e-1', dedupe_key='escalated:1', **event))
        connection.execute(_table(database, 'work_expectation_links').insert().values(id='l-1', **link))
    duplicates = [
        # The same source identity, the same code, a second escalation, the same fax linked twice.
        ('work_expectations', {**EXPECTATION, 'id': 'x-2', 'code': 'ACD35'}),
        ('work_expectations', {**EXPECTATION, 'id': 'x-3', 'operation_id': 'PO 484'}),
        ('work_expectation_events', {**event, 'id': 'e-2', 'dedupe_key': 'escalated:1'}),
        ('work_expectation_links', {**link, 'id': 'l-2'}),
    ]
    for table, row in duplicates:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(_table(database, table).insert().values(**row))


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE work_expectations (id VARCHAR(40) PRIMARY KEY)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == PRIOR
