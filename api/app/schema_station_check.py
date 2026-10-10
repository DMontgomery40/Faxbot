"""Frozen 0074 the station check; registration and validation belong to ``schema``.

Before any page, the built-in engine compares the station a called number
answers as (its CSI) with the stations Faxbot expects there (asterisk patch
0007, ``routing/stations.py``). This revision adds three tables and changes no
stored row:

- ``recipient_stations``: append-only. One row each time a number answered a
  successful fax as a station (``source`` ``call``), or a person confirmed
  the station a number answers as (``source`` ``person``, with who). Only the
  digits are kept. A row counts until ``expires_at``.
- ``station_check_settings``: append-only. One row per change to what Faxbot
  does when a number answers as another station, for one recipient number
  (``scope`` ``recipient``) or one mailbox's faxes (``scope`` ``mailbox``):
  ``mode`` ``warn`` (the fax goes on and Sent says so, the default) or
  ``refuse`` (the call ends before any page). The newest row per scope counts.
- ``station_check_results``: one row per sent attempt whose station did not
  match (``id`` is the attempt's own ID): the number, the station it answered
  as, ``outcome`` ``differs`` or ``refused``, and the engine (``builtin``, or
  ``sslfax`` for the SSL Fax engine, which can only compare after the call).

The downgrade drops the tables and refuses while any of them holds a row.
Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_closures import frozen_metadata as previous_metadata


REVISION = '0074_station_check'
ORDER = ('recipient_stations', 'station_check_settings', 'station_check_results')
TABLES = frozenset(ORDER)
SOURCES = ('call', 'person')
SCOPES = ('recipient', 'mailbox')
MODES = ('warn', 'refuse')
OUTCOMES = ('differs', 'refused')
ENGINES = ('builtin', 'sslfax')
INDEXES = (
    ('ix_recipient_stations_number', 'recipient_stations', ('phone_number', 'expires_at'), False),
    ('ix_station_check_settings_scope', 'station_check_settings', ('scope', 'scope_key', 'created_at'), False),
    ('ix_station_check_results_job', 'station_check_results', ('job_id',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    return {
        'recipient_stations': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('station', sa.String(20), nullable=False),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('actor_principal_id', sa.String(40), nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('seen_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_recipient_stations'),
            sa.CheckConstraint(_choice('source', SOURCES), name='ck_recipient_stations_source'),
        ),
        'station_check_settings': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('scope', sa.String(16), nullable=False),
            sa.Column('scope_key', sa.String(40), nullable=False),
            sa.Column('mode', sa.String(16), nullable=False),
            sa.Column('actor_principal_id', sa.String(40), nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_station_check_settings'),
            sa.CheckConstraint(_choice('scope', SCOPES), name='ck_station_check_settings_scope'),
            sa.CheckConstraint(_choice('mode', MODES), name='ck_station_check_settings_mode'),
        ),
        'station_check_results': (
            # The attempt's own ID.
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('station', sa.String(20), nullable=False),
            sa.Column('outcome', sa.String(16), nullable=False),
            sa.Column('engine', sa.String(16), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_station_check_results'),
            sa.CheckConstraint(_choice('outcome', OUTCOMES), name='ck_station_check_results_outcome'),
            sa.CheckConstraint(_choice('engine', ENGINES), name='ck_station_check_results_engine'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_station_check_results_job',
                                    ondelete='CASCADE'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for name, columns in _definitions().items():
        sa.Table(name, metadata, *columns)
    for index, name, columns, unique in INDEXES:
        sa.Index(index, *(metadata.tables[name].c[column] for column in columns), unique=unique)
    return metadata


def upgrade_station_check(connection, operations):
    from .schema import SchemaUpgradeError
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Station check tables already exist before their migration.')
    for name, columns in _definitions().items():
        operations.create_table(name, *columns)
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_station_check(connection, operations):
    from .schema import SchemaUpgradeError
    for name in ORDER:
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            raise SchemaUpgradeError('Stations, station check settings or results are recorded; this revision '
                                     'cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
