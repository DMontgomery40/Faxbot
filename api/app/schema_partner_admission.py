"""Frozen 0084 partner admission: line windows a partner grants, and what it accepts before a fax is paid for.

Registration and validation belong to ``schema``. Between enrolled partners that both take part (brief 82, research
CE4 and E), a receiving Faxbot grants senders short, signed windows to start a call to its limited lines, and
answers, signed, whether it accepts a document class from that sender before the sender pays to send it. This
revision adds four append-only tables and changes no stored row:

- ``admission_settings``: this installation's receiving side, newest row counts: how many lines it offers partners
  (``lines``; NULL is the trunk's lines less the reserve), the lines it keeps for callers that don't take part
  (``reserve``), how long a window stays open (``window_minutes``), and the policy it tells senders for urgent faxes
  and deadlines (``urgent``, ``deadline``: ``go`` starts the call without a window, ``wait`` keeps the window).
- ``admission_partners``: per enrolled partner, newest row counts: whether you take part with it (``take_part``),
  the document classes you refuse from it (``refused_classes``, JSON) and the reason you give.
- ``admission_grants``: each window, as issued (``direction`` ``issued``) or received (``received``, with the fax it
  is for): the partner, an opaque ``job_ref``, ``outcome`` (``granted``, ``not_taking_part`` or ``no_answer``), the
  earliest and latest start, the duration class, the policy, and the signed statement as it travelled.
- ``preflight_answers``: each preflight, as asked (``asked``, with the fax) or answered (``answered``): the document
  class, purpose and the mailbox it would arrive in, ``accepts`` (1, 0, or NULL for no answer or not taking part),
  the reason, the statement's version and expiry, and the signed statement.

The downgrade drops the tables and refuses while any row exists, because they record what partners agreed and
granted. Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_court_profile import frozen_metadata as previous_metadata


REVISION = '0084_partner_admission'
ORDER = ('admission_settings', 'admission_partners', 'admission_grants', 'preflight_answers')
TABLES = frozenset(ORDER)
POLICIES = ('go', 'wait')
DIRECTIONS_GRANT = ('issued', 'received')
OUTCOMES = ('granted', 'not_taking_part', 'no_answer')
DURATIONS = ('short', 'medium', 'long')
DIRECTIONS_PREFLIGHT = ('asked', 'answered')
INDEXES = (
    ('ix_admission_partners_peer', 'admission_partners', ('peer_id', 'created_at'), False),
    ('ix_admission_grants_job', 'admission_grants', ('job_id', 'created_at'), False),
    ('ix_admission_grants_peer', 'admission_grants', ('peer_id', 'direction', 'created_at'), False),
    ('ix_preflight_answers_job', 'preflight_answers', ('job_id', 'created_at'), False),
    ('ix_preflight_answers_peer', 'preflight_answers', ('peer_id', 'direction', 'created_at'), False),
)


def _choice(column, values, *, nullable=False):
    text = ' OR '.join(f"{column} = '{value}'" for value in values)
    return f'{column} IS NULL OR {text}' if nullable else text


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'admission_settings': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('lines', sa.Integer(), nullable=True),
            sa.Column('reserve', sa.Integer(), nullable=False),
            sa.Column('window_minutes', sa.Integer(), nullable=False),
            sa.Column('urgent', sa.String(8), nullable=False),
            sa.Column('deadline', sa.String(8), nullable=False),
            sa.Column('recorded_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_admission_settings'),
            sa.CheckConstraint('(lines IS NULL OR lines >= 0) AND reserve >= 0 AND window_minutes >= 1',
                               name='ck_admission_settings_counts'),
            sa.CheckConstraint(_choice('urgent', POLICIES), name='ck_admission_settings_urgent'),
            sa.CheckConstraint(_choice('deadline', POLICIES), name='ck_admission_settings_deadline'),
        ),
        'admission_partners': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('take_part', sa.Integer(), nullable=False),
            sa.Column('refused_classes', sa.Text(), nullable=True),
            sa.Column('refusal_reason', sa.String(300), nullable=True),
            sa.Column('recorded_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_admission_partners'),
            sa.CheckConstraint('take_part >= 0 AND take_part <= 1', name='ck_admission_partners_take_part'),
        ),
        'admission_grants': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('direction', sa.String(16), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('job_ref', sa.String(64), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('outcome', sa.String(16), nullable=False),
            sa.Column('earliest', sa.DateTime(), nullable=True),
            sa.Column('latest', sa.DateTime(), nullable=True),
            sa.Column('duration_class', sa.String(8), nullable=False),
            sa.Column('policy', sa.Text(), nullable=True),
            sa.Column('statement', sa.Text(), nullable=True),
            sa.Column('signature', sa.String(128), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_admission_grants'),
            sa.CheckConstraint(_choice('direction', DIRECTIONS_GRANT), name='ck_admission_grants_direction'),
            sa.CheckConstraint(_choice('outcome', OUTCOMES), name='ck_admission_grants_outcome'),
            sa.CheckConstraint(_choice('duration_class', DURATIONS), name='ck_admission_grants_duration'),
        ),
        'preflight_answers': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('direction', sa.String(16), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('document_class', sa.String(64), nullable=False),
            sa.Column('purpose', sa.String(64), nullable=False),
            sa.Column('mailbox', sa.String(200), nullable=True),
            sa.Column('accepts', sa.Integer(), nullable=True),
            sa.Column('reason', sa.String(300), nullable=True),
            sa.Column('version', sa.Integer(), nullable=True),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
            sa.Column('statement', sa.Text(), nullable=True),
            sa.Column('signature', sa.String(128), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_preflight_answers'),
            sa.CheckConstraint(_choice('direction', DIRECTIONS_PREFLIGHT), name='ck_preflight_answers_direction'),
            sa.CheckConstraint('accepts IS NULL OR (accepts >= 0 AND accepts <= 1)',
                               name='ck_preflight_answers_accepts'),
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


def upgrade_partner_admission(connection, operations):
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        _refuse('A partner admission table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_partner_admission(connection, operations):
    for name in ORDER:
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            _refuse('Partner line windows or preflight answers are recorded; this revision cannot be undone without '
                    'losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
