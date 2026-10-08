"""Frozen 0047 certainty; registration and validation belong to ``schema``.

A sent fax whose outcome is uncertain must never be sent again on a guess
(``outbound_store``). This revision gives each such outcome an owned record a
person settles (D19), with the cheap ways to find out ranked by cost (M19,
``work/certainty.py``). It adds three tables and changes no stored row:

- ``certainty_items``: one row per uncertain attempt (``attempt_id`` is
  unique): its fax (``job_id``), why it is uncertain (``category``, the
  attempt's error category when the row was made), the route it went by, the
  owner and how the owner was chosen (``owner_source``: ``sender``,
  ``mailbox``, ``fallback`` or ``person``), the sending mailbox when the fax
  named one, and the deadline (``due_at``, computed once from ``due_hours``).
  ``reference`` is the short code printed on a receipt query page and read out
  in a phone call. ``state`` is ``open`` until a person settles it, then
  ``settled`` with ``outcome`` (``delivered``, ``not_delivered`` or
  ``unknown``), who decided (``settled_by`` and the name they had then), when,
  and why. ``query_job_id`` is the one-page receipt query a person sent;
  ``resend_job_id`` the new fax a person chose to send instead. Every change
  is a compare-and-set on ``version`` together with its event.
- ``certainty_events``: append-only history of each item (opened, assigned,
  reassigned, probe, drafted, query_sent, settled, escalated). A probe event
  records what an automatic check found; ``dedupe_key`` gives a repeated
  finding one row.
- ``certainty_settings``: one row (``id`` = ``installation``), absent until
  saved: the person who owns an item when neither the sender nor the sending
  mailbox's backup person can, and the hours a person has to settle an item
  (0 means no deadline). No row means no fallback person and 24 hours.

The downgrade drops the tables, and refuses while any item is kept, because
an item is the record of who decided what happened to a fax. Runtime code
reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_notice_repair import frozen_metadata as previous_metadata


REVISION = '0047_certainty'
ITEMS, EVENTS, SETTINGS = 'certainty_items', 'certainty_events', 'certainty_settings'
ORDER = (ITEMS, EVENTS, SETTINGS)
TABLES = frozenset(ORDER)
STATES = ('open', 'settled')
OUTCOMES = ('delivered', 'not_delivered', 'unknown')
OWNER_SOURCES = ('sender', 'mailbox', 'fallback', 'person')
EVENT_KINDS = ('opened', 'assigned', 'reassigned', 'probe', 'drafted', 'query_sent', 'settled', 'escalated')
DEFAULT_SETTLE_HOURS = 24
MAX_SETTLE_HOURS = 720
INDEXES = (
    ('uq_certainty_items_attempt', ITEMS, ('attempt_id',), True),
    ('uq_certainty_items_reference', ITEMS, ('reference',), True),
    ('ix_certainty_items_job', ITEMS, ('job_id',), False),
    ('ix_certainty_items_state_due', ITEMS, ('state', 'due_at'), False),
    ('ix_certainty_items_owner_state', ITEMS, ('owner_principal_id', 'state'), False),
    ('ix_certainty_items_resend', ITEMS, ('resend_job_id',), False),
    ('ix_certainty_items_query', ITEMS, ('query_job_id',), False),
    ('uq_certainty_events_dedupe', EVENTS, ('item_id', 'dedupe_key'), True),
    ('ix_certainty_events_item_time', EVENTS, ('item_id', 'created_at'), False),
)


def _id(name='id', nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        ITEMS: (
            _id(),
            _id('job_id'),
            _id('attempt_id'),
            sa.Column('category', sa.String(64), nullable=False),
            sa.Column('route', sa.String(64), nullable=True),
            sa.Column('reference', sa.String(12), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            _id('owner_principal_id', True),
            sa.Column('owner_source', sa.String(16), nullable=True),
            _id('mailbox_id', True),
            sa.Column('due_at', sa.DateTime(), nullable=True),
            sa.Column('due_hours', sa.Integer(), nullable=True),
            sa.Column('escalated_at', sa.DateTime(), nullable=True),
            sa.Column('outcome', sa.String(16), nullable=True),
            _id('settled_by', True),
            sa.Column('settled_by_name', sa.String(200), nullable=True),
            sa.Column('settled_at', sa.DateTime(), nullable=True),
            sa.Column('settled_reason', sa.String(400), nullable=True),
            _id('query_job_id', True),
            _id('resend_job_id', True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_certainty_items'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_certainty_items_state'),
            sa.CheckConstraint('outcome IS NULL OR ' + _choice('outcome', OUTCOMES), name='ck_certainty_items_outcome'),
            sa.CheckConstraint('owner_source IS NULL OR ' + _choice('owner_source', OWNER_SOURCES),
                               name='ck_certainty_items_owner_source'),
            # NULL hours pass (a CHECK only rejects false).
            sa.CheckConstraint('due_hours >= 1 AND version >= 1', name='ck_certainty_items_counts'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'], name='fk_certainty_items_job', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['attempt_id'], ['outbound_attempts.id'], name='fk_certainty_items_attempt',
                                    ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['owner_principal_id'], ['access_principals.id'],
                                    name='fk_certainty_items_owner', ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['mailbox_id'], ['mailboxes.id'], name='fk_certainty_items_mailbox',
                                    ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['settled_by'], ['access_principals.id'], name='fk_certainty_items_settled_by',
                                    ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['query_job_id'], ['fax_jobs.id'], name='fk_certainty_items_query_job',
                                    ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['resend_job_id'], ['fax_jobs.id'], name='fk_certainty_items_resend_job',
                                    ondelete='SET NULL'),
        ),
        EVENTS: (
            _id(),
            _id('item_id'),
            sa.Column('kind', sa.String(24), nullable=False),
            # History keeps the acting principal's id and, in details, its name at the time.
            _id('actor_principal_id', True),
            sa.Column('details', sa.Text(), nullable=False),
            sa.Column('dedupe_key', sa.String(64), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_certainty_events'),
            sa.CheckConstraint(_choice('kind', EVENT_KINDS), name='ck_certainty_events_kind'),
            sa.ForeignKeyConstraint(['item_id'], ['certainty_items.id'], name='fk_certainty_events_item',
                                    ondelete='CASCADE'),
        ),
        SETTINGS: (
            _id(),
            _id('fallback_principal_id', True),
            sa.Column('settle_hours', sa.Integer(), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_certainty_settings'),
            sa.CheckConstraint(f'settle_hours >= 0 AND settle_hours <= {MAX_SETTLE_HOURS} AND version >= 1',
                               name='ck_certainty_settings_values'),
            sa.ForeignKeyConstraint(['fallback_principal_id'], ['access_principals.id'],
                                    name='fk_certainty_settings_fallback', ondelete='SET NULL'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    definitions = _definitions()
    tables = {name: sa.Table(name, metadata, *definitions[name]) for name in ORDER}
    for index, table, columns, unique in INDEXES:
        sa.Index(index, *(tables[table].c[column] for column in columns), unique=unique)
    return metadata


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_certainty(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A table for uncertain sent faxes already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
        for index, table, columns, unique in INDEXES:
            if table == name:
                operations.create_index(index, name, list(columns), unique=unique)


def downgrade_certainty(connection, operations):
    # Who decided what happened to a fax is never dropped silently.
    if connection.execute(sa.select(sa.func.count()).select_from(sa.table(ITEMS))).scalar():
        _refuse('Uncertain sent faxes and who settled them are recorded; this revision cannot be undone without '
                'losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
