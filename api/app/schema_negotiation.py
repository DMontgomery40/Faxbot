"""Frozen 0023 fax call negotiation; registration and validation belong to ``schema``.

Each fax engine reports what a call negotiated, but not to the same extent
(see ``fax_negotiation``). This revision adds nullable columns to
``fax_engine_calls`` so every engine call, sent or received, can hold them.
Each column's name says what it covers, so a value about the last page is
never read as one about the whole call:

- ``negotiation_by``: the engine that reported the values below ('builtin' or
  'hylafax'). NULL means no engine reported a negotiation for the call.
- Whole call: ``rate_first`` (the first speed the call trained at, bit/s),
  ``rate_lowest`` (the lowest speed it trained at successfully), ``trainings``
  (how many times it trained), ``compression`` ('MH', 'MR', 'MMR', 'JBIG',
  'JPEG' or 'mixed'), ``resolution`` ('standard', 'fine', 'superfine' or
  'mixed') and ``ecm`` ('on', 'off' or 'mixed').
- Last page only: ``rate_last_page`` and ``resolution_last_page``.

NULL always means not reported, never a default. Values are filled once and
never rewritten. The downgrade drops the columns. Runtime code reflects the
table; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_capacity import frozen_metadata as previous_metadata


REVISION = '0023_negotiation'
TABLES = frozenset()
TABLE = 'fax_engine_calls'
COLUMNS = (
    ('negotiation_by', lambda: sa.String(16)),
    ('rate_first', sa.Integer),
    ('rate_lowest', sa.Integer),
    ('rate_last_page', sa.Integer),
    ('trainings', sa.Integer),
    ('compression', lambda: sa.String(8)),
    ('resolution', lambda: sa.String(16)),
    ('resolution_last_page', lambda: sa.String(16)),
    ('ecm', lambda: sa.String(8)),
)


def _column(name, kind):
    return sa.Column(name, kind(), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for name, kind in COLUMNS:
        metadata.tables[TABLE].append_column(_column(name, kind))
    return metadata


def upgrade_negotiation(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    present = {column['name'] for column in inspector.get_columns(TABLE)} if inspector.has_table(TABLE) else None
    if present is None or any(name in present for name, _ in COLUMNS):
        from .schema import SchemaUpgradeError
        raise SchemaUpgradeError('The fax engine call table is not in the expected state for its migration.')
    for name, kind in COLUMNS:
        operations.add_column(TABLE, _column(name, kind))


def downgrade_negotiation(connection, operations):
    # No constraint or index names these columns, so SQLite drops them too.
    for name, _ in reversed(COLUMNS):
        operations.drop_column(TABLE, name)
