"""Frozen 0080 error-correction records: what error correction and T.38 did on each fax call (brief 98, item 1).

Registration and validation belong to ``schema``. This revision adds one append-only table and changes no stored
row:

- ``fax_call_ecm``: one row per call and source (``engine_ecm.py``). ``source`` says who counted: ``builtin`` (the
  built-in engine, Asterisk patch 0008's FaxFrames event), ``hylafax`` (the SSL Fax engine's session log) or
  ``gateway`` (the T.38 gateway on an SSL Fax engine call, its FaxEngineCall event). Error correction: the frame size
  (``frame_octets``, 64 or 256; ``small_frames`` 1 when Faxbot asked for 64), pages, partial page signals, blocks
  and the frames in them, requests to send frames again (``ppr``), the frames a block's first request asked for
  (``bad_frames``) in how many runs of neighbouring frames (``bad_runs``, ``longest_run``), all frames sent again
  (``resent``), continue-to-correct requests (``ctc``), and the timed turnarounds: block edges (``edges``,
  ``edge_ms``) and rounds of resent frames (``rounds``, ``round_ms``). T.38: packets received, recovered by UDPTL
  redundancy and still missing (``t38_received``, ``t38_recovered``, ``t38_missing``), and the protection by packet
  category the call sent with (``t38_protect``). ``trunk`` is the account the call went over (``sip`` for the first
  trunk). A replayed report is kept once (``direction``, ``call_key``, ``source`` are unique).

The downgrade drops the table, and refuses while a record is kept: the rows are the call history engine learning
reads, and history is never thrown away by a migration.
Runtime code reflects this table; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_pots_quote import frozen_metadata as previous_metadata


REVISION = '0080_engine_package'
ORDER = ('fax_call_ecm',)
TABLES = frozenset(ORDER)
DIRECTIONS = ('outbound', 'inbound')
SOURCES = ('builtin', 'hylafax', 'gateway')
INDEXES = (
    ('uq_fax_call_ecm_call', 'fax_call_ecm', ('direction', 'call_key', 'source'), True),
    ('ix_fax_call_ecm_number', 'fax_call_ecm', ('number', 'created_at'), False),
    ('ix_fax_call_ecm_trunk', 'fax_call_ecm', ('trunk', 'created_at'), False),
)
COUNTS = ('pages', 'pps', 'blocks', 'frames', 'ppr', 'bad_frames', 'bad_runs', 'longest_run', 'resent', 'ctc',
          'edges', 'edge_ms', 'rounds', 'round_ms', 't38_received', 't38_recovered', 't38_missing')


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'fax_call_ecm': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('direction', sa.String(8), nullable=False),
            sa.Column('call_key', sa.String(100), nullable=False),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('number', sa.String(32), nullable=True),
            sa.Column('trunk', sa.String(64), nullable=True),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('attempt_id', sa.String(40), nullable=True),
            sa.Column('frame_octets', sa.Integer(), nullable=True),
            sa.Column('small_frames', sa.Integer(), nullable=True),
            *(sa.Column(name, sa.Integer(), nullable=True) for name in COUNTS),
            sa.Column('t38_protect', sa.String(16), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_call_ecm'),
            sa.CheckConstraint(_choice('direction', DIRECTIONS), name='ck_fax_call_ecm_direction'),
            sa.CheckConstraint(_choice('source', SOURCES), name='ck_fax_call_ecm_source'),
            sa.CheckConstraint('frame_octets IS NULL OR frame_octets = 64 OR frame_octets = 256',
                               name='ck_fax_call_ecm_frame'),
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


def upgrade_engine_package(connection, operations):
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        _refuse('An error-correction record table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_engine_package(connection, operations):
    if connection.execute(sa.select(sa.func.count()).select_from(sa.table('fax_call_ecm'))).scalar():
        _refuse('Error-correction records are kept; this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
