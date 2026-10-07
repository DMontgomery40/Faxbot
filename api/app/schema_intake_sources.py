"""Frozen 0041 intake connectors; registration and validation belong to ``schema``.

Connectors bring documents in from an email mailbox or a watched folder, and
send faxes from email (email to fax) or from a folder (folder to fax). Every
document goes through the existing paths: received documents through the
generic import contract (``inbound_imports``, source ``import``), sent faxes
through ordinary fax acceptance with its idempotency key. These tables only
record what each connector saw and what became of it. This revision adds:

- ``intake_sources``: one connector. ``kind`` is ``email`` or ``folder``,
  ``direction`` is ``receive`` or ``send``. ``settings`` is JSON without
  secrets; ``secret_envelope`` holds the sealed mailbox password or sign-in
  secrets and, for a sending connector, the token of the connector's own
  ``fax:send`` key, whose public id and binding are kept in ``key_id`` and
  ``key_binding_id``. ``enabled`` 0 is paused; ``removed_at`` keeps a removed
  connector's history. ``claim_token``/``claim_expires_at`` lease one check at
  a time; ``last_*`` describe the latest check in one sentence.
- ``intake_source_senders``: for email to fax, which email address belongs to
  which Faxbot person. ``principal_id`` is a value, not a key, so removing a
  person never rewrites what a connector recorded.
- ``intake_source_items``: one row per source identity, unique on
  ``(source_id, operation_id, part)``: a message (by its Message-ID, or the
  SHA-256 of the whole message when it has none) and, when receiving, one
  part per attachment; a file by its SHA-256 (and, when sending, its
  destination). ``state`` is what became of it, ``reason`` the one sentence
  why; ``duplicates`` counts later copies of the same identity, which were
  never filed or sent again. ``reply_*`` record the one email reply an email
  sender gets, at most once per identity; an uncertain reply is never resent.
  The fax, import and received fax are named by value: cleaning up a fax never
  rewrites this record.

The downgrade refuses while any connector, sender or item is recorded, because
items are the record of what was filed and sent. Runtime code reflects these
tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_receiving_rules import frozen_metadata as previous_metadata


REVISION = '0041_intake_connectors'
ORDER = ('intake_sources', 'intake_source_senders', 'intake_source_items')
TABLES = frozenset(ORDER)
KINDS = ('email', 'folder')
DIRECTIONS = ('receive', 'send')
STATES = ('sending', 'imported', 'sent', 'refused', 'failed', 'conflict', 'uncertain')
REPLY_STATES = ('none', 'waiting', 'due', 'sent', 'failed', 'uncertain')
REPLY_KINDS = ('refused', 'result')
INDEXES = (
    ('uq_intake_sources_name', 'intake_sources', ('normalized_name',), True),
    ('ix_intake_sources_due', 'intake_sources', ('enabled', 'next_check_at'), False),
    ('uq_intake_source_senders_address', 'intake_source_senders', ('source_id', 'normalized_address'), True),
    ('uq_intake_source_items_identity', 'intake_source_items', ('source_id', 'operation_id', 'part'), True),
    ('ix_intake_source_items_recent', 'intake_source_items', ('source_id', 'created_at'), False),
    ('ix_intake_source_items_fax', 'intake_source_items', ('fax_job_id',), False),
    ('ix_intake_source_items_reply', 'intake_source_items', ('reply_state', 'reply_next_at'), False),
)


def _either(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'intake_sources': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('kind', sa.String(16), nullable=False),
            sa.Column('direction', sa.String(16), nullable=False),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('normalized_name', sa.String(100), nullable=False),
            sa.Column('enabled', sa.Integer(), nullable=False),
            sa.Column('settings', sa.Text(), nullable=False),
            sa.Column('secret_envelope', sa.Text(), nullable=True),
            sa.Column('key_id', sa.String(40), nullable=True),
            sa.Column('key_binding_id', sa.String(40), nullable=True),
            sa.Column('key_principal_id', sa.String(40), nullable=True),
            sa.Column('paused_reason', sa.String(300), nullable=True),
            sa.Column('last_checked_at', sa.DateTime(), nullable=True),
            sa.Column('last_ok', sa.Integer(), nullable=True),
            sa.Column('last_result', sa.String(300), nullable=True),
            sa.Column('next_check_at', sa.DateTime(), nullable=True),
            sa.Column('claim_token', sa.String(64), nullable=True),
            sa.Column('claim_expires_at', sa.DateTime(), nullable=True),
            sa.Column('removed_at', sa.DateTime(), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_intake_sources'),
            sa.CheckConstraint(_either('kind', KINDS), name='ck_intake_sources_kind'),
            sa.CheckConstraint(_either('direction', DIRECTIONS), name='ck_intake_sources_direction'),
            sa.CheckConstraint('(enabled = 0 OR enabled = 1) AND (last_ok = 0 OR last_ok = 1) AND version >= 1',
                               name='ck_intake_sources_flags'),
        ),
        'intake_source_senders': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('source_id', sa.String(40), nullable=False),
            sa.Column('address', sa.String(320), nullable=False),
            sa.Column('normalized_address', sa.String(320), nullable=False),
            sa.Column('principal_id', sa.String(40), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_intake_source_senders'),
            sa.ForeignKeyConstraint(['source_id'], ['intake_sources.id'],
                                    name='fk_intake_source_senders_source', ondelete='CASCADE'),
        ),
        'intake_source_items': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('source_id', sa.String(40), nullable=False),
            sa.Column('direction', sa.String(16), nullable=False),
            sa.Column('operation_id', sa.String(100), nullable=False),
            sa.Column('part', sa.String(40), nullable=False),
            sa.Column('reference', sa.String(255), nullable=True),
            sa.Column('subject', sa.String(200), nullable=True),
            sa.Column('sender', sa.String(320), nullable=True),
            sa.Column('sender_principal_id', sa.String(40), nullable=True),
            sa.Column('sender_name', sa.String(200), nullable=True),
            sa.Column('source_received_at', sa.DateTime(), nullable=True),
            sa.Column('document_digest', sa.String(64), nullable=True),
            sa.Column('submitted_digest', sa.String(64), nullable=True),
            sa.Column('to_number', sa.String(64), nullable=True),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('reason', sa.String(300), nullable=True),
            sa.Column('import_id', sa.String(40), nullable=True),
            sa.Column('inbound_fax_id', sa.String(40), nullable=True),
            sa.Column('fax_job_id', sa.String(40), nullable=True),
            sa.Column('duplicates', sa.Integer(), nullable=False),
            sa.Column('last_duplicate_at', sa.DateTime(), nullable=True),
            sa.Column('reply_to', sa.String(320), nullable=True),
            sa.Column('reply_state', sa.String(16), nullable=False),
            sa.Column('reply_kind', sa.String(16), nullable=True),
            sa.Column('reply_attempts', sa.Integer(), nullable=False),
            sa.Column('reply_next_at', sa.DateTime(), nullable=True),
            sa.Column('replied_at', sa.DateTime(), nullable=True),
            sa.Column('reply_note', sa.String(300), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_intake_source_items'),
            sa.CheckConstraint(_either('direction', DIRECTIONS), name='ck_intake_source_items_direction'),
            sa.CheckConstraint(_either('state', STATES), name='ck_intake_source_items_state'),
            sa.CheckConstraint(_either('reply_state', REPLY_STATES), name='ck_intake_source_items_reply_state'),
            sa.CheckConstraint(_either('reply_kind', REPLY_KINDS), name='ck_intake_source_items_reply_kind'),
            sa.CheckConstraint('duplicates >= 0 AND reply_attempts >= 0 AND version >= 1',
                               name='ck_intake_source_items_counters'),
            # History stays while its connector is kept (a removed connector is only marked removed).
            sa.ForeignKeyConstraint(['source_id'], ['intake_sources.id'], name='fk_intake_source_items_source'),
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


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_intake_sources(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table before any DDL; never adopt an object.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('Intake connector table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_intake_sources(connection, operations):
    # What each connector filed and sent is evidence; never drop it silently.
    stored = [sa.select(sa.func.count()).select_from(sa.table(name)) for name in ORDER]
    if any(connection.execute(query).scalar() for query in stored):
        _refuse('Intake connectors or their items are recorded; this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
