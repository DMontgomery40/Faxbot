"""Frozen 0037 case ledger; registration and validation belong to ``schema``.

Until this revision a case document counted as accepted once the fax that
carried it finished, and a document was only its exact bytes. This revision
records what the recipient acknowledged, separately from what was sent, and
gives every document its context:

- ``case_originals``: the case's original documents, kept unchanged (the file
  is stored once under the installation's data folder, in ``cases/``, named by
  its SHA-256). Each has a title, a document type, the date on the document,
  its source and its version. The same bytes from another source or as another
  version are another original. Rows are never updated. They follow the
  artifact retention setting: once that many days pass after an original was
  last added or sent, the cleanup deletes its row and, when no other original
  uses the same bytes, its file.
- ``case_original_removals``: append-only, one row per original the retention
  cleanup removed: its case, SHA-256, source and version, and when. No title
  or other content is kept, only enough to say that it was removed and when.
- ``case_entries``: the ledger. One row per case, recipient, exact bytes,
  source, version and purpose: the same bytes sent for another purpose, or as
  a newer version, are another entry. ``original_id`` is NULL for documents
  sent before Faxbot kept originals. Rows are never updated.
- ``case_entry_sends``: append-only, one row per fax that carried an entry,
  with its page range in that fax. Whether it was sent comes from that fax's
  delivery record.
- ``case_entry_events``: append-only acknowledgements and invalidations.
  ``accepted`` comes from a partner's signed receipt for the packet
  (``partner_receipt``), the receiving team acknowledging a packet delivered
  to one of the installation's own numbers (``work_acknowledged``), a received
  fax a person recorded as the recipient's acknowledgement (``received_fax``)
  or a person recording that the recipient confirmed it (``person``, with a
  note). ``invalidated`` (``cache_miss``) records that the recipient could not
  find a document a packet referred to. Automatic events carry a
  ``dedupe_key`` so the same evidence is recorded once.
- ``case_recipients``: per recipient, how many days a reference to an
  accepted document is trusted (NULL: Faxbot's default; 0: no limit).
- ``case_checklists``: a recipient's checklist of document types, dates and
  versions, as JSON items. A change is a new version; rows are never updated.
- ``case_packets``: one row per case packet fax sent after this revision: an
  update, a full-packet repair (with the person's reason) or a packet built
  from a checklist version, its purpose, and who sent it.

The upgrade copies each ``case_documents`` row into ``case_entries`` (empty
source, version and purpose) and its last carrying fax into
``case_entry_sends``. ``case_documents.accepted_at`` is left as it was: it
records fax success, and no longer allows a reference by itself. The
downgrade drops the tables only while they hold nothing but those copies;
otherwise it refuses, because acknowledgements would be lost. Runtime code
reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_engine_frames import frozen_metadata as previous_metadata


REVISION = '0037_case_ledger'
# Creation order respects foreign keys.
ORDER = ('case_originals', 'case_entries', 'case_entry_sends', 'case_entry_events', 'case_recipients',
         'case_checklists', 'case_packets', 'case_original_removals')
TABLES = frozenset(ORDER)
EVENT_KINDS = ('accepted', 'invalidated')
ACCEPTED_SOURCES = ('partner_receipt', 'work_acknowledged', 'received_fax', 'person')
INVALIDATED_SOURCES = ('cache_miss',)
PACKET_KINDS = ('update', 'repair', 'checklist')
# Rows a downgrade cannot rebuild: any of these refuses it.
KEPT = ('case_originals', 'case_entry_events', 'case_recipients', 'case_checklists', 'case_packets',
        'case_original_removals')
REMOVAL_REASONS = ('retention',)
INDEXES = (
    ('uq_case_originals_document', 'case_originals', ('case_id', 'digest', 'source', 'version'), True),
    ('ix_case_originals_case', 'case_originals', ('case_id', 'created_at'), False),
    ('uq_case_entries_document', 'case_entries',
     ('case_id', 'recipient', 'digest', 'source', 'version', 'purpose'), True),
    ('ix_case_entries_case', 'case_entries', ('case_id', 'recipient'), False),
    ('uq_case_entry_sends_carriage', 'case_entry_sends', ('entry_id', 'job_id'), True),
    ('ix_case_entry_sends_job', 'case_entry_sends', ('job_id',), False),
    ('uq_case_entry_events_dedupe', 'case_entry_events', ('entry_id', 'dedupe_key'), True),
    ('ix_case_entry_events_entry', 'case_entry_events', ('entry_id', 'occurred_at'), False),
    ('uq_case_recipients_phone_number', 'case_recipients', ('phone_number',), True),
    ('uq_case_checklists_version', 'case_checklists', ('name', 'version'), True),
    ('ix_case_packets_case', 'case_packets', ('case_id', 'recipient'), False),
    ('ix_case_original_removals_case', 'case_original_removals', ('case_id', 'digest'), False),
)


def _id(name='id', nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _who():
    return (sa.Column('principal_id', sa.String(40), nullable=True),
            sa.Column('principal_name', sa.String(200), nullable=True))


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    accepted = ' OR '.join(f"source = '{value}'" for value in ACCEPTED_SOURCES)
    invalidated = ' OR '.join(f"source = '{value}'" for value in INVALIDATED_SOURCES)
    return {
        'case_originals': (
            _id(),
            sa.Column('case_id', sa.String(100), nullable=False),
            sa.Column('digest', sa.String(64), nullable=False),
            sa.Column('title', sa.String(200), nullable=False),
            sa.Column('document_type', sa.String(100), nullable=False),
            # The date printed on the document (midnight), when one was given.
            sa.Column('document_date', sa.DateTime(), nullable=True),
            sa.Column('source', sa.String(120), nullable=False),
            sa.Column('version', sa.String(64), nullable=False),
            sa.Column('page_count', sa.Integer(), nullable=False),
            sa.Column('size_bytes', sa.Integer(), nullable=False),
            *_who(),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_case_originals'),
            sa.CheckConstraint('page_count >= 1 AND size_bytes >= 1', name='ck_case_originals_values'),
        ),
        'case_entries': (
            _id(),
            sa.Column('case_id', sa.String(100), nullable=False),
            sa.Column('recipient', sa.String(32), nullable=False),
            sa.Column('digest', sa.String(64), nullable=False),
            sa.Column('source', sa.String(120), nullable=False),
            sa.Column('version', sa.String(64), nullable=False),
            sa.Column('purpose', sa.String(120), nullable=False),
            sa.Column('title', sa.String(200), nullable=False),
            sa.Column('page_count', sa.Integer(), nullable=False),
            _id('original_id', nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_case_entries'),
            sa.CheckConstraint('page_count >= 1', name='ck_case_entries_pages'),
            sa.ForeignKeyConstraint(['original_id'], ['case_originals.id'],
                                    name='fk_case_entries_original', ondelete='SET NULL'),
        ),
        'case_entry_sends': (
            _id(),
            _id('entry_id'),
            _id('job_id'),
            sa.Column('first_page', sa.Integer(), nullable=True),
            sa.Column('last_page', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_case_entry_sends'),
            sa.CheckConstraint('(first_page IS NULL AND last_page IS NULL) '
                               'OR (first_page >= 1 AND last_page >= first_page)', name='ck_case_entry_sends_pages'),
            sa.ForeignKeyConstraint(['entry_id'], ['case_entries.id'],
                                    name='fk_case_entry_sends_entry', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_case_entry_sends_job', ondelete='CASCADE'),
        ),
        'case_entry_events': (
            _id(),
            _id('entry_id'),
            sa.Column('kind', sa.String(16), nullable=False),
            sa.Column('source', sa.String(24), nullable=False),
            # The packet fax the evidence is about, and the evidence row itself
            # (a direct delivery, a work item or a received fax), when there is one.
            _id('job_id', nullable=True),
            _id('evidence_id', nullable=True),
            sa.Column('note', sa.Text(), nullable=True),
            *_who(),
            sa.Column('dedupe_key', sa.String(80), nullable=True),
            sa.Column('occurred_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_case_entry_events'),
            sa.CheckConstraint(f"(kind = 'accepted' AND ({accepted})) OR (kind = 'invalidated' AND ({invalidated}))",
                               name='ck_case_entry_events_kind'),
            sa.ForeignKeyConstraint(['entry_id'], ['case_entries.id'],
                                    name='fk_case_entry_events_entry', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_case_entry_events_job', ondelete='SET NULL'),
        ),
        'case_recipients': (
            _id(),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('reuse_days', sa.Integer(), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_case_recipients'),
            sa.CheckConstraint('(reuse_days IS NULL OR (reuse_days >= 0 AND reuse_days <= 3650)) AND version >= 1',
                               name='ck_case_recipients_values'),
        ),
        'case_checklists': (
            _id(),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            # The recipient number the checklist was written for; NULL for any recipient.
            sa.Column('recipient', sa.String(32), nullable=True),
            sa.Column('items', sa.Text(), nullable=False),
            *_who(),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_case_checklists'),
            sa.CheckConstraint('version >= 1', name='ck_case_checklists_version'),
        ),
        'case_packets': (
            # The primary key is the fax job identity: one row per case packet fax.
            _id(),
            sa.Column('case_id', sa.String(100), nullable=False),
            sa.Column('recipient', sa.String(32), nullable=False),
            sa.Column('kind', sa.String(16), nullable=False),
            sa.Column('purpose', sa.String(120), nullable=False),
            sa.Column('reason', sa.Text(), nullable=True),
            _id('checklist_id', nullable=True),
            *_who(),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_case_packets'),
            sa.CheckConstraint(_choice('kind', PACKET_KINDS), name='ck_case_packets_kind'),
            sa.CheckConstraint("(kind = 'repair' AND reason IS NOT NULL) OR kind = 'update' "
                               "OR (kind = 'checklist' AND checklist_id IS NOT NULL)", name='ck_case_packets_why'),
            sa.ForeignKeyConstraint(['id'], ['fax_jobs.id'], name='fk_case_packets_job', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['checklist_id'], ['case_checklists.id'],
                                    name='fk_case_packets_checklist', ondelete='SET NULL'),
        ),
        'case_original_removals': (
            _id(),
            sa.Column('case_id', sa.String(100), nullable=False),
            sa.Column('digest', sa.String(64), nullable=False),
            sa.Column('source', sa.String(120), nullable=False),
            sa.Column('version', sa.String(64), nullable=False),
            sa.Column('reason', sa.String(16), nullable=False),
            sa.Column('removed_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_case_original_removals'),
            sa.CheckConstraint(_choice('reason', REMOVAL_REASONS), name='ck_case_original_removals_reason'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    definitions = _definitions()
    for name in ORDER:
        sa.Table(name, metadata, *definitions[name])
    for index, name, columns, unique in INDEXES:
        table = metadata.tables[name]
        sa.Index(index, *(table.c[column] for column in columns), unique=unique)
    return metadata


def _table(connection, name):
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def upgrade_case_ledger(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view before any DDL; never adopt an existing object.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER) or not inspector.has_table('case_documents'):
        raise SchemaUpgradeError('Case ledger tables are not in the expected state for their migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)
    # Copy each document a packet carried; its fax success is "sent", never "accepted".
    documents = _table(connection, 'case_documents')
    entries, sends = _table(connection, 'case_entries'), _table(connection, 'case_entry_sends')
    rows = connection.execute(sa.select(documents).order_by(documents.c.created_at, documents.c.id)).mappings().all()
    for row in rows:
        connection.execute(entries.insert().values(
            id=row['id'], case_id=row['case_id'], recipient=row['recipient'], digest=row['digest'], source='',
            version='', purpose='', title=row['title'], page_count=row['page_count'], original_id=None,
            created_at=row['created_at']))
        if row['source_job_id'] is not None:
            pages = {'first_page': row['first_page'], 'last_page': row['last_page']}
            if pages['first_page'] is None or pages['last_page'] is None:
                pages = {'first_page': None, 'last_page': None}
            connection.execute(sends.insert().values(
                id=row['id'], entry_id=row['id'], job_id=row['source_job_id'], created_at=row['created_at'], **pages))


def downgrade_case_ledger(connection, operations):
    from .schema import SchemaUpgradeError
    for name in KEPT:
        if connection.execute(sa.select(sa.func.count()).select_from(_table(connection, name))).scalar():
            raise SchemaUpgradeError('The case ledger holds acknowledgements, originals or checklists that a '
                                     'downgrade would lose; restore a verified backup instead.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
