"""Frozen 0083 court profile: where the California court send profile applies, and each fax sent under it (N26).

Registration and validation belong to ``schema``. This revision adds two append-only tables and changes no stored
row:

- ``court_profile_attachments``: the mailboxes and routing rules whose faxes go under the court profile
  (``court/store.py``). ``kind`` is ``mailbox`` or ``rule``, ``target`` the mailbox id or the rule id; the newest row
  per kind and target counts, and ``state`` ``removed`` takes the profile off. Who attached it is kept.
- ``court_sends``: one row per fax sent under the profile (``id`` is the fax's), written in the fax's acceptance transaction and never
  changed: ``purpose`` (``file``, direct filing with a court under rule 2.304, or ``serve``, service on a party
  under rule 2.306), ``source`` (the sender gave the case details, or the mailbox or rule required them), the case
  details as given (``details``, JSON; never a payment card), whether the MC-005 cover was added
  (``cover_pages``), how "By fax" below each title was established (``by_fax``: found in the document's text,
  confirmed by the sender for a scan, or not required for service) and the pages before and after the cover.

The downgrade drops both tables and refuses while any row exists, because they record what people chose and
which faxes were court filings or service. Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_partner_layouts import frozen_metadata as previous_metadata


REVISION = '0083_court_profile'
ORDER = ('court_profile_attachments', 'court_sends')
TABLES = frozenset(ORDER)
KINDS = ('mailbox', 'rule')
STATES = ('active', 'removed')
PURPOSES = ('file', 'serve')
SOURCES = ('sender', 'mailbox', 'rule')
BY_FAX = ('found', 'confirmed', 'not_required')
INDEXES = (
    ('ix_court_profile_attachments_target', 'court_profile_attachments', ('kind', 'target', 'created_at'), False),
    ('ix_court_sends_created', 'court_sends', ('created_at',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'court_profile_attachments': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('kind', sa.String(16), nullable=False),
            sa.Column('target', sa.String(100), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('recorded_by', sa.String(40), nullable=True),
            sa.Column('recorded_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_court_profile_attachments'),
            sa.CheckConstraint(_choice('kind', KINDS), name='ck_court_profile_attachments_kind'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_court_profile_attachments_state'),
        ),
        'court_sends': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('purpose', sa.String(8), nullable=False),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('details', sa.Text(), nullable=False),
            sa.Column('cover_pages', sa.Integer(), nullable=False),
            sa.Column('by_fax', sa.String(16), nullable=False),
            sa.Column('original_pages', sa.Integer(), nullable=False),
            sa.Column('sent_pages', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_court_sends'),
            sa.CheckConstraint(_choice('purpose', PURPOSES), name='ck_court_sends_purpose'),
            sa.CheckConstraint(_choice('source', SOURCES), name='ck_court_sends_source'),
            sa.CheckConstraint(_choice('by_fax', BY_FAX), name='ck_court_sends_by_fax'),
            sa.CheckConstraint('cover_pages >= 0 AND original_pages >= 1 AND sent_pages >= 1',
                               name='ck_court_sends_pages'),
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


def upgrade_court_profile(connection, operations):
    if any(sa.inspect(connection).has_table(name) for name in ORDER):
        _refuse('A court profile table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_court_profile(connection, operations):
    for name in ORDER:
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            _refuse('Court profile choices or court faxes are recorded; this revision cannot be undone without '
                    'losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
