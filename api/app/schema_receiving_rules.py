"""Frozen 0030 receiving accounts and receiving rules; registration and validation belong to ``schema``.

Several accounts may receive at once, and the number-to-mailbox rules gain
conditions and actions. This revision adds:

- ``inbound_imports.account_key``: the provider account that received the fax
  (``sinch``, ``sinch-uk``, ``sip``, ``sip-leeds`` and so on). NULL for faxes
  received before this revision: they came in on the installation's one
  receiving provider.
- ``sip_call_records.trunk_key``: the trunk account a call went over, so calls
  coming in count against their trunk. NULL for calls recorded before this
  revision, which all went over the one trunk.
- ``inbound_rule_options``: optional extra settings for one ``inbound_rules``
  row, keyed by its ID. A rule without a row here behaves exactly as before.
  ``place`` orders the rules (existing rules keep creation order);
  ``any_number`` matches every receiving number (such a rule stores an empty
  ``to_number``, which the older lookup never matches); ``account_key``,
  ``site_key``, ``from_numbers`` (JSON list of numbers and prefixes), ``days``
  (comma-separated ``mon`` … ``sun``) and ``start_minute``/``end_minute``
  (minutes after local midnight; a window may wrap past midnight) are
  conditions; ``email_connector_id``, ``email_off``, ``urgent`` and
  ``keep_days`` (cleanup age only, never a legal hold) are actions.
- ``inbound_fax_routing``: how one received fax was placed, keyed by its ID:
  the rule, its version and a snapshot of its conditions and actions as they
  matched, the account and site, the mailbox and email connector chosen,
  whether it was marked urgent, how long it is kept, and whether the receipt
  time came from the provider or from the import. Rule and mailbox IDs are kept
  as values without foreign keys, so a later change to a rule never rewrites
  how an earlier fax was placed.

The downgrade drops the tables and columns; it refuses while any of them holds
a value, because that is the record of which account received each fax and how
it was placed. Runtime code reflects these tables; it never imports this
metadata.
"""
import sqlalchemy as sa

from .schema_routing_rules import frozen_metadata as previous_metadata


REVISION = '0030_receiving_accounts'
ORDER = ('inbound_rule_options', 'inbound_fax_routing')
TABLES = frozenset(ORDER)
TIME_SOURCES = ('provider', 'import')
# (table, column, type) added to tables created by earlier revisions; all nullable.
ADDED_COLUMNS = (
    ('inbound_imports', 'account_key', lambda: sa.String(64)),
    ('sip_call_records', 'trunk_key', lambda: sa.String(64)),
)
ADDED_INDEXES = (
    # Which account received what, newest last: health ("waiting for the first fax") and per-account lists.
    ('ix_inbound_imports_account', 'inbound_imports', ('account_key', 'imported_at'), False),
)
INDEXES = (
    ('ix_inbound_rule_options_place', 'inbound_rule_options', ('place', 'id'), False),
    ('ix_inbound_rule_options_connector', 'inbound_rule_options', ('email_connector_id',), False),
    ('ix_inbound_fax_routing_rule', 'inbound_fax_routing', ('rule_id', 'created_at'), False),
)


