"""Frozen 0078 fax server renewal: an incumbent fax server's call records, its renewal and its number routing
(brief 95, N20 and N24).

Registration and validation belong to ``schema``. This revision adds four append-only tables and changes no stored
row:

- ``channel_call_imports``: one imported file of another fax server's or phone system's call records
  (``routing/channel_peak.py``): which ``system`` it describes (the name you gave it, such as "RightFax at HQ"), the
  file's ``source_format`` (``asterisk``, ``cucm``, ``rightfax``, ``faxmaker`` or ``csv``), its name and SHA-256,
  how many calls it held, the first and last call, and the channels the system is licensed for when you entered
  them. Removing an import sets ``removed_at`` once; its calls stay.
- ``channel_calls``: one row per call of an import: when it started and ended (naive UTC), its direction
  (``sent``, ``received`` or NULL), the channel the file names, and the number it was to or from.
- ``fax_server_renewals``: what you entered about a system's renewal: the product, the date, the amount (a decimal
  string with its ``currency``; never zero by default), the channels it licenses, where the figure came from
  (``source_url``, ``note``), and the numbers Faxbot runs in parallel with it (``parallel_numbers``, JSON, since
  ``parallel_since``). The newest row per system counts; ``state`` ``removed`` withdraws it.
- ``incumbent_routes``: the system's number-to-user routing as imported (number, user, email, cover sheet); a newer
  import for the same system supersedes the older rows (``superseded_at``).

The downgrade drops the tables and refuses while a renewal or routing row is recorded, because those are what people
entered. Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_line_inventory import frozen_metadata as previous_metadata


REVISION = '0078_fax_server_renewal'
ORDER = ('channel_call_imports', 'channel_calls', 'fax_server_renewals', 'incumbent_routes')
TABLES = frozenset(ORDER)
FORMATS = ('asterisk', 'cucm', 'rightfax', 'faxmaker', 'csv')
DIRECTIONS = ('sent', 'received')
STATES = ('active', 'removed')
INDEXES = (
    ('ix_channel_call_imports_system', 'channel_call_imports', ('system', 'created_at'), False),
    ('ix_channel_calls_import', 'channel_calls', ('import_id', 'started_at'), False),
    ('ix_fax_server_renewals_system', 'fax_server_renewals', ('system', 'created_at'), False),
    ('ix_incumbent_routes_system', 'incumbent_routes', ('system', 'superseded_at'), False),
    ('ix_incumbent_routes_import', 'incumbent_routes', ('import_id',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _who(prefix):
    return (sa.Column(f'{prefix}_by', sa.String(40), nullable=True),
            sa.Column(f'{prefix}_by_name', sa.String(200), nullable=True))


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'channel_call_imports': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('system', sa.String(100), nullable=False),
            sa.Column('source_format', sa.String(16), nullable=False),
            sa.Column('file_name', sa.String(200), nullable=True),
            sa.Column('file_sha256', sa.String(64), nullable=False),
            sa.Column('calls', sa.Integer(), nullable=False),
            sa.Column('first_call', sa.DateTime(), nullable=True),
            sa.Column('last_call', sa.DateTime(), nullable=True),
            sa.Column('licensed_channels', sa.Integer(), nullable=True),
            sa.Column('time_zone', sa.String(64), nullable=True),
            sa.Column('removed_at', sa.DateTime(), nullable=True),
            sa.Column('removed_by_name', sa.String(200), nullable=True),
            *_who('imported'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_channel_call_imports'),
            sa.CheckConstraint(_choice('source_format', FORMATS), name='ck_channel_call_imports_format'),
            sa.CheckConstraint('calls >= 0', name='ck_channel_call_imports_calls'),
            sa.CheckConstraint('licensed_channels >= 1',  # NULL passes: not entered
                               name='ck_channel_call_imports_licensed'),
        ),
        'channel_calls': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('import_id', sa.String(40), nullable=False),
            sa.Column('started_at', sa.DateTime(), nullable=False),
            sa.Column('ended_at', sa.DateTime(), nullable=False),
            sa.Column('direction', sa.String(8), nullable=True),
            sa.Column('channel', sa.String(32), nullable=True),
            sa.Column('number', sa.String(32), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_channel_calls'),
            sa.CheckConstraint(_choice('direction', DIRECTIONS), name='ck_channel_calls_direction'),
        ),
        'fax_server_renewals': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('system', sa.String(100), nullable=False),
            sa.Column('product', sa.String(100), nullable=True),
            sa.Column('renews_on', sa.DateTime(), nullable=True),
            sa.Column('amount', sa.String(32), nullable=True),
            sa.Column('currency', sa.String(3), nullable=True),
            sa.Column('licensed_channels', sa.Integer(), nullable=True),
            sa.Column('source_url', sa.String(512), nullable=True),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('parallel_numbers', sa.Text(), nullable=True),
            sa.Column('parallel_since', sa.DateTime(), nullable=True),
            sa.Column('state', sa.String(16), nullable=False),
            *_who('recorded'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_server_renewals'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_fax_server_renewals_state'),
            sa.CheckConstraint('licensed_channels >= 1',  # NULL passes: not entered
                               name='ck_fax_server_renewals_licensed'),
        ),
        'incumbent_routes': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('import_id', sa.String(40), nullable=False),
            sa.Column('system', sa.String(100), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('user_name', sa.String(200), nullable=True),
            sa.Column('user_email', sa.String(320), nullable=True),
            sa.Column('cover_sheet', sa.String(200), nullable=True),
            sa.Column('file_name', sa.String(200), nullable=True),
            sa.Column('superseded_at', sa.DateTime(), nullable=True),
            *_who('imported'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_incumbent_routes'),
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


def upgrade_fax_server_renewal(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        _refuse('A fax server renewal table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_fax_server_renewal(connection, operations):
    # Renewals and routing are what people entered; imported call records can be imported again.
    for table in ('fax_server_renewals', 'incumbent_routes'):
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(table))).scalar():
            _refuse('Fax server renewals or number routing are recorded; this revision cannot be undone without '
                    'losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
