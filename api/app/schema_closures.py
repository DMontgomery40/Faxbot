"""Frozen 0070 closures: copper-closure dates by commune, carrier notice dates per line, country eligibility.

Registration and validation belong to ``schema``. This revision adds three
append-only tables and changes no stored row:

- ``copper_closures``: one row per commune of one imported closure file
  (``routing/closures.py``): Orange's commune-level trajectory file, or the
  economy ministry's copy of it on data.gouv.fr. ``code_insee`` is the
  commune's INSEE code, ``commercial_closure`` and ``technical_closure`` the
  dates copper (and the switched telephone network with it) stops being sold
  and stops working there, ``lot`` Orange's closure lot. ``import_id`` groups
  one import with its ``source`` (``orange`` or ``gouv``), ``source_url`` and
  the file's own date (``file_date``). A newer import from the same source
  supersedes the older rows (``superseded_at``).
- ``line_notices``: a carrier's notice that one of your lines closes, entered
  from its letter (Canada, Israel and Italy publish no usable schedule):
  ``number``, ``carrier``, ``closes_on``, the date the notice arrived
  (``notice_received_on``) and a note; the newest row per number counts and
  ``state`` ``removed`` withdraws it.
- ``service_eligibility``: what you confirmed about a country's rules for one
  sending account (``routing/country_rules.py``, the UAE and Saudi Arabia
  today): ``state`` ``confirmed`` or ``withdrawn``, with your evidence; the
  newest row per account and country counts.

The downgrade drops the tables and refuses while a notice or a confirmation
is recorded, because those are what people entered; imported closure files
can be imported again. Runtime code reflects these tables; it never imports
this metadata.
"""
import sqlalchemy as sa

from .schema_dialing import frozen_metadata as previous_metadata


REVISION = '0070_closures'
ORDER = ('copper_closures', 'line_notices', 'service_eligibility')
TABLES = frozenset(ORDER)
SOURCES = ('orange', 'gouv')
NOTICE_STATES = ('active', 'removed')
ELIGIBILITY_STATES = ('confirmed', 'withdrawn')
INDEXES = (
    ('ix_copper_closures_commune', 'copper_closures', ('code_insee', 'superseded_at'), False),
    ('ix_copper_closures_import', 'copper_closures', ('import_id',), False),
    ('ix_line_notices_number', 'line_notices', ('number', 'created_at'), False),
    ('ix_service_eligibility_account', 'service_eligibility', ('account', 'country', 'created_at'), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _who(prefix):
    return (sa.Column(f'{prefix}_by', sa.String(40), nullable=True),
            sa.Column(f'{prefix}_by_name', sa.String(200), nullable=True))


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'copper_closures': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('import_id', sa.String(40), nullable=False),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('source_url', sa.String(512), nullable=True),
            sa.Column('file_date', sa.DateTime(), nullable=True),
            sa.Column('code_insee', sa.String(8), nullable=False),
            sa.Column('commune', sa.String(200), nullable=True),
            sa.Column('lot', sa.String(40), nullable=True),
            sa.Column('commercial_closure', sa.DateTime(), nullable=True),
            sa.Column('technical_closure', sa.DateTime(), nullable=True),
            sa.Column('superseded_at', sa.DateTime(), nullable=True),
            *_who('imported'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_copper_closures'),
            sa.CheckConstraint(_choice('source', SOURCES), name='ck_copper_closures_source'),
        ),
        'line_notices': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('carrier', sa.String(100), nullable=True),
            sa.Column('closes_on', sa.DateTime(), nullable=True),
            sa.Column('notice_received_on', sa.DateTime(), nullable=True),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('note', sa.Text(), nullable=True),
            *_who('recorded'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_line_notices'),
            sa.CheckConstraint(_choice('state', NOTICE_STATES), name='ck_line_notices_state'),
        ),
        'service_eligibility': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('account', sa.String(64), nullable=False),
            sa.Column('country', sa.String(2), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('evidence', sa.Text(), nullable=True),
            sa.Column('evidence_url', sa.String(512), nullable=True),
            *_who('recorded'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_service_eligibility'),
            sa.CheckConstraint(_choice('state', ELIGIBILITY_STATES), name='ck_service_eligibility_state'),
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


def upgrade_closures(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A closure, notice or eligibility table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_closures(connection, operations):
    # Notices and confirmations are what people entered; never dropped silently.
    for table in ('line_notices', 'service_eligibility'):
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(table))).scalar():
            _refuse('Carrier notices or country confirmations are recorded; this revision cannot be undone without '
                    'losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
