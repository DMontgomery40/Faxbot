"""Frozen 0027 dialed number; registration and validation belong to ``schema``.

A recipient may approve another number for the same intake, usually a
toll-free one (``routing/alternates.py``). A fax keeps its original recipient
in ``fax_jobs.to_number``; this revision adds four nullable, additive columns
so the number Faxbot actually called is evidence of its own:

- ``outbound_deliveries.alternate_number``: the approved alternate (E.164)
  captured when the fax was accepted. NULL means no approval was in force, so
  a later approval or withdrawal never changes an accepted fax.
- ``outbound_deliveries.alternate_approval``: the identity of that approval
  record, kept for provenance. NULL when the source gave none.
- ``outbound_attempts.dialed_number``: the number this attempt called, written
  while the attempt is prepared and never changed after its durable submission
  marker. NULL means not recorded (an attempt from before this revision, or a
  route that places no call); such an attempt called ``fax_jobs.to_number``.
- ``outbound_attempts.dialed_approval``: the approval identity when the
  attempt called the approved alternate; NULL otherwise.

The downgrade drops the columns. Runtime code reflects these tables; it never
imports this metadata.
"""
import sqlalchemy as sa

from .schema_negotiation import frozen_metadata as previous_metadata


REVISION = '0027_dialed_number'
TABLES = frozenset()
COLUMNS = (
    ('outbound_deliveries', 'alternate_number', 32),
    ('outbound_deliveries', 'alternate_approval', 64),
    ('outbound_attempts', 'dialed_number', 32),
    ('outbound_attempts', 'dialed_approval', 64),
)


def _column(name, length):
    return sa.Column(name, sa.String(length), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, name, length in COLUMNS:
        metadata.tables[table].append_column(_column(name, length))
    return metadata


def upgrade_dialed(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    for table, name, _ in COLUMNS:
        if not inspector.has_table(table) or name in {column['name'] for column in inspector.get_columns(table)}:
            from .schema import SchemaUpgradeError
            raise SchemaUpgradeError('The sent fax tables are not in the expected state for their migration.')
    for table, name, length in COLUMNS:
        operations.add_column(table, _column(name, length))


def downgrade_dialed(connection, operations):
    # No constraint or index names these columns, so SQLite drops them too.
    for table, name, _ in reversed(COLUMNS):
        operations.drop_column(table, name)
