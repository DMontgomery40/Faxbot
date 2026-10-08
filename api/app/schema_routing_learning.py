"""Frozen 0059 routing learning; registration and validation belong to ``schema``.

Two additions, both additive; no stored row changes:

- ``page_capability_observations.codings`` (nullable): the codings the
  receiving machine said it takes on a call the SSL Fax engine placed, as
  HylaFAX+ logs its DIS ("REMOTE format support: MH, MR, MMR, JBIG",
  faxd/FaxSend.c++), kept as 'MH,MR,MMR,JBIG'. JBIG is chosen only for a
  machine whose capabilities are on record (``pages.coding``). NULL: not
  reported (older rows, or a built-in engine call, whose frames hold the DIS).
- Collecting faxes by polling (ITU-T T.30 polling, ``routing.polling``), where
  your side places the call and the other fax server sends the document it
  holds for you:

  - ``poll_sources``: your per-number opt-in, append-only (the newest row for
    a number is in force): whether polling that number is allowed, a name for
    it, and the selective polling address (T.30 SEP) when its server needs one.
  - ``poll_requests``: one row each time Faxbot asked its SSL Fax engine to
    collect from a number, written before the call; never rewritten.
  - ``poll_results``: what that call brought back, once per request: received
    (with the fax in Received), nothing waiting, refused, failed, uncertain, or
    not sent. Never rewritten.

Learned hours (``routing.schedule``) are worked out from the delivery records
each time and are not stored. The downgrade refuses while a polling row or a
recorded coding list exists, because they are evidence. Runtime code reflects
these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_measured_codec import frozen_metadata as previous_metadata


REVISION = '0059_routing_learning'
ORDER = ('poll_sources', 'poll_requests', 'poll_results')
TABLES = frozenset(ORDER)
ADDED_COLUMNS = (('page_capability_observations', 'codings'),)
OUTCOMES = ('received', 'nothing_waiting', 'refused', 'failed', 'uncertain', 'not_sent')
INDEXES = (
    ('ix_poll_sources_number', 'poll_sources', ('number', 'created_at'), False),
    ('ix_poll_requests_number', 'poll_requests', ('number', 'requested_at'), False),
    ('uq_poll_results_request', 'poll_results', ('request_id',), True),
)


def _choice(column, values):
    # No grouping parentheses (PostgreSQL reflection round trip).
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _codings_column():
    return sa.Column('codings', sa.String(32), nullable=True)


def _definitions():
    return {
        'poll_sources': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('enabled', sa.Integer(), nullable=False),
            sa.Column('label', sa.String(100), nullable=True),
            sa.Column('selective', sa.String(20), nullable=True),
            sa.Column('recorded_by', sa.String(40), nullable=True),
            sa.Column('recorded_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_poll_sources'),
            sa.CheckConstraint('enabled >= 0 AND enabled <= 1', name='ck_poll_sources_enabled'),
        ),
        'poll_requests': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('selective', sa.String(20), nullable=True),
            sa.Column('engine_job', sa.String(20), nullable=True),
            sa.Column('requested_by', sa.String(40), nullable=True),
            sa.Column('requested_by_name', sa.String(200), nullable=True),
            sa.Column('requested_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_poll_requests'),
        ),
        'poll_results': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('request_id', sa.String(40), nullable=False),
            sa.Column('outcome', sa.String(16), nullable=False),
            sa.Column('pages', sa.Integer(), nullable=True),
            sa.Column('inbound_fax_id', sa.String(40), nullable=True),
            sa.Column('seconds', sa.Integer(), nullable=True),
            sa.Column('sentence', sa.String(300), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_poll_results'),
            sa.CheckConstraint(_choice('outcome', OUTCOMES), name='ck_poll_results_outcome'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, column in ADDED_COLUMNS:
        metadata.tables[table].append_column(_codings_column())
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


def upgrade_routing_learning(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse an unexpected shape before any DDL;
    # never adopt an existing object.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A polling table already exists before its migration.')
    for table, column in ADDED_COLUMNS:
        if not inspector.has_table(table) or column in {item['name'] for item in inspector.get_columns(table)}:
            _refuse('The receiving machine records are not in the expected state for their migration.')
    for table, column in ADDED_COLUMNS:
        operations.add_column(table, _codings_column())
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_routing_learning(connection, operations):
    # Polling requests, their results and the codings machines reported are evidence; never drop them silently.
    for name in ORDER:
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar():
            _refuse('Faxbot has polling records; this revision cannot be undone without losing them.')
    for table, column in ADDED_COLUMNS:
        values = sa.table(table, sa.column(column))
        if connection.execute(sa.select(sa.func.count()).select_from(values)
                              .where(values.c[column].is_not(None))).scalar():
            _refuse('Receiving machines reported the codings they take; this revision cannot be undone without '
                    'losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
    # No constraint or index names the column, so SQLite drops it too.
    for table, column in reversed(ADDED_COLUMNS):
        operations.drop_column(table, column)
