"""Frozen 0079 POTS-replacement quotes: the per-line price of a box quoted to replace copper lines (brief 95, N23).

Registration and validation belong to ``schema``. This revision adds one append-only table and changes no stored
row:

- ``pots_quotes``: what you entered from a POTS-replacement quote (``routing/pots_quote.py``): its ``name`` (the
  product, such as Ooma AirDial), the price per line a month (``per_line``, a decimal string, with ``currency``),
  the term in months, the lines it covers, the analog ports per device and the one-time price per device when the
  quote has one, and where the figures came from (``source_url``, ``source_date``, ``note``). The newest row per
  name counts; ``state`` ``removed`` withdraws it.

The downgrade drops the table and refuses while a quote is recorded, because quotes are what people entered.
Runtime code reflects this table; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_fax_server_renewal import frozen_metadata as previous_metadata


REVISION = '0079_pots_quote'
ORDER = ('pots_quotes',)
TABLES = frozenset(ORDER)
STATES = ('active', 'removed')
INDEXES = (
    ('ix_pots_quotes_name', 'pots_quotes', ('name', 'created_at'), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'pots_quotes': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('per_line', sa.String(32), nullable=True),
            sa.Column('currency', sa.String(3), nullable=True),
            sa.Column('term_months', sa.Integer(), nullable=True),
            sa.Column('lines_quoted', sa.Integer(), nullable=True),
            sa.Column('ports_per_device', sa.Integer(), nullable=True),
            sa.Column('device_price', sa.String(32), nullable=True),
            sa.Column('source_url', sa.String(512), nullable=True),
            sa.Column('source_date', sa.DateTime(), nullable=True),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('recorded_by', sa.String(40), nullable=True),
            sa.Column('recorded_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_pots_quotes'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_pots_quotes_state'),
            sa.CheckConstraint('term_months >= 1', name='ck_pots_quotes_term'),
            sa.CheckConstraint('lines_quoted >= 1', name='ck_pots_quotes_lines'),
            sa.CheckConstraint('ports_per_device >= 1', name='ck_pots_quotes_ports'),
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


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_pots_quote(connection, operations):
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        _refuse('A POTS-replacement quote table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_pots_quote(connection, operations):
    if connection.execute(sa.select(sa.func.count()).select_from(sa.table('pots_quotes'))).scalar():
        _refuse('POTS-replacement quotes are recorded; this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
