"""Frozen 0076 digits after answer; registration and validation belong to ``schema``.

Some fax machines sit behind a phone menu ("press 2 for the fax"). A
per-recipient dial suffix lets Faxbot press those keys once the call is
answered and before the fax starts (``routing/after_answer.py``). This
revision adds two tables and changes no stored row:

- ``recipient_after_answer``: append-only. One row per change to the keys
  Faxbot presses after a recipient number answers, with who changed it. The
  newest row per number counts; a row whose ``digits`` is NULL clears them.
- ``after_answer_calls``: one row per sent attempt that pressed keys after
  answer (``id`` is the attempt's own ID): the number dialled, the keys sent
  and the engine (``builtin`` or ``sslfax``), so Sent details say what each
  call really pressed even after the recipient's setting changes.

The downgrade drops the tables and refuses while either holds a row.
Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_codec_decoder import frozen_metadata as previous_metadata


REVISION = '0076_after_answer'
ORDER = ('recipient_after_answer', 'after_answer_calls')
TABLES = frozenset(ORDER)
ENGINES = ('builtin', 'sslfax')
INDEXES = (
    ('ix_recipient_after_answer_number', 'recipient_after_answer', ('phone_number', 'created_at'), False),
    ('ix_after_answer_calls_job', 'after_answer_calls', ('job_id',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    return {
        'recipient_after_answer': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('phone_number', sa.String(32), nullable=False),
            # Keys 0-9, * and #, with w (half a second) and W (one second) pauses; NULL clears them.
            sa.Column('digits', sa.String(32), nullable=True),
            sa.Column('actor_principal_id', sa.String(40), nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_recipient_after_answer'),
        ),
        'after_answer_calls': (
            # The attempt's own ID.
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('digits', sa.String(32), nullable=False),
            sa.Column('engine', sa.String(16), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_after_answer_calls'),
            sa.CheckConstraint(_choice('engine', ENGINES), name='ck_after_answer_calls_engine'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_after_answer_calls_job',
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


def upgrade_after_answer(connection, operations):
    from .schema import SchemaUpgradeError
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Digits-after-answer tables already exist before their migration.')
    for name, columns in _definitions().items():
        operations.create_table(name, *columns)
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_after_answer(connection, operations):
    from .schema import SchemaUpgradeError
    for name in ORDER:
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            raise SchemaUpgradeError('Digits after answer are recorded for a recipient or a call; this revision '
                                     'cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
