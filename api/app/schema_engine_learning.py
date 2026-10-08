"""Frozen 0048 engine learning; registration and validation belong to ``schema``.

Faxbot learns from its own fax calls, one number at a time (``engine_learning.py``):
which of fax over IP (T.38) and audio fax fail to a number, the speed a number's
calls train at, the compression that gets through, and where error correction
helps. This revision adds three tables and changes no stored row:

- ``fax_learning_epochs``: append-only. A new row each time Faxbot first sees
  a different trunk configuration (``config_key``, a digest of the trunk
  settings that decide a call's path: carrier, account, transport, network
  and T.38) or fax engine build
  (``engine_key``: each engine's version and the digest of Faxbot's patches to
  it). Only calls made since the newest row count as evidence, so whatever
  Faxbot learned before a change is forgotten, and changing back later does
  not bring it back. ``trunk`` is the carrier preset and server, for people.
  ``learned_through`` is the last change of the newest call record the
  background work has learned from in that epoch (NULL: none yet), so a
  restart reads no call again; it is the only column that ever changes.
- ``fax_destination_memory``: append-only, one row per call that taught a
  negative fact about one number in one direction: ``t38_failed`` (the far
  fax machine answered over T.38 and the fax did not finish) or
  ``audio_failed`` (the same over audio). ``evidence`` is that call's record,
  ``epoch_id`` the epoch it belongs to, ``expires_at`` when it stops counting.
  A person telling Faxbot to forget a number sets ``forgotten_at``,
  ``forgotten_by`` and ``forgotten_by_name`` once; nothing else changes a row.
- ``fax_call_choices``: append-only, one row per sent call attempt on which
  Faxbot changed something for that number (audio fax, T.38 at once, a lower
  starting speed, a more robust compression, error correction turned on, or
  Internet Aware Fax), with one sentence per change in ``reasons``, at most
  one row per attempt. Written when the call is placed, so a fax's details say
  what that call used.

It also adds two nullable columns to ``fax_call_frames`` (0036):
``csa_full`` and ``tsa_full``, the far end's internet address frames whole, up
to 83 octets as hex (T.30 5.3.6.2.12: at most 77 address octets). ``csa`` and
``tsa`` keep their first 32 octets, as before; calls recorded before this
revision have NULL here.

No foreign key ties these rows to faxes, calls or people; retention may remove
a fax while what its calls taught stays until it expires. The downgrade drops
the three tables and the two columns; back up first. Runtime code reflects these tables; it never
imports this metadata.
"""
import sqlalchemy as sa

from .schema_partner_relay import frozen_metadata as previous_metadata


REVISION = '0048_engine_learning'
ORDER = ('fax_learning_epochs', 'fax_destination_memory', 'fax_call_choices')
TABLES = frozenset(ORDER)
DIRECTIONS = ('outbound', 'inbound')
KINDS = ('t38_failed', 'audio_failed')
ENGINES = ('builtin', 'hylafax')
ADDED_COLUMNS = (('fax_call_frames', 'csa_full'), ('fax_call_frames', 'tsa_full'))
ADDRESS_HEX = 166
INDEXES = (
    ('ix_fax_learning_epochs_started', 'fax_learning_epochs', ('started_at', 'id'), False),
    ('ix_fax_destination_memory_number', 'fax_destination_memory', ('number', 'learned_at'), False),
    ('uq_fax_destination_memory_evidence', 'fax_destination_memory', ('evidence', 'kind'), True),
    ('uq_fax_call_choices_attempt', 'fax_call_choices', ('attempt_id',), True),
    ('ix_fax_call_choices_job', 'fax_call_choices', ('job_id',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _flag(column):
    return sa.CheckConstraint(f'{column} = 0 OR {column} = 1', name=f'ck_fax_call_choices_{column}')


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'fax_learning_epochs': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('config_key', sa.String(64), nullable=False),
            sa.Column('engine_key', sa.String(300), nullable=False),
            sa.Column('trunk', sa.String(300), nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=False),
            sa.Column('learned_through', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_fax_learning_epochs'),
        ),
        'fax_destination_memory': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('direction', sa.String(16), nullable=False),
            sa.Column('kind', sa.String(16), nullable=False),
            sa.Column('engine', sa.String(16), nullable=True),
            sa.Column('evidence', sa.String(40), nullable=False),
            sa.Column('epoch_id', sa.String(40), nullable=False),
            sa.Column('learned_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('forgotten_at', sa.DateTime(), nullable=True),
            sa.Column('forgotten_by', sa.String(40), nullable=True),
            sa.Column('forgotten_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_destination_memory'),
            sa.CheckConstraint(_choice('direction', DIRECTIONS), name='ck_fax_destination_memory_direction'),
            sa.CheckConstraint(_choice('kind', KINDS), name='ck_fax_destination_memory_kind'),
            sa.CheckConstraint('forgotten_by IS NULL OR forgotten_at IS NOT NULL',
                               name='ck_fax_destination_memory_forgotten'),
        ),
        'fax_call_choices': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('attempt_id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('number', sa.String(32), nullable=True),
            sa.Column('engine', sa.String(16), nullable=False),
            sa.Column('audio', sa.Integer(), nullable=False),
            sa.Column('t38_now', sa.Integer(), nullable=False),
            sa.Column('max_rate', sa.Integer(), nullable=True),
            sa.Column('compression', sa.String(8), nullable=True),
            sa.Column('ecm_on', sa.Integer(), nullable=False),
            sa.Column('iaf', sa.String(16), nullable=True),
            sa.Column('reasons', sa.Text(), nullable=False),
            sa.Column('epoch_id', sa.String(40), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_call_choices'),
            sa.CheckConstraint(_choice('engine', ENGINES), name='ck_fax_call_choices_engine'),
            _flag('audio'), _flag('t38_now'), _flag('ecm_on'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, column in ADDED_COLUMNS:
        metadata.tables[table].append_column(sa.Column(column, sa.String(ADDRESS_HEX), nullable=True))
    definitions = _definitions()
    for name in ORDER:
        sa.Table(name, metadata, *definitions[name])
    for index, name, columns, unique in INDEXES:
        table = metadata.tables[name]
        sa.Index(index, *(table.c[column] for column in columns), unique=unique)
    return metadata


def upgrade_engine_learning(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Engine learning table already exists before its migration.')
    for table, column in ADDED_COLUMNS:
        if not inspector.has_table(table) or column in {c['name'] for c in inspector.get_columns(table)}:
            raise SchemaUpgradeError('The fax call frames table is not in the expected state for its migration.')
    for table, column in ADDED_COLUMNS:
        operations.add_column(table, sa.Column(column, sa.String(ADDRESS_HEX), nullable=True))
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_engine_learning(connection, operations):
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
    for table, column in reversed(ADDED_COLUMNS):
        operations.drop_column(table, column)