def _flag(name):
    return sa.Column(name, sa.Integer(), nullable=False)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'inbound_rule_options': (
            # The primary key is the inbound rule's ID: one options row per rule.
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('place', sa.Integer(), nullable=False),
            _flag('enabled'),
            _flag('any_number'),
            sa.Column('account_key', sa.String(64), nullable=True),
            sa.Column('site_key', sa.String(64), nullable=True),
            sa.Column('from_numbers', sa.Text(), nullable=True),
            sa.Column('days', sa.String(32), nullable=True),
            sa.Column('start_minute', sa.Integer(), nullable=True),
            sa.Column('end_minute', sa.Integer(), nullable=True),
            sa.Column('email_connector_id', sa.String(40), nullable=True),
            _flag('email_off'),
            _flag('urgent'),
            sa.Column('keep_days', sa.Integer(), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_inbound_rule_options'),
            sa.CheckConstraint('(enabled = 0 OR enabled = 1) AND (any_number = 0 OR any_number = 1) '
                               'AND (email_off = 0 OR email_off = 1) AND (urgent = 0 OR urgent = 1) '
                               'AND place >= 0 AND version >= 1',
                               name='ck_inbound_rule_options_flags'),
            # NULL passes (a CHECK only rejects false): no window, no cleanup age.
            sa.CheckConstraint('start_minute >= 0 AND start_minute <= 1439 AND end_minute >= 0 '
                               'AND end_minute <= 1439 AND keep_days >= 1',
                               name='ck_inbound_rule_options_ranges'),
            sa.ForeignKeyConstraint(['id'], ['inbound_rules.id'],
                                    name='fk_inbound_rule_options_rule', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['email_connector_id'], ['intake_connectors.id'],
                                    name='fk_inbound_rule_options_connector', ondelete='SET NULL'),
        ),
        'inbound_fax_routing': (
            # The primary key is the received fax's ID: one placement record per fax.
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('rule_id', sa.String(40), nullable=True),
            sa.Column('rule_version', sa.Integer(), nullable=True),
            sa.Column('rule_snapshot', sa.Text(), nullable=True),
            sa.Column('account_key', sa.String(64), nullable=True),
            sa.Column('site_key', sa.String(64), nullable=True),
            sa.Column('mailbox_id', sa.String(40), nullable=True),
            sa.Column('connector_id', sa.String(40), nullable=True),
            sa.Column('urgent', sa.Integer(), nullable=True),
            sa.Column('keep_days', sa.Integer(), nullable=True),
            sa.Column('received_time_source', sa.String(16), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_inbound_fax_routing'),
            sa.CheckConstraint("received_time_source IS NULL OR received_time_source = 'provider' "
                               "OR received_time_source = 'import'", name='ck_inbound_fax_routing_time_source'),
            sa.CheckConstraint('(urgent = 0 OR urgent = 1) AND rule_version >= 1 AND keep_days >= 1',
                               name='ck_inbound_fax_routing_values'),
            sa.ForeignKeyConstraint(['id'], ['inbound_faxes.id'],
                                    name='fk_inbound_fax_routing_inbound_fax', ondelete='CASCADE'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, column, kind in ADDED_COLUMNS:
        metadata.tables[table].append_column(sa.Column(column, kind(), nullable=True))
    for index, name, columns, unique in ADDED_INDEXES:
        table = metadata.tables[name]
        sa.Index(index, *(table.c[column] for column in columns), unique=unique)
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


def upgrade_receiving_accounts(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or an existing column before any DDL; never adopt an object.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('Receiving rule table already exists before its migration.')
    for table, column, _ in ADDED_COLUMNS:
        if not inspector.has_table(table) or column in {c['name'] for c in inspector.get_columns(table)}:
            _refuse('Received-fax or call record tables are not in the expected state for their migration.')
    for table, column, kind in ADDED_COLUMNS:
        operations.add_column(table, sa.Column(column, kind(), nullable=True))
    for index, table, columns, unique in ADDED_INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_receiving_accounts(connection, operations):
    # Which account received each fax, and how each was placed, is evidence; never drop it silently.
    stored = [sa.select(sa.func.count()).select_from(sa.table(name)) for name in ORDER]
    for table, column, _ in ADDED_COLUMNS:
        values = sa.table(table, sa.column(column))
        stored.append(sa.select(sa.func.count()).select_from(values).where(values.c[column].is_not(None)))
    if any(connection.execute(query).scalar() for query in stored):
        _refuse('Receiving accounts or receiving rules are recorded; this revision cannot be undone without '
                'losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
    # SQLite cannot drop an indexed column, so the index goes first.
    for index, table, _, _ in reversed(ADDED_INDEXES):
        operations.drop_index(index, table_name=table)
    for table, column, _ in reversed(ADDED_COLUMNS):
        operations.drop_column(table, column)
