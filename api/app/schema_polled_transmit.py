"""Frozen 0062 polled transmission; registration and validation belong to ``schema``.

The other half of collecting faxes by polling (``routing.polling``): Faxbot's
SSL Fax engine can now be polled (hylafax/patches/0002-polled-transmit.patch),
so a fax can be held for another site's fax server to collect, and the polling
Faxbot does itself gets a password and a timetable. All additive; no stored
row changes:

- ``poll_sources`` (0059, append-only; the newest row of a direction is in
  force) gets five nullable columns: ``direction`` (NULL or 'collect': Faxbot
  collects from the number; 'hold': the number collects from Faxbot),
  ``password_sealed`` (the T.30 polling password, sealed with the installation
  key: sent when collecting, required of the caller when holding),
  ``collect_times`` ("08:00,16:00"), ``collect_days`` ("mon,tue,wed,thu,fri")
  and ``time_zone`` for timed collection, off (NULL) by default.
- ``poll_held``: one row for each fax held for a number to collect, written
  when the engine has it; never rewritten.
- ``poll_collections``: what happened to a held fax, one row per event: sent
  (collected by the caller), refused (wrong selective address or password),
  failed (the call ended before every page was confirmed; the fax stays held)
  or withdrawn (taken back here). The engine's report reference keeps a
  report posted twice to one row.

The downgrade refuses while a held fax, a collection or a direction, password
or timetable exists, because they are evidence. Runtime code reflects these
tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_routing_learning import frozen_metadata as previous_metadata


REVISION = '0062_polled_transmit'
ORDER = ('poll_held', 'poll_collections')
TABLES = frozenset(ORDER)
ADDED_COLUMNS = (('poll_sources', 'direction'), ('poll_sources', 'password_sealed'),
                 ('poll_sources', 'collect_times'), ('poll_sources', 'collect_days'), ('poll_sources', 'time_zone'))
DIRECTIONS = ('collect', 'hold')
OUTCOMES = ('sent', 'refused', 'failed', 'withdrawn')
INDEXES = (
    ('ix_poll_held_number', 'poll_held', ('number', 'held_at'), False),
    ('ix_poll_collections_held', 'poll_collections', ('held_id', 'created_at'), False),
    ('uq_poll_collections_ref', 'poll_collections', ('engine_ref',), True),
)


def _choice(column, values):
    # No grouping parentheses (PostgreSQL reflection round trip).
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _added_column(name):
    # No constraint names these columns, so SQLite can drop them again.
    return {
        'direction': sa.Column('direction', sa.String(8), nullable=True),
        'password_sealed': sa.Column('password_sealed', sa.Text(), nullable=True),
        'collect_times': sa.Column('collect_times', sa.String(60), nullable=True),
        'collect_days': sa.Column('collect_days', sa.String(40), nullable=True),
        'time_zone': sa.Column('time_zone', sa.String(64), nullable=True),
    }[name]


def _definitions():
    return {
        'poll_held': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('selective', sa.String(20), nullable=True),
            sa.Column('password_sealed', sa.Text(), nullable=True),
            sa.Column('document', sa.String(120), nullable=False),
            sa.Column('pages', sa.Integer(), nullable=False),
            sa.Column('source_name', sa.String(200), nullable=True),
            sa.Column('tsi', sa.String(32), nullable=True),
            sa.Column('held_by', sa.String(40), nullable=True),
            sa.Column('held_by_name', sa.String(200), nullable=True),
            sa.Column('held_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_poll_held'),
            sa.CheckConstraint('pages >= 0', name='ck_poll_held_pages'),
        ),
        'poll_collections': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('held_id', sa.String(40), nullable=False),
            sa.Column('outcome', sa.String(16), nullable=False),
            sa.Column('sentence', sa.String(300), nullable=False),
            sa.Column('pages', sa.Integer(), nullable=True),
            sa.Column('caller', sa.String(32), nullable=True),
            sa.Column('cig', sa.String(32), nullable=True),
            sa.Column('sep', sa.String(20), nullable=True),
            sa.Column('seconds', sa.Integer(), nullable=True),
            sa.Column('engine_ref', sa.String(64), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_poll_collections'),
            sa.CheckConstraint(_choice('outcome', OUTCOMES), name='ck_poll_collections_outcome'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, column in ADDED_COLUMNS:
        metadata.tables[table].append_column(_added_column(column))
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


def upgrade_polled_transmit(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse an unexpected shape before any DDL;
    # never adopt an existing object.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A held fax table already exists before its migration.')
    for table, column in ADDED_COLUMNS:
        if not inspector.has_table(table) or column in {item['name'] for item in inspector.get_columns(table)}:
            _refuse('The polling settings are not in the expected state for their migration.')
    for table, column in ADDED_COLUMNS:
        operations.add_column(table, _added_column(column))
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_polled_transmit(connection, operations):
    # Held faxes, their collections and the directions, passwords and timetables set are evidence; never drop them
    # silently.
    for name in ORDER:
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            _refuse('Faxbot has held fax records; this revision cannot be undone without losing them.')
    for table, column in ADDED_COLUMNS:
        values = sa.table(table, sa.column(column))
        if connection.execute(sa.select(sa.func.count()).select_from(values)
                              .where(values.c[column].is_not(None))).scalar():
            _refuse('Polling settings name a direction, password or timetable; this revision cannot be undone '
                    'without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
    for table, column in reversed(ADDED_COLUMNS):
        operations.drop_column(table, column)
