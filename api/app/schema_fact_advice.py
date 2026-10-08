"""Frozen 0065 fact advice; registration and validation belong to ``schema``.

Whether one of your fax numbers can go, and moving one as a checked plan
(``routing/number_placement.py`` line advice, ``routing/number_moves.py``).
Faxbot never ports, cancels or changes a provider account; these tables keep
what the administrator answered and did, and what Faxbot checked. This
revision adds three append-only tables and changes no stored row:

- ``number_dependencies``: one row per answer about one of your numbers:
  ``question`` is ``broadband`` (the line also carries broadband),
  ``other_lines`` (alarm, lift or other device lines share it), ``emergency``
  (it is kept for emergency use) or ``printed`` (it is printed on forms,
  letterhead or a website you list; ``note`` names them); ``answer`` is
  ``yes``, ``no`` or ``unknown``. The newest answer to a question counts;
  older ones stay as history.
- ``number_moves``: one row per move you start for a number, from the account
  that carries it (``from_account``) to the one that will
  (``to_account``), both account keys as Faxbot names them.
- ``number_move_events``: what happened to each step of a move, newest
  counting: ``step`` names it (``new_account_ready``, ``new_route_tested``,
  ``port_ordered``, ``cutover``, ``delivery``, ``receipt_test``,
  ``facts_expired``, ``move``), ``state`` says what you recorded (``done``,
  ``not_done``, ``started``, ``finished``, ``abandoned``), ``origin`` is the
  route a receipt test is sent from, ``note`` is what you wrote and
  ``evidence`` is what Faxbot recorded (JSON), such as how many learned
  facts about the number it forgot.

No foreign key ties these rows to faxes; a move's events refer to the move.
The downgrade drops the tables and refuses while any answer or move is
recorded, because those are what people entered. Runtime code reflects these
tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_digital_routes import frozen_metadata as previous_metadata


REVISION = '0065_fact_advice'
ORDER = ('number_dependencies', 'number_moves', 'number_move_events')
TABLES = frozenset(ORDER)
QUESTIONS = ('broadband', 'other_lines', 'emergency', 'printed')
ANSWERS = ('yes', 'no', 'unknown')
STEPS = ('new_account_ready', 'new_route_tested', 'port_ordered', 'cutover', 'delivery', 'receipt_test',
         'facts_expired', 'move')
STATES = ('done', 'not_done', 'started', 'finished', 'abandoned')
INDEXES = (
    ('ix_number_dependencies_number', 'number_dependencies', ('number', 'question', 'created_at'), False),
    ('ix_number_moves_number', 'number_moves', ('number', 'created_at'), False),
    ('ix_number_move_events_move', 'number_move_events', ('move_id', 'step', 'created_at'), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _who(prefix):
    return (sa.Column(f'{prefix}_by', sa.String(40), nullable=True),
            sa.Column(f'{prefix}_by_name', sa.String(200), nullable=True))


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'number_dependencies': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('question', sa.String(16), nullable=False),
            sa.Column('answer', sa.String(8), nullable=False),
            sa.Column('note', sa.Text(), nullable=True),
            *_who('recorded'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_number_dependencies'),
            sa.CheckConstraint(_choice('question', QUESTIONS), name='ck_number_dependencies_question'),
            sa.CheckConstraint(_choice('answer', ANSWERS), name='ck_number_dependencies_answer'),
        ),
        'number_moves': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('from_account', sa.String(64), nullable=False),
            sa.Column('to_account', sa.String(64), nullable=False),
            *_who('started'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_number_moves'),
        ),
        'number_move_events': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('move_id', sa.String(40), nullable=False),
            sa.Column('step', sa.String(24), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('origin', sa.String(64), nullable=True),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('evidence', sa.Text(), nullable=True),
            *_who('recorded'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_number_move_events'),
            sa.CheckConstraint(_choice('step', STEPS), name='ck_number_move_events_step'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_number_move_events_state'),
            sa.ForeignKeyConstraint(['move_id'], ['number_moves.id'], name='fk_number_move_events_move'),
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


def upgrade_fact_advice(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A number move table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_fact_advice(connection, operations):
    # Answers and moves are what people entered and did; never dropped silently.
    answers = connection.execute(sa.select(sa.func.count()).select_from(sa.table('number_dependencies'))).scalar()
    moves = connection.execute(sa.select(sa.func.count()).select_from(sa.table('number_moves'))).scalar()
    if answers or moves:
        _refuse('Answers about your numbers, or moves you started, are recorded; this revision cannot be undone '
                'without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
