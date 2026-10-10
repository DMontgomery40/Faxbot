"""Frozen 0071 codec decoder: which payload formats a recipient's decoder reads (brief 85 M2).

Registration and validation belong to ``schema``. The capacity layout (payload
format 2, ``codec/capacity.py``) is read only by a Faxbot decoder from October
2026 or later, so Faxbot chooses it by itself only for a recipient whose
decoder you recorded as reading it. This revision adds one nullable column to
each of two tables and changes no stored row:

- ``codec_numbers.decoder``: ``capacity`` when the recipient's decoder reads
  capacity pages; NULL (or ``any``) for any Faxbot decoder, which reads the
  format 1 layouts only.
- ``codec_number_changes.decoder``: the same, as each recorded change set it.

The downgrade drops the columns. Runtime code reflects these tables; it never
imports this metadata.
"""
import sqlalchemy as sa

from .schema_route_families import frozen_metadata as previous_metadata


REVISION = '0071_codec_decoder'
TABLES = frozenset()
DECODERS = ('any', 'capacity')
COLUMNS = (
    ('codec_numbers', 'decoder', 16),
    ('codec_number_changes', 'decoder', 16),
)


def _column(name, length):
    return sa.Column(name, sa.String(length), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, name, length in COLUMNS:
        metadata.tables[table].append_column(_column(name, length))
    return metadata


def upgrade_codec_decoder(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    for table, name, _ in COLUMNS:
        if not inspector.has_table(table) or name in {column['name'] for column in inspector.get_columns(table)}:
            from .schema import SchemaUpgradeError
            raise SchemaUpgradeError('The encoded-page tables are not in the expected state for their migration.')
    for table, name, length in COLUMNS:
        operations.add_column(table, _column(name, length))


def downgrade_codec_decoder(connection, operations):
    # No constraint or index names these columns, so SQLite drops them too.
    for table, name, _ in reversed(COLUMNS):
        operations.drop_column(table, name)
