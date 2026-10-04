"""Frozen 0014 sending together; registration and validation belong to ``schema``.

Short faxes to the same number can share one SIP trunk call when the
recipient agreed. This revision adds:

- ``batching_numbers``: the current setting for one fax number (on or off,
  how long a fax may wait, how many pages one call may carry, and whether
  faxes from different senders may share a call). It is mutable settings.
- ``batching_changes``: append-only history of every change to that setting,
  with who made it, when, and whether the person recorded that the recipient
  agreed. Rows are never updated.
- ``outbound_batch_members``: one row per fax held for a number that sends
  together. ``batch_id`` is the delivery attempt that placed the shared call
  (the call's identity) and ``attempt_id`` the fax's own attempt in it,
  ``document_number`` its place in the call;
  ``first_page``/``last_page`` are the fax's pages in that call, its separator
  page first. Each fax keeps its own delivery record, attempts and history.

Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_records import frozen_metadata as previous_metadata


REVISION = '0014_send_together'
# Creation order respects foreign keys.
ORDER = ('batching_numbers', 'batching_changes', 'outbound_batch_members')
TABLES = frozenset(ORDER)
MEMBER_STATES = ('waiting', 'together', 'separate')
CHANGE_ACTIONS = ('on', 'changed', 'off')
INDEXES = (
    ('uq_batching_numbers_number', 'batching_numbers', ('phone_number',), True),
    ('ix_batching_changes_number', 'batching_changes', ('phone_number', 'created_at'), False),
    ('ix_outbound_batch_members_waiting', 'outbound_batch_members', ('state', 'phone_number', 'hold_until'), False),
    ('ix_outbound_batch_members_batch', 'outbound_batch_members', ('batch_id',), False),
    ('ix_outbound_batch_members_attempt', 'outbound_batch_members', ('attempt_id',), False),
)


def _id(name='id', nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'batching_numbers': (
            _id(),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('enabled', sa.Integer(), nullable=False),
            sa.Column('max_wait_seconds', sa.Integer(), nullable=False),
            sa.Column('max_pages', sa.Integer(), nullable=False),
            sa.Column('mixed_senders', sa.Integer(), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_batching_numbers'),
            sa.CheckConstraint('enabled >= 0 AND enabled <= 1 AND mixed_senders >= 0 AND mixed_senders <= 1 '
                               'AND max_wait_seconds >= 60 AND max_wait_seconds <= 3600 '
                               'AND max_pages >= 2 AND max_pages <= 200 AND version >= 1',
                               name='ck_batching_numbers_values'),
        ),
        'batching_changes': (
            _id(),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('action', sa.String(16), nullable=False),
            # The person or key that made the change (a replay scope such as principal:<id>) and its name then.
            sa.Column('actor', sa.String(100), nullable=False),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('recipient_agreed', sa.Integer(), nullable=False),
            sa.Column('max_wait_seconds', sa.Integer(), nullable=False),
            sa.Column('max_pages', sa.Integer(), nullable=False),
            sa.Column('mixed_senders', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_batching_changes'),
            sa.CheckConstraint(_choice('action', CHANGE_ACTIONS), name='ck_batching_changes_action'),
            sa.CheckConstraint('recipient_agreed >= 0 AND recipient_agreed <= 1 '
                               'AND mixed_senders >= 0 AND mixed_senders <= 1', name='ck_batching_changes_values'),
        ),
        'outbound_batch_members': (
            # One row per fax: the primary key is the fax job identity.
            _id(),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('sender_scope', sa.String(100), nullable=False),
            sa.Column('sender_name', sa.String(200), nullable=True),
            sa.Column('pages', sa.Integer(), nullable=False),
            sa.Column('urgent', sa.Integer(), nullable=False),
            sa.Column('hold_until', sa.DateTime(), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            _id('batch_id', True),
            _id('attempt_id', True),
            sa.Column('document_number', sa.Integer(), nullable=True),
            sa.Column('documents', sa.Integer(), nullable=True),
            sa.Column('first_page', sa.Integer(), nullable=True),
            sa.Column('last_page', sa.Integer(), nullable=True),
            sa.Column('reference', sa.String(120), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_outbound_batch_members'),
            sa.CheckConstraint(_choice('state', MEMBER_STATES), name='ck_outbound_batch_members_state'),
            # NULL positions pass (a CHECK only rejects false); no grouping parentheses.
            sa.CheckConstraint('pages >= 1 AND urgent >= 0 AND urgent <= 1 AND document_number >= 1 AND documents >= 1 '
                               'AND first_page >= 1 AND last_page >= first_page',
                               name='ck_outbound_batch_members_values'),
            sa.ForeignKeyConstraint(['id'], ['fax_jobs.id'], name='fk_outbound_batch_members_job',
                                    ondelete='CASCADE'),
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


def upgrade_batching(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view before any DDL; never adopt an existing object.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Sending-together tables already exist before their migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)
