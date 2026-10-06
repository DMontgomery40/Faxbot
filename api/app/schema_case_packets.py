"""Frozen 0016 case packet sends; registration and validation belong to ``schema``.

A case packet leaves out documents the recipient already accepted and lists
them on a one-page index instead. ``case_documents`` remembers only what a
packet carried, so how much each packet left out was not kept. This revision
adds:

- ``case_packet_sends``: one row per case packet fax, written when it is
  queued. The primary key is the fax job identity. ``pages_sent`` is every
  page the fax carries (the index page included), ``pages_left_out`` the pages
  of the accepted documents it listed instead of sending, and
  ``documents_left_out`` how many such documents. Rows are never updated;
  whether the fax was delivered comes from its delivery record.

Savings from case packets are counted from the first row; packets sent before
this revision are not counted. Runtime code reflects this table; it never
imports this metadata.
"""
import sqlalchemy as sa

from .schema_inbound_sources import frozen_metadata as previous_metadata


REVISION = '0016_case_packet_sends'
ORDER = ('case_packet_sends',)
TABLES = frozenset(ORDER)
INDEXES = (
    ('ix_case_packet_sends_created_at', 'case_packet_sends', ('created_at',), False),
    ('ix_case_packet_sends_case', 'case_packet_sends', ('case_id', 'recipient'), False),
)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'case_packet_sends': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('case_id', sa.String(100), nullable=False),
            sa.Column('recipient', sa.String(32), nullable=False),
            sa.Column('pages_sent', sa.Integer(), nullable=False),
            sa.Column('pages_left_out', sa.Integer(), nullable=False),
            sa.Column('documents_left_out', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_case_packet_sends'),
            sa.CheckConstraint('pages_sent >= 1 AND pages_left_out >= 0 AND documents_left_out >= 0',
                               name='ck_case_packet_sends_values'),
            sa.ForeignKeyConstraint(['id'], ['fax_jobs.id'], name='fk_case_packet_sends_job', ondelete='CASCADE'),
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


def upgrade_case_packets(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view before any DDL; never adopt an existing object.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Case packet send table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)
