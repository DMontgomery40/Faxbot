"""Frozen 0015 inbound sources; registration and validation belong to ``schema``.

``inbound_imports.source`` names where a received document came from. 0010
allowed only phaxio, sinch, sip, import and test, so a fax fetched from eFax
could not be recorded with its own source. This revision widens that one CHECK
constraint, ``ck_inbound_imports_source``, to every built-in provider id:

- ``efax``: received faxes Faxbot fetches from the eFax Enterprise API.
- ``signalwire``, ``documo``, ``humblefax``, ``freeswitch``: providers that only
  send today. The constraint describes which values a row may hold, not which
  providers can receive, so a provider that gains receiving needs no new
  migration. Which sources Faxbot actually fetches stays in runtime code.

Nothing else changes: no column, index, key or row. On PostgreSQL the
constraint is dropped and added again; SQLite cannot alter a CHECK
constraint, so the table is rebuilt in the same guarded transaction with every
row copied unchanged, and its indexes are created again afterwards (index
names are shared across the schema). No table references ``inbound_imports``.
Runtime code reflects the table; it never imports this metadata.
"""
import sqlalchemy as sa

from . import schema_inbound
from .schema_batching import frozen_metadata as previous_metadata


REVISION = '0015_inbound_sources'
TABLES = frozenset()
TABLE = 'inbound_imports'
CONSTRAINT = 'ck_inbound_imports_source'
SOURCES = schema_inbound.SOURCES + ('efax', 'signalwire', 'documo', 'humblefax', 'freeswitch')
_REBUILD = '_inbound_imports_0015'


def _expression():
    return ' OR '.join(f"source = '{value}'" for value in SOURCES)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    metadata.remove(metadata.tables[TABLE])
    table = sa.Table(TABLE, metadata, *schema_inbound._definition(sources=SOURCES))
    for name, columns, unique in schema_inbound.INDEXES:
        sa.Index(name, *(table.c[column] for column in columns), unique=unique)
    return metadata


def upgrade_sources(connection, operations):
    # The guarded migration owns the transaction and validated the 0014 shape,
    # including the exact 0010 constraint, before this runs.
    inspector = sa.inspect(connection)
    if not inspector.has_table(TABLE) or inspector.has_table(_REBUILD):
        from .schema import SchemaUpgradeError
        raise SchemaUpgradeError('Inbound import table is not in the expected state for its migration.')
    if connection.dialect.name == 'postgresql':
        operations.drop_constraint(CONSTRAINT, TABLE, type_='check')
        operations.create_check_constraint(CONSTRAINT, TABLE, _expression())
        return
    columns = ', '.join(column.name for column in schema_inbound._definition() if isinstance(column, sa.Column))
    operations.create_table(_REBUILD, *schema_inbound._definition(sources=SOURCES))
    connection.exec_driver_sql(f'INSERT INTO {_REBUILD} ({columns}) SELECT {columns} FROM {TABLE}')
    operations.drop_table(TABLE)
    connection.exec_driver_sql(f'ALTER TABLE {_REBUILD} RENAME TO {TABLE}')
    for name, indexed, unique in schema_inbound.INDEXES:
        operations.create_index(name, TABLE, list(indexed), unique=unique)
