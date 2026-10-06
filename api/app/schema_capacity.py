"""Frozen 0021 capacity; registration and validation belong to ``schema``.

Faxbot admits a call only while the number it calls and the trunk it uses have
room (``capacity.py``). The calls already holding room are worked out from the
durable delivery records, so this revision stores only two inputs, both
nullable and additive:

- ``fax_jobs.urgent``: 1 when the sender marked the fax urgent; it goes before
  other faxes waiting for the same room. NULL means not urgent.
- ``delivery_destinations.max_calls``: how many calls at once this number takes.
  NULL means the default (one: a fax line answers one call at a time); 0 means
  no limit.

The downgrade drops both columns. Runtime code reflects these tables; it never
imports this metadata.
"""
import sqlalchemy as sa

from .schema_local_delivery import frozen_metadata as previous_metadata


REVISION = '0021_capacity'
TABLES = frozenset()
COLUMNS = (('fax_jobs', 'urgent'), ('delivery_destinations', 'max_calls'))


def _column(name):
    return sa.Column(name, sa.Integer(), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, column in COLUMNS:
        metadata.tables[table].append_column(_column(column))
    return metadata


def upgrade_capacity(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    for table, column in COLUMNS:
        if not inspector.has_table(table) or column in {c['name'] for c in inspector.get_columns(table)}:
            from .schema import SchemaUpgradeError
            raise SchemaUpgradeError('Fax and recipient tables are not in the expected state for their migration.')
    for table, column in COLUMNS:
        operations.add_column(table, _column(column))


def downgrade_capacity(connection, operations):
    for table, column in COLUMNS:
        operations.drop_column(table, column)
