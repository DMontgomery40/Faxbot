"""Frozen 0077 line inventory: your fax lines with their addresses and contracts, carrier lists of discontinued or
grandfathered service areas, and the kind of each line notice (brief 95, N19).

Registration and validation belong to ``schema``. This revision adds two append-only tables and three nullable
columns to ``line_notices``, and changes no stored row:

- ``line_inventory``: one row per line of one imported inventory (``routing/inventory.py``): the number, its
  service address (``street``, ``city``, ``region``, ``postal_code``, ``country``), the ``carrier``, the
  ``product`` or USOC, the serving ``wire_center`` (its CLLI code) and ``distribution_area`` when known, the
  telecom contract's end (``contract_end``), what the line is used for (``line_use``: ``fax``, ``alarm``,
  ``elevator``, ``emergency``, ``other`` or ``unknown``), and its monthly price as entered (``monthly_amount``, a
  decimal string, with ``currency``; NULL when not entered, never zero). ``import_id`` groups one import; a newer
  import supersedes the older rows (``superseded_at``).
- ``carrier_service_areas``: one row per area of one imported carrier list: AT&T's Discontinued TDM Service Areas
  workbook (read as published), or any carrier's list saved as CSV with the documented columns. ``carrier`` is
  the carrier's key (``att`` for AT&T and its Bell companies), ``kind`` ``discontinued`` or ``grandfathered``,
  ``wire_center`` the CLLI code, ``distribution_area`` the area within it (NULL or ``ALL`` for the whole wire
  center), ``effective_on`` the date, ``region`` the state, and ``place`` the development name or point AT&T
  gives. ``source_url``, ``file_date`` and ``file_name`` say where the list came from. A newer import of the same
  carrier and kind supersedes the older rows.
- ``line_notices.kind``: ``letter`` (a carrier's letter, entered by a person; NULL in rows written before this
  revision) or ``contract_end`` (the contract end date from an imported inventory), with ``source_label`` and
  ``source_url``. The newest row per number and kind counts.

The downgrade drops the tables and the columns, and refuses while a contract-end notice is recorded, because
without its kind it would read as a carrier's letter. Runtime code reflects these tables; it never imports this
metadata.
"""
import sqlalchemy as sa

from .schema_after_answer import frozen_metadata as previous_metadata


REVISION = '0077_line_inventory'
ORDER = ('line_inventory', 'carrier_service_areas')
TABLES = frozenset(ORDER)
USES = ('fax', 'alarm', 'elevator', 'emergency', 'other', 'unknown')
KINDS = ('discontinued', 'grandfathered')
NOTICE_KINDS = ('letter', 'contract_end')
NOTICE_COLUMNS = (
    ('kind', 16),
    ('source_label', 200),
    ('source_url', 512),
)
INDEXES = (
    ('ix_line_inventory_number', 'line_inventory', ('number', 'superseded_at'), False),
    ('ix_line_inventory_import', 'line_inventory', ('import_id',), False),
    ('ix_carrier_service_areas_wire_center', 'carrier_service_areas', ('wire_center', 'superseded_at'), False),
    ('ix_carrier_service_areas_import', 'carrier_service_areas', ('import_id',), False),
    ('ix_carrier_service_areas_list', 'carrier_service_areas', ('carrier', 'kind', 'superseded_at'), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _who(prefix):
    return (sa.Column(f'{prefix}_by', sa.String(40), nullable=True),
            sa.Column(f'{prefix}_by_name', sa.String(200), nullable=True))


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'line_inventory': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('import_id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('street', sa.String(200), nullable=True),
            sa.Column('city', sa.String(100), nullable=True),
            sa.Column('region', sa.String(40), nullable=True),
            sa.Column('postal_code', sa.String(20), nullable=True),
            sa.Column('country', sa.String(2), nullable=True),
            sa.Column('carrier', sa.String(100), nullable=True),
            sa.Column('product', sa.String(100), nullable=True),
            sa.Column('wire_center', sa.String(16), nullable=True),
            sa.Column('distribution_area', sa.String(16), nullable=True),
            sa.Column('contract_end', sa.DateTime(), nullable=True),
            sa.Column('line_use', sa.String(16), nullable=False),
            sa.Column('monthly_amount', sa.String(32), nullable=True),
            sa.Column('currency', sa.String(3), nullable=True),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('file_name', sa.String(200), nullable=True),
            sa.Column('superseded_at', sa.DateTime(), nullable=True),
            *_who('imported'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_line_inventory'),
            sa.CheckConstraint(_choice('line_use', USES), name='ck_line_inventory_use'),
        ),
        'carrier_service_areas': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('import_id', sa.String(40), nullable=False),
            sa.Column('carrier', sa.String(40), nullable=False),
            sa.Column('kind', sa.String(16), nullable=False),
            sa.Column('region', sa.String(40), nullable=True),
            sa.Column('city', sa.String(100), nullable=True),
            sa.Column('place', sa.String(200), nullable=True),
            sa.Column('wire_center', sa.String(16), nullable=False),
            sa.Column('wire_center_name', sa.String(100), nullable=True),
            sa.Column('distribution_area', sa.String(16), nullable=True),
            sa.Column('effective_on', sa.DateTime(), nullable=True),
            sa.Column('ref_table', sa.String(8), nullable=True),
            sa.Column('source_url', sa.String(512), nullable=True),
            sa.Column('file_date', sa.DateTime(), nullable=True),
            sa.Column('file_name', sa.String(200), nullable=True),
            sa.Column('superseded_at', sa.DateTime(), nullable=True),
            *_who('imported'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_carrier_service_areas'),
            sa.CheckConstraint(_choice('kind', KINDS), name='ck_carrier_service_areas_kind'),
        ),
    }


def _notice_column(name, length):
    return sa.Column(name, sa.String(length), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for name, length in NOTICE_COLUMNS:
        metadata.tables['line_notices'].append_column(_notice_column(name, length))
    definitions = _definitions()
    for name in ORDER:
        sa.Table(name, metadata, *definitions[name])
    for index, name, columns, unique in INDEXES:
        table = metadata.tables[name]
        sa.Index(index, *(table.c[column] for column in columns), unique=unique)
    return metadata


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_line_inventory(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A line inventory or carrier list table already exists before its migration.')
    if not inspector.has_table('line_notices') or {name for name, _ in NOTICE_COLUMNS} & {
            column['name'] for column in inspector.get_columns('line_notices')}:
        _refuse('The line notice table is not in the expected state for its migration.')
    for name, length in NOTICE_COLUMNS:
        operations.add_column('line_notices', _notice_column(name, length))
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_line_inventory(connection, operations):
    # A contract-end notice would read as a carrier's letter once its kind is gone; never silently.
    notices = sa.table('line_notices', sa.column('kind'))
    if connection.execute(sa.select(sa.func.count()).select_from(notices).where(
            notices.c.kind == 'contract_end')).scalar():
        _refuse('Contract end dates from your line inventory are recorded; this revision cannot be undone without '
                'turning them into carrier notices.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
    # No constraint or index names these columns, so SQLite drops them too.
    for name, _ in reversed(NOTICE_COLUMNS):
        operations.drop_column('line_notices', name)
