"""Frozen 0025 one index page for a shared call; registration and validation belong to ``schema``.

Sending together puts a separator page before each document in a shared
call. When the recipient has also agreed to it, one index page listing each
document's page range replaces those separators. This revision adds nullable
columns only:

- ``batching_numbers.index_page``: 1 when the number's faxes sent together
  start with one index page instead of a separator before each document.
  NULL or 0 means separators.
- ``batching_changes.index_page``: that setting after the change.
  ``batching_changes.index_page_agreed``: 1 when the change itself recorded
  the recipient's agreement to an index page. NULL on changes made before
  this revision.
- ``outbound_batch_members.layout``: the layout of the call the fax went in,
  fixed when the call was formed: 'separators' or 'index_page'. NULL while
  the fax waits or goes on its own, and on calls formed before this
  revision, which used separators. With 'index_page', ``first_page`` and
  ``last_page`` are the fax's own pages (page 1 is the index page); with
  separators, ``first_page`` is its separator page.

Existing rows are never rewritten. The downgrade drops the columns. Runtime
code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_negotiation import frozen_metadata as previous_metadata


REVISION = '0025_shared_manifest'
TABLES = frozenset()
LAYOUTS = ('separators', 'index_page')
COLUMNS = (
    ('batching_numbers', 'index_page', sa.Integer),
    ('batching_changes', 'index_page', sa.Integer),
    ('batching_changes', 'index_page_agreed', sa.Integer),
    ('outbound_batch_members', 'layout', lambda: sa.String(16)),
)


def _column(name, kind):
    return sa.Column(name, kind(), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, name, kind in COLUMNS:
        metadata.tables[table].append_column(_column(name, kind))
    return metadata


def upgrade_shared_manifest(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    for table, name, _ in COLUMNS:
        if not inspector.has_table(table) or name in {column['name'] for column in inspector.get_columns(table)}:
            from .schema import SchemaUpgradeError
            raise SchemaUpgradeError('Sending-together tables are not in the expected state for their migration.')
    for table, name, kind in COLUMNS:
        operations.add_column(table, _column(name, kind))


def downgrade_shared_manifest(connection, operations):
    # No constraint or index names these columns, so SQLite drops them too.
    for table, name, _ in reversed(COLUMNS):
        operations.drop_column(table, name)
