"""Frozen 0056 measured codec; registration and validation belong to ``schema``.

Before each attempt on Faxbot's own engines, Faxbot measures the lossless fax
codings (MH, MR, MMR; JBIG with an encoder) on the attempt's own pages and
asks the engine for the smallest one the call may use (``pages/coding.py``).
This revision adds one table:

- ``fax_coding_choices``: append-only, one row per attempt (``attempt_id``,
  unique). ``requested`` is the coding asked for ('MH', 'MR', 'MMR' or
  'JBIG'); ``measured`` is 1 when that coding was measured on the pages and 0
  for a JBIG request that could not be (no encoder); ``compared`` is the
  coding the sentence compares with, when there is one, and for a JBIG
  request that could not be measured, the coding sent where JBIG is not (the
  smallest measured; the built-in engine has no JBIG). ``pages`` is the
  number of pages measured and ``bits`` the measured bits of all of them for
  each coding, as JSON ({"MH": 698656, ...}). ``receiver_known`` is 1 when
  the receiving machine's own capabilities (its DIS) were on record, so the
  engine sends exactly the coding asked for, and 0 when the engine may fall
  back to one the machine has. ``reason`` is the one sentence the Sent
  detail shows (for a JBIG request that could not be measured, that of the
  coding sent where JBIG is not). What the call actually negotiated is read from the engines'
  own records of the attempt (``fax_call_frames``, ``fax_engine_calls``),
  never copied here.

Rows are never rewritten. The downgrade drops the table. Runtime code reflects
it; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_invoices import frozen_metadata as previous_metadata


REVISION = '0056_measured_codec'
ORDER = ('fax_coding_choices',)
TABLES = frozenset(ORDER)
CODINGS = ('MH', 'MR', 'MMR', 'JBIG')
INDEXES = (
    ('uq_fax_coding_choices_attempt', 'fax_coding_choices', ('attempt_id',), True),
    ('ix_fax_coding_choices_job', 'fax_coding_choices', ('job_id', 'created_at'), False),
)


def _choice(column, values):
    # No grouping parentheses (PostgreSQL reflection round trip).
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    return {
        'fax_coding_choices': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('attempt_id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=True),
            sa.Column('route', sa.String(40), nullable=True),
            sa.Column('requested', sa.String(8), nullable=False),
            sa.Column('measured', sa.Integer(), nullable=False),
            sa.Column('compared', sa.String(8), nullable=True),
            sa.Column('pages', sa.Integer(), nullable=False),
            sa.Column('bits', sa.Text(), nullable=False),
            sa.Column('receiver_known', sa.Integer(), nullable=False),
            sa.Column('reason', sa.String(300), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_fax_coding_choices'),
            sa.CheckConstraint(_choice('requested', CODINGS), name='ck_fax_coding_choices_requested'),
            # NULL passes (no comparison).
            sa.CheckConstraint(_choice('compared', CODINGS), name='ck_fax_coding_choices_compared'),
            sa.CheckConstraint('measured >= 0 AND measured <= 1 AND receiver_known >= 0 AND receiver_known <= 1 '
                               'AND pages >= 1', name='ck_fax_coding_choices_values'),
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


def upgrade_measured_codec(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a same-name table or view before any
    # DDL; never adopt an existing object.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('A measured coding table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_measured_codec(connection, operations):
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
