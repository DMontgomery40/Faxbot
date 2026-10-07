"""Frozen 0040 destination schedule; registration and validation belong to ``schema``.

Faxbot schedules each fax around its recipient (``routing/schedule.py``): the
hours the recipient takes faxes, the hours its line is usually busy (learned
from earlier calls, never stored), and the time the sender needs it sent by.
This revision stores only what a person sets:

- ``fax_jobs.send_by``: the time (UTC) the sender needs the fax sent by. NULL
  means no deadline; faxes accepted before this revision hold NULL.
- ``destination_schedules``: the hours a recipient takes faxes, at its own
  request ("business hours only"), and whether Faxbot may learn its busy hours.
  Append-only: every change is a new row and no row is ever updated or deleted
  by Faxbot; the newest row for a number is its current setting. ``days`` is
  comma-separated ``mon`` … ``sun`` and ``start_minute``/``end_minute`` are
  minutes after local midnight (a window may wrap past midnight), in
  ``time_zone`` (an IANA name; NULL means the installation's own zone). All
  three NULL means any time. ``learn_busy`` is 1 (the default) or 0.
  ``recorded_by`` and ``recorded_by_name`` are the person who saved the row, as
  they were then.

The downgrade drops the table and the column; it refuses while either holds a
value, because those are what people asked for. Runtime code reflects these
tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_forms import frozen_metadata as previous_metadata


REVISION = '0040_destination_schedule'
TABLE = 'destination_schedules'
TABLES = frozenset({TABLE})
ADDED_COLUMNS = (('fax_jobs', 'send_by', sa.DateTime),)
INDEXES = (
    ('ix_destination_schedules_number', ('phone_number', 'created_at'), False),
)


def _definition():
    return (
        sa.Column('id', sa.String(40), nullable=False),
        sa.Column('phone_number', sa.String(32), nullable=False),
        sa.Column('time_zone', sa.String(64), nullable=True),
        sa.Column('days', sa.String(32), nullable=True),
        sa.Column('start_minute', sa.Integer(), nullable=True),
        sa.Column('end_minute', sa.Integer(), nullable=True),
        sa.Column('learn_busy', sa.Integer(), nullable=False),
        sa.Column('recorded_by', sa.String(40), nullable=True),
        sa.Column('recorded_by_name', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_destination_schedules'),
        sa.CheckConstraint('learn_busy = 0 OR learn_busy = 1', name='ck_destination_schedules_learn'),
        # NULL minutes pass (a CHECK only rejects false); no grouping parentheses, which PostgreSQL
        # reflection does not round-trip for this grammar.
        sa.CheckConstraint('start_minute >= 0 AND start_minute <= 1439 AND end_minute >= 1 AND end_minute <= 1440',
                           name='ck_destination_schedules_minutes'),
    )


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, column, kind in ADDED_COLUMNS:
        metadata.tables[table].append_column(sa.Column(column, kind(), nullable=True))
    table = sa.Table(TABLE, metadata, *_definition())
    for name, columns, unique in INDEXES:
        sa.Index(name, *(table.c[column] for column in columns), unique=unique)
    return metadata


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_destination_schedule(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table or an existing column before any DDL.
    inspector = sa.inspect(connection)
    if inspector.has_table(TABLE):
        _refuse('The recipient schedule table already exists before its migration.')
    for table, column, _ in ADDED_COLUMNS:
        if not inspector.has_table(table) or column in {c['name'] for c in inspector.get_columns(table)}:
            _refuse('The fax table is not in the expected state for its migration.')
    for table, column, kind in ADDED_COLUMNS:
        operations.add_column(table, sa.Column(column, kind(), nullable=True))
    operations.create_table(TABLE, *_definition())
    for name, columns, unique in INDEXES:
        operations.create_index(name, TABLE, list(columns), unique=unique)


def downgrade_destination_schedule(connection, operations):
    # What senders and administrators asked for is never dropped silently.
    stored = [sa.select(sa.func.count()).select_from(sa.table(TABLE))]
    for table, column, _ in ADDED_COLUMNS:
        values = sa.table(table, sa.column(column))
        stored.append(sa.select(sa.func.count()).select_from(values).where(values.c[column].is_not(None)))
    if any(connection.execute(query).scalar() for query in stored):
        _refuse('Recipient schedules or send-by times are recorded; this revision cannot be undone without '
                'losing them.')
    for name, _, _ in reversed(INDEXES):
        operations.drop_index(name, table_name=TABLE)
    operations.drop_table(TABLE)
    for table, column, _ in reversed(ADDED_COLUMNS):
        operations.drop_column(table, column)
