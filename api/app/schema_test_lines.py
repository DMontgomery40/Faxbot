"""Frozen 0075 public test lines and the answer cap's calls; registration and validation belong to ``schema``.

This revision adds three append-only tables and changes no stored row:

- ``answer_cap_calls``: one row per sent attempt that Faxbot's built-in engine ended because no fax machine
  answered within the cap (``routing/stations.py``, asterisk patch 0007's ``T0Capped``). ``id`` is the
  attempt's own ID. It keeps the cap and the trunk's billing step and minimum as Faxbot read them when the
  result arrived, so Sent details can say which billed step the call fitted in without reading today's prices.
- ``test_line_sends``: one row per test fax a person sent to a public test line from Diagnostics
  (``test_lines.py``): the line, the fax, the number dialled, the number the call showed for a reply, how many
  minutes a reply may take (none when the line sends nothing back), and who sent it. Faxbot never schedules one.
- ``test_line_replies``: one row per received fax labelled as a test line's reply: ``id`` is the received
  fax's own ID, ``how`` is ``number`` (it came from the line's own number) or ``person`` (a person confirmed
  it). The received fax itself is never changed.

The downgrade drops the tables and refuses while any of them holds a row.
Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_codec_decoder import frozen_metadata as previous_metadata


REVISION = '0075_test_lines'
ORDER = ('answer_cap_calls', 'test_line_sends', 'test_line_replies')
TABLES = frozenset(ORDER)
HOW = ('number', 'person')
INDEXES = (
    ('ix_answer_cap_calls_job', 'answer_cap_calls', ('job_id',), False),
    ('uq_test_line_sends_job', 'test_line_sends', ('job_id',), True),
    ('ix_test_line_sends_created', 'test_line_sends', ('created_at',), False),
    ('ix_test_line_replies_send', 'test_line_replies', ('send_id',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    return {
        'answer_cap_calls': (
            # The attempt's own ID.
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('cap_seconds', sa.Integer(), nullable=False),
            sa.Column('increment_seconds', sa.Integer(), nullable=True),
            sa.Column('minimum_seconds', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_answer_cap_calls'),
            sa.CheckConstraint('cap_seconds >= 1', name='ck_answer_cap_calls_cap'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_answer_cap_calls_job', ondelete='CASCADE'),
        ),
        'test_line_sends': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('line_id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('reply_number', sa.String(32), nullable=True),
            # Minutes a reply may take; NULL when the line sends nothing back.
            sa.Column('reply_minutes', sa.Integer(), nullable=True),
            sa.Column('actor_principal_id', sa.String(40), nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_test_line_sends'),
            sa.CheckConstraint('reply_minutes IS NULL OR reply_minutes >= 1', name='ck_test_line_sends_minutes'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_test_line_sends_job', ondelete='CASCADE'),
        ),
        'test_line_replies': (
            # The received fax's own ID.
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('send_id', sa.String(40), nullable=False),
            sa.Column('how', sa.String(16), nullable=False),
            sa.Column('actor_principal_id', sa.String(40), nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_test_line_replies'),
            sa.CheckConstraint(_choice('how', HOW), name='ck_test_line_replies_how'),
            sa.ForeignKeyConstraint(['send_id'], ['test_line_sends.id'], name='fk_test_line_replies_send',
                                    ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['id'], ['inbound_faxes.id'], name='fk_test_line_replies_inbound',
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


def upgrade_test_lines(connection, operations):
    from .schema import SchemaUpgradeError
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Test line tables already exist before their migration.')
    for name, columns in _definitions().items():
        operations.create_table(name, *columns)
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_test_lines(connection, operations):
    from .schema import SchemaUpgradeError
    for name in ORDER:
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            raise SchemaUpgradeError('Test faxes, their replies or capped calls are recorded; this revision cannot '
                                     'be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
