"""Frozen 0036 engine frames; registration and validation belong to ``schema``.

Asterisk patch 0004 reports what the far end's fax machine said on each call
of Faxbot's built-in engine (the FaxFrames event; ``engine_frames.py``). This
revision adds two tables and changes no stored row:

- ``fax_call_frames``: append-only, one row per built-in engine fax session,
  keyed by the call (``out:<attempt ID>`` or ``in:<Asterisk call ID>``): the
  direction, the fax and attempt (sent) or the call (received), the far end's
  number, the T.30 frames as hex exactly as reported (DIS, first and last DCS,
  CSA, TSA, SUB, NSF, each at most 32 octets) and each DCS's speed code, the
  whole-call speeds read from them (first and lowest, bit/s), trainings and
  failures to train, the mode (T38 or audio) and status, when T.38 started
  (milliseconds after the answer) and who asked, whether Faxbot asked for
  T.38 at once, Internet Aware Fax when used, and the trunk the call used
  (preset and server), so what Faxbot learns expires when the trunk changes.
  A row is written once; a repeated event is ignored, never merged.
- ``fax_iaf_endpoints``: the fax servers an administrator approved for
  Internet Aware Fax (``peer``: another Faxbot; ``endpoint``: an IAF fax
  server such as an SR140), with who approved and why. Removal sets
  ``removed_at``, ``removed_by`` and ``removed_by_name`` once.

No foreign key ties these rows to faxes or people; retention may remove a fax
while its call's frames stay. The downgrade drops both tables; back up first.
Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_screening import frozen_metadata as previous_metadata


REVISION = '0036_engine_frames'
ORDER = ('fax_call_frames', 'fax_iaf_endpoints')
TABLES = frozenset(ORDER)
KINDS = ('peer', 'endpoint')
INDEXES = (
    ('ix_fax_call_frames_number', 'fax_call_frames', ('number', 'created_at'), False),
    ('ix_fax_call_frames_attempt', 'fax_call_frames', ('attempt_id',), False),
    ('ix_fax_iaf_endpoints_number', 'fax_iaf_endpoints', ('number',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    hex_frame = 64
    return {
        'fax_call_frames': (
            sa.Column('id', sa.String(80), nullable=False),
            sa.Column('direction', sa.String(8), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('attempt_id', sa.String(40), nullable=True),
            sa.Column('call_key', sa.String(40), nullable=True),
            sa.Column('number', sa.String(32), nullable=True),
            sa.Column('did', sa.String(32), nullable=True),
            sa.Column('mode', sa.String(8), nullable=True),
            sa.Column('status', sa.String(32), nullable=True),
            sa.Column('dis', sa.String(hex_frame), nullable=True),
            sa.Column('dcs_first', sa.String(hex_frame), nullable=True),
            sa.Column('dcs_last', sa.String(hex_frame), nullable=True),
            sa.Column('dcs_sent', sa.Integer(), nullable=True),
            sa.Column('rates', sa.String(64), nullable=True),
            sa.Column('csa', sa.String(hex_frame), nullable=True),
            sa.Column('tsa', sa.String(hex_frame), nullable=True),
            sa.Column('sub', sa.String(hex_frame), nullable=True),
            sa.Column('nsf', sa.String(hex_frame), nullable=True),
            sa.Column('rate_first', sa.Integer(), nullable=True),
            sa.Column('rate_lowest', sa.Integer(), nullable=True),
            sa.Column('trainings', sa.Integer(), nullable=True),
            sa.Column('ftt', sa.Integer(), nullable=True),
            sa.Column('t38_after_ms', sa.Integer(), nullable=True),
            sa.Column('t38_by', sa.String(8), nullable=True),
            sa.Column('t38_now', sa.Integer(), nullable=True),
            sa.Column('iaf', sa.String(16), nullable=True),
            sa.Column('trunk', sa.String(300), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_call_frames'),
            sa.CheckConstraint("direction = 'out' OR direction = 'in'", name='ck_fax_call_frames_direction'),
        ),
        'fax_iaf_endpoints': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('kind', sa.String(16), nullable=False),
            sa.Column('label', sa.String(200), nullable=False),
            sa.Column('added_by', sa.String(40), nullable=True),
            sa.Column('added_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('removed_at', sa.DateTime(), nullable=True),
            sa.Column('removed_by', sa.String(40), nullable=True),
            sa.Column('removed_by_name', sa.String(200), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_fax_iaf_endpoints'),
            sa.CheckConstraint(_choice('kind', KINDS), name='ck_fax_iaf_endpoints_kind'),
            sa.CheckConstraint('removed_by IS NULL OR removed_at IS NOT NULL', name='ck_fax_iaf_endpoints_removed'),
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


def upgrade_engine_frames(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Engine frame table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_engine_frames(connection, operations):
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
