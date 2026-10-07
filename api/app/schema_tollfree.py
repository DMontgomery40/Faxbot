"""Frozen 0026 toll-free approvals; registration and validation belong to ``schema``.

A recipient may publish a toll-free fax number that reaches the same intake as
its ordinary number. Calls to a toll-free number are paid by the recipient, so
Faxbot uses one for a recipient only once someone at the recipient has agreed
and the administrator has recorded who agreed, when, and the evidence.

``toll_free_approvals`` is append-only: every change is a new row and no row is
ever updated or deleted by Faxbot. The newest row for a recipient's number is
its current state:

- ``noted``: the toll-free number is on file, not yet approved;
- ``approved``: ``approved_by`` (the recipient's person, as written), on
  ``approved_on`` (the day they agreed), with ``evidence`` (where the agreement
  is recorded, such as an email subject and date);
- ``withdrawn``: the approval no longer holds.

``phone_number`` is the recipient's ordinary number and ``alternate_number``
the toll-free one, both in E.164. ``recorded_by`` and ``recorded_by_name`` are
the person who recorded the row, as they were then. Runtime code reflects the
table; it never imports this metadata. The downgrade drops the table, and its
rows with it.
"""
import sqlalchemy as sa

from .schema_shared_manifest import frozen_metadata as previous_metadata


REVISION = '0026_tollfree_approval'
TABLE = 'toll_free_approvals'
TABLES = frozenset({TABLE})
ACTIONS = ('noted', 'approved', 'withdrawn')
INDEXES = (
    ('ix_toll_free_approvals_number', ('phone_number', 'created_at'), False),
)


def _definition():
    actions = ' OR '.join(f"action = '{value}'" for value in ACTIONS)
    return (
        sa.Column('id', sa.String(40), nullable=False),
        sa.Column('phone_number', sa.String(32), nullable=False),
        sa.Column('alternate_number', sa.String(32), nullable=False),
        sa.Column('action', sa.String(16), nullable=False),
        sa.Column('approved_by', sa.String(200), nullable=True),
        sa.Column('approved_on', sa.DateTime(), nullable=True),
        sa.Column('evidence', sa.Text(), nullable=True),
        sa.Column('recorded_by', sa.String(40), nullable=True),
        sa.Column('recorded_by_name', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_toll_free_approvals'),
        sa.CheckConstraint(actions, name='ck_toll_free_approvals_action'),
    )


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    table = sa.Table(TABLE, metadata, *_definition())
    for name, columns, unique in INDEXES:
        sa.Index(name, *(table.c[column] for column in columns), unique=unique)
    return metadata


def upgrade_tollfree(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table or view before any DDL.
    if sa.inspect(connection).has_table(TABLE):
        from .schema import SchemaUpgradeError
        raise SchemaUpgradeError('The toll-free approval table already exists before its migration.')
    operations.create_table(TABLE, *_definition())
    for name, columns, unique in INDEXES:
        operations.create_index(name, TABLE, list(columns), unique=unique)


def downgrade_tollfree(connection, operations):
    for name, _, _ in reversed(INDEXES):
        operations.drop_index(name, table_name=TABLE)
    operations.drop_table(TABLE)
