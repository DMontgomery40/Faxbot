"""Frozen 0020 delivery inside Faxbot; registration and validation belong to ``schema``.

A fax sent to one of the installation's own receiving numbers can become a
received fax on the same installation without a call. This revision adds what
that needs, and nothing else:

- ``inbound_imports.source`` may be ``local``: a received fax Faxbot delivered
  to itself from one of its own sent faxes. The CHECK constraint
  ``ck_inbound_imports_source`` is widened as 0015 widened it (dropped and added
  again on PostgreSQL; on SQLite the table is rebuilt in the same guarded
  transaction with every row copied unchanged and its indexes created again).
- ``fax_jobs.send_by_call``: a nullable flag on a sent fax, 1 when the sender
  asked for a real call through the carrier even to one of the installation's
  own numbers (the Setup wizard's test fax always does). NULL means no request.

The downgrade drops the column and narrows the constraint again; it refuses
while any received fax was delivered inside Faxbot, because narrowing would
reject those rows. Runtime code reflects these tables; it never imports this
metadata.
"""
import sqlalchemy as sa

from . import schema_inbound, schema_inbound_sources
from .schema_retired_permissions import frozen_metadata as previous_metadata


REVISION = '0020_local_delivery'
TABLES = frozenset()
TABLE = 'inbound_imports'
CONSTRAINT = 'ck_inbound_imports_source'
SOURCES = schema_inbound_sources.SOURCES + ('local',)
COLUMN = 'send_by_call'
_REBUILD = '_inbound_imports_0020'


def _flag():
    return sa.Column(COLUMN, sa.Integer(), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    metadata.remove(metadata.tables[TABLE])
    table = sa.Table(TABLE, metadata, *schema_inbound._definition(sources=SOURCES))
    for name, columns, unique in schema_inbound.INDEXES:
        sa.Index(name, *(table.c[column] for column in columns), unique=unique)
    metadata.tables['fax_jobs'].append_column(_flag())
    return metadata


def _rebuild(connection, operations, sources):
    """Swap the inbound import table for one with ``sources`` in its CHECK constraint (SQLite)."""
    columns = ', '.join(column.name for column in schema_inbound._definition() if isinstance(column, sa.Column))
    operations.create_table(_REBUILD, *schema_inbound._definition(sources=sources))
    connection.exec_driver_sql(f'INSERT INTO {_REBUILD} ({columns}) SELECT {columns} FROM {TABLE}')
    operations.drop_table(TABLE)
    connection.exec_driver_sql(f'ALTER TABLE {_REBUILD} RENAME TO {TABLE}')
    for name, indexed, unique in schema_inbound.INDEXES:
        operations.create_index(name, TABLE, list(indexed), unique=unique)


def _expression(sources):
    return ' OR '.join(f"source = '{value}'" for value in sources)


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_local_delivery(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    if (not inspector.has_table(TABLE) or inspector.has_table(_REBUILD)
            or COLUMN in {column['name'] for column in inspector.get_columns('fax_jobs')}):
        _refuse('Received-fax or sent-fax tables are not in the expected state for their migration.')
    operations.add_column('fax_jobs', _flag())
    if connection.dialect.name == 'postgresql':
        operations.drop_constraint(CONSTRAINT, TABLE, type_='check')
        operations.create_check_constraint(CONSTRAINT, TABLE, _expression(SOURCES))
        return
    _rebuild(connection, operations, SOURCES)


def downgrade_local_delivery(connection, operations):
    imports = sa.table(TABLE, sa.column('source'))
    if connection.execute(sa.select(sa.func.count()).select_from(imports).where(imports.c.source == 'local')).scalar():
        _refuse('Faxes delivered inside Faxbot are recorded; this revision cannot be undone without losing them.')
    operations.drop_column('fax_jobs', COLUMN)
    if connection.dialect.name == 'postgresql':
        operations.drop_constraint(CONSTRAINT, TABLE, type_='check')
        operations.create_check_constraint(CONSTRAINT, TABLE, _expression(schema_inbound_sources.SOURCES))
        return
    _rebuild(connection, operations, schema_inbound_sources.SOURCES)
