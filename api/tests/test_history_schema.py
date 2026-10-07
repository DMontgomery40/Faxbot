"""Actual 0021 to 0022 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0022 keeps history that earlier code rewrote or never stored: an import's
failures before it was fetched again, eFax deletion retries outside the
provider report, the addresses each email went to, and a carrier call's
records that were never priced.
"""
from datetime import datetime
import json

from alembic import command
from alembic.config import Config
import sqlalchemy as sa

from api.app import schema, schema_capacity, schema_history
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 6, 9, 0)
# What _write_mark stored: the provider report with Faxbot's retry state added, in sanitize_report's encoding.
ORIGINAL = {'fax': {'fax_id': 'efax-1', 'pages': 2}, 'listed_by': 'efax'}
MARK = {'state': 'pending', 'attempts': 2, 'since': '2026-10-05T08:00:00', 'next_at': '2026-10-05T08:04:00'}
STOPPED = {'state': 'stopped', 'attempts': 9, 'since': '2026-09-20T08:00:00'}


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _table(engine, name):
    return sa.Table(name, sa.MetaData(), autoload_with=engine)


def _seed(engine):
    faxes, imports = _table(engine, 'inbound_faxes'), _table(engine, 'inbound_imports')
    items, calls = _table(engine, 'intake_items'), _table(engine, 'sip_call_records')
    checks = _table(engine, 'carrier_call_checks')
    reports = {'import-1': _encode({**ORIGINAL, 'efax_delete': MARK}),
               'import-2': _encode({**ORIGINAL, 'efax_delete': STOPPED}),
               # Not Faxbot's mark, and not JSON at all: both are left exactly as they are.
               'import-3': _encode({**ORIGINAL, 'efax_delete': 'not a mark'}),
               'import-4': 'saved-report'}
    with engine.begin() as connection:
        for number, (identity, report) in enumerate(sorted(reports.items()), start=1):
            connection.execute(faxes.insert().values(
                id=f'inbound-{number}', from_number='+15555550100', to_number='+15555550123', status='received',
                backend='efax', created_at=NOW, received_at=NOW, updated_at=NOW))
            connection.execute(imports.insert().values(
                id=identity, source='efax', account='efax:0123456789ab', operation_id=f'900{number}', revision='',
                state='received', attempts=1, imported_at=NOW, acquired_at=NOW, artifact_digest='b' * 64,
                artifact_size=10, artifact_media_type='application/pdf', report=report,
                inbound_fax_id=f'inbound-{number}', created_at=NOW, updated_at=NOW))
        # A fax emailed before 0022: who it went to was never stored.
        connection.execute(items.insert().values(
            id='item-1', source='fax', inbound_fax_id='inbound-1', received_at=NOW, state='delivered', attempts=1,
            delivered_at=NOW, delivery_reference='<intake-item-1@clinic.example>', version=2, created_at=NOW,
            updated_at=NOW))
        connection.execute(calls.insert().values(
            id='call-1', direction='outbound', call_id='call-1', started_at=NOW, ended_at=NOW, disposition='answered',
            t38='unknown', fax_preference=0, created_at=NOW, updated_at=NOW))
        connection.execute(checks.insert().values(
            id='call-1', provider_id='telnyx', state='matched', checks=3, next_check_at=NOW, created_at=NOW,
            updated_at=NOW))
    return reports


def _columns(engine, table):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns(table)}


def _rows(engine, name):
    table = _table(engine, name)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table).order_by(table.c.id)).mappings()]


def test_history_follows_capacity():
    assert schema.HISTORY == schema_history.REVISION == '0022_history'
    assert schema.CAPACITY == schema_capacity.REVISION == '0021_capacity'
    assert schema_history.TABLES == frozenset({'inbound_import_failures', 'inbound_provider_deletions'})
    assert schema_history.TABLES <= schema.STRICT_TABLES


def test_0022_moves_deletion_marks_out_of_reports_keeps_every_other_row_and_downgrades(database):
    at_revision(database, '0021_capacity')
    reports = _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    for name, rows in before.items():
        if name in ('alembic_version', 'inbound_imports', 'intake_items', 'carrier_call_checks'):
            continue
        assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name

    # Each mark left the provider report, which reads again exactly as it was first written.
    imports = {row['id']: row for row in after['inbound_imports']}
    assert imports['import-1']['report'] == imports['import-2']['report'] == _encode(ORIGINAL)
    assert imports['import-3']['report'] == reports['import-3']
    assert imports['import-4']['report'] == 'saved-report'
    for row in before['inbound_imports']:
        assert {k: v for k, v in imports[row['id']].items() if k not in ('report', 'account_key')} == {
            k: v for k, v in row.items() if k != 'report'}
    deletions = _rows(database, 'inbound_provider_deletions')
    assert [(row['id'], row['inbound_fax_id'], row['state'], row['attempts'], row['since'], row['next_at'])
            for row in deletions] == [
        ('import-1', 'inbound-1', 'pending', 2, datetime(2026, 10, 5, 8), datetime(2026, 10, 5, 8, 4)),
        ('import-2', 'inbound-2', 'stopped', 9, datetime(2026, 9, 20, 8), None)]
    assert _rows(database, 'inbound_import_failures') == []

    # Older deliveries and call checks gain an empty column: not recorded, never guessed.
    [item] = after['intake_items']
    assert item['delivered_to'] is None
    assert {k: v for k, v in item.items() if k != 'delivered_to'} == before['intake_items'][0]
    [check] = _rows(database, 'carrier_call_checks')
    assert check['unpriced_records'] is None

    # A row written after the upgrade is dropped by the downgrade; marks go back into the report.
    with database.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO inbound_import_failures (id, import_id, inbound_fax_id, attempts, last_error, failed_at, "
            "resumed_at, resumed_by, created_at) VALUES ('failure-1', 'import-4', 'inbound-4', 30, "
            "'eFax sent something that is not a PDF.', :now, :now, 'person', :now)"), {'now': NOW})
        connection.execute(sa.text("UPDATE intake_items SET delivered_to = :to"),
                           {'to': json.dumps(['frontdesk@clinic.example'])})
    _downgrade(database, '0021_capacity')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0021_capacity'
        tables = set(sa.inspect(connection).get_table_names())
    assert not tables & schema_history.TABLES
    assert 'delivered_to' not in _columns(database, 'intake_items')
    assert 'unpriced_records' not in _columns(database, 'carrier_call_checks')
    restored = {row['id']: row['report'] for row in _rows(database, 'inbound_imports')}
    assert restored == reports
    assert snapshot(database)['intake_items'] == before['intake_items']

    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    assert len(_rows(database, 'inbound_provider_deletions')) == 2
