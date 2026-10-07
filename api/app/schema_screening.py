"""Frozen 0035 junk screening; registration and validation belong to ``schema``.

Junk senders an administrator marked are rejected before Asterisk answers
(``inbound/screening.py``): an unanswered call is not billed and leaves
nothing to triage. This revision adds two tables and changes no stored row:

- ``screened_callers``: one row per "Mark sender as junk": the caller's
  number in E.164, the reason, the received fax it came from (when it came
  from one), who marked it (principal ID and name as they were then), when,
  and when it expires (90 days by default). Removal sets ``removed_at``,
  ``removed_by`` and ``removed_by_name`` once; the row is never deleted or
  rewritten otherwise, so the history of who blocked whom stays.
- ``screened_call_rejections``: append-only, one row per call Asterisk turned
  away, keyed by the call's own key from Asterisk's queue (``<epoch>.<call>``),
  so a call recorded twice is one row: the number as presented, the number it
  called, when, the entry that matched (when one still did) and the call's SIP
  Call-ID.

No foreign key ties these rows to received faxes or people: retention may
remove a received fax, and the record of a blocked caller outlives it. The
downgrade drops both tables; their history is lost, so back up first.
Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_negotiation import frozen_metadata as previous_metadata


REVISION = '0035_junk_screening'
ORDER = ('screened_callers', 'screened_call_rejections')
TABLES = frozenset(ORDER)
INDEXES = (
    ('ix_screened_callers_number', 'screened_callers', ('number',), False),
    ('ix_screened_callers_expires', 'screened_callers', ('expires_at',), False),
    ('ix_screened_call_rejections_at', 'screened_call_rejections', ('rejected_at',), False),
    ('ix_screened_call_rejections_number', 'screened_call_rejections', ('number',), False),
)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'screened_callers': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('reason', sa.String(200), nullable=False),
            sa.Column('inbound_fax_id', sa.String(40), nullable=True),
            sa.Column('added_by', sa.String(40), nullable=True),
            sa.Column('added_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('removed_at', sa.DateTime(), nullable=True),
            sa.Column('removed_by', sa.String(40), nullable=True),
            sa.Column('removed_by_name', sa.String(200), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_screened_callers'),
            sa.CheckConstraint('removed_by IS NULL OR removed_at IS NOT NULL', name='ck_screened_callers_removed'),
        ),
        'screened_call_rejections': (
            sa.Column('id', sa.String(64), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('called', sa.String(32), nullable=True),
            sa.Column('entry_id', sa.String(40), nullable=True),
            sa.Column('call_id', sa.String(255), nullable=True),
            sa.Column('rejected_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_screened_call_rejections'),
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


def upgrade_screening(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Junk screening table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_screening(connection, operations):
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
