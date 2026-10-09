"""Actual 0036 to 0037 upgrade: the case ledger tables, the copy of earlier documents, and the downgrade rule."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_case_ledger
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9)
# The migration chain follows merge order: 0037 comes after 0036 (engine frames).
PRIOR = '0036_engine_frames'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _table(engine, name):
    return sa.Table(name, sa.MetaData(), autoload_with=engine)


def _rows(engine, name):
    table = _table(engine, name)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table).order_by(table.c.id)).mappings()]


def _seed(engine):
    jobs, documents = _table(engine, 'fax_jobs'), _table(engine, 'case_documents')
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id='job-1', to_number='+15555550123', status='SUCCESS', pages=41,
                                                file_name='case.pdf', tiff_path='', backend='phaxio',
                                                created_at=NOW, updated_at=NOW))
        # Accepted under the old rule (its fax finished); not yet sent by any fax; sent without page numbers.
        connection.execute(documents.insert().values(
            id='doc-1', case_id='claim-1', recipient='+15555550123', digest='a' * 64, title='Medical record',
            page_count=40, first_page=2, last_page=41, source_job_id='job-1', accepted_at=NOW, created_at=NOW))
        connection.execute(documents.insert().values(
            id='doc-2', case_id='claim-1', recipient='+15555550123', digest='b' * 64, title='Cover letter',
            page_count=1, first_page=None, last_page=None, source_job_id=None, accepted_at=None, created_at=NOW))
        connection.execute(documents.insert().values(
            id='doc-3', case_id='claim-1', recipient='+15555550199', digest='a' * 64, title='Medical record',
            page_count=40, first_page=None, last_page=None, source_job_id='job-1', accepted_at=None, created_at=NOW))


def test_case_ledger_follows_engine_frames():
    assert schema_case_ledger.REVISION == '0037_case_ledger'
    assert schema.ENGINE_FRAMES == PRIOR
    assert schema_case_ledger.TABLES <= schema.STRICT_TABLES
    assert set(schema_case_ledger.KEPT) < schema_case_ledger.TABLES


def test_0037_copies_earlier_documents_as_sent_keeps_every_row_and_validates(database):
    at_revision(database, PRIOR)
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            # Each earlier table keeps every row on its original columns; later revisions may add columns.
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    # Each earlier document becomes a ledger entry with no source, version or purpose ...
    entries = _rows(database, 'case_entries')
    assert [(row['id'], row['recipient'], row['digest'][:1], row['source'], row['version'], row['purpose'],
             row['title'], row['page_count'], row['original_id']) for row in entries] == [
        ('doc-1', '+15555550123', 'a', '', '', '', 'Medical record', 40, None),
        ('doc-2', '+15555550123', 'b', '', '', '', 'Cover letter', 1, None),
        ('doc-3', '+15555550199', 'a', '', '', '', 'Medical record', 40, None)]
    # ... its carrying fax is kept, and fax success no longer counts as the recipient's acceptance.
    assert [(row['entry_id'], row['job_id'], row['first_page'], row['last_page'])
            for row in _rows(database, 'case_entry_sends')] == [('doc-1', 'job-1', 2, 41), ('doc-3', 'job-1', None, None)]
    for name in schema_case_ledger.KEPT:
        assert after[name] == [], name
    metadata = schema_case_ledger.frozen_metadata(dialect=database.dialect.name)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        for name in schema_case_ledger.ORDER:
            table = metadata.tables[name]
            assert inspector.get_pk_constraint(name)['name'] == f'pk_{name}'
            assert {c['name'] for c in inspector.get_check_constraints(name)} == {
                c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
            assert {f['name'] for f in inspector.get_foreign_keys(name)} == {f.name for f in table.foreign_key_constraints}
            assert {(i['name'], tuple(i['column_names']), bool(i['unique'])) for i in inspector.get_indexes(name)} == {
                (index, columns, unique) for index, owner, columns, unique in schema_case_ledger.INDEXES
                if owner == name}
    schema.upgrade_schema(database)
    assert snapshot(database) == after


@pytest.mark.parametrize('conflict', ['table', 'view'])
def test_reserved_case_ledger_namespace_refuses_without_changes(database, conflict):
    at_revision(database, PRIOR)
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql('CREATE TABLE case_entries (private_data VARCHAR(40))')
        else:
            connection.exec_driver_sql('CREATE VIEW case_checklists AS SELECT id FROM access_state')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


def test_case_ledger_checks_reject_bad_values(database):
    schema.upgrade_schema(database)
    _seed_jobs = _table(database, 'fax_jobs')
    with database.begin() as connection:
        connection.execute(_seed_jobs.insert().values(id='job-1', to_number='+15555550123', status='queued', pages=5,
                                                      file_name='case.pdf', tiff_path='', backend='phaxio',
                                                      created_at=NOW, updated_at=NOW))
    metadata = schema_case_ledger.frozen_metadata(dialect=database.dialect.name)
    t = metadata.tables
    entry = dict(id='entry-1', case_id='claim-1', recipient='+15555550123', digest='a' * 64, source='', version='',
                 purpose='', title='Record', page_count=4, original_id=None, created_at=NOW)
    with database.begin() as connection:
        connection.execute(t['case_entries'].insert().values(**entry))
    event = dict(id='event-1', entry_id='entry-1', kind='accepted', source='person', note='Confirmed by phone',
                 occurred_at=NOW, created_at=NOW)
    bad_rows = [
        ('case_entries', {**entry, 'id': 'entry-2'}),  # the same document, recipient, source, version and purpose
        ('case_entries', {**entry, 'id': 'entry-3', 'purpose': 'Appeal', 'page_count': 0}),
        ('case_entry_events', {**event, 'kind': 'invalidated'}),  # a person's confirmation is not an invalidation
        ('case_entry_events', {**event, 'source': 'fax_success'}),
        ('case_entry_events', {**event, 'entry_id': 'missing'}),
        ('case_entry_sends', dict(id='send-1', entry_id='entry-1', job_id='job-1', first_page=3, last_page=2,
                                  created_at=NOW)),
        ('case_recipients', dict(id='r-1', phone_number='+15555550123', reuse_days=-1, version=1, created_at=NOW,
                                 updated_at=NOW)),
        ('case_packets', dict(id='job-1', case_id='claim-1', recipient='+15555550123', kind='repair', purpose='',
                              reason=None, created_at=NOW)),  # a repair always records why
        ('case_packets', dict(id='job-1', case_id='claim-1', recipient='+15555550123', kind='resend', purpose='',
                              created_at=NOW)),
    ]
    for name, values in bad_rows:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(t[name].insert().values(**values))
    # Same bytes for another purpose is another entry; one automatic event per piece of evidence.
    with database.begin() as connection:
        connection.execute(t['case_entries'].insert().values(**{**entry, 'id': 'entry-4', 'purpose': 'Appeal'}))
        connection.execute(t['case_entry_events'].insert().values(**{**event, 'dedupe_key': 'partner_receipt:d-1',
                                                                      'source': 'partner_receipt'}))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(t['case_entry_events'].insert().values(
                **{**event, 'id': 'event-2', 'dedupe_key': 'partner_receipt:d-1', 'source': 'partner_receipt'}))
    # Two people's confirmations have no dedupe key and are both kept.
    with database.begin() as connection:
        connection.execute(t['case_entry_events'].insert().values(**{**event, 'id': 'event-3'}))
        connection.execute(t['case_entry_events'].insert().values(**{**event, 'id': 'event-4'}))
        connection.execute(t['case_recipients'].insert().values(id='r-2', phone_number='+15555550123',
                                                                reuse_days=None, version=1, created_at=NOW,
                                                                updated_at=NOW))
    assert len(_rows(database, 'case_entry_events')) == 3


def test_downgrade_drops_only_copies_and_refuses_when_acknowledgements_would_be_lost(database):
    at_revision(database, PRIOR)
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    _downgrade(database, PRIOR)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
        assert not set(sa.inspect(connection).get_table_names()) & schema_case_ledger.TABLES
    assert snapshot(database) == before
    schema.upgrade_schema(database)
    events = _table(database, 'case_entry_events')
    with database.begin() as connection:
        connection.execute(events.insert().values(id='event-1', entry_id='doc-1', kind='accepted', source='person',
                                                  note='Confirmed by phone', occurred_at=NOW, created_at=NOW))
    kept = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='downgrade would lose'):
        _downgrade(database, PRIOR)
    assert snapshot(database) == kept
