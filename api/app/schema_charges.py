"""Frozen 0012 carrier charges; registration and validation belong to ``schema``.

A SIP trunk carrier bills each telephone call some time after it ends. This
revision records those bills against Faxbot's own call records, for sent and
received faxes alike:

- ``carrier_charges`` is append-only: every distinct report the carrier makes
  about one of its call records is a new row. A correction is a new version
  that names the row it replaced; an older report that arrives late is kept as
  history without taking effect (``applied`` 0). Money is integer millionths
  of the currency, signed as the carrier means it (a credit is negative), next
  to the carrier's own text.
- ``carrier_call_checks`` is the reconciler's working state for one call:
  whether a carrier record was found, could not be told apart from another
  (``ambiguous``), or is settled, and when to ask again.

Two nullable columns are added to earlier tables: the SIP Call-ID of a call
(the carrier's matching key) and a rate card's monthly plan fee. Runtime code
reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_work import frozen_metadata as previous_metadata


REVISION = '0012_carrier_charges'
# Creation order respects foreign keys.
ORDER = ('carrier_charges', 'carrier_call_checks')
TABLES = frozenset(ORDER)
MATCH_METHODS = ('call_id', 'time_window')
CHECK_STATES = ('waiting', 'ambiguous', 'matched', 'settled', 'unreported')
# (table, column, type) added to tables created by earlier revisions; all nullable.
ADDED_COLUMNS = (
    ('sip_call_records', 'sip_call_id', sa.String(100)),
    ('provider_rate_cards', 'monthly_fee_micros', sa.Integer()),
)
INDEXES = (
    ('uq_carrier_charges_version', 'carrier_charges', ('call_record_id', 'record_id', 'version'), True),
    ('ix_carrier_charges_record', 'carrier_charges', ('provider_id', 'record_id'), False),
    ('ix_carrier_call_checks_due', 'carrier_call_checks', ('state', 'next_check_at'), False),
    ('ix_sip_call_records_sip_call_id', 'sip_call_records', ('sip_call_id',), False),
)


def _id(name='id', nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'carrier_charges': (
            _id(),
            _id('call_record_id'),
            sa.Column('provider_id', sa.String(64), nullable=False),
            # The carrier's identity for its billing record of the call.
            sa.Column('record_id', sa.String(100), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('amount_micros', sa.Integer(), nullable=False),
            sa.Column('raw_amount', sa.String(32), nullable=False),
            sa.Column('currency', sa.String(3), nullable=False),
            sa.Column('billed_seconds', sa.Integer(), nullable=True),
            sa.Column('call_seconds', sa.Integer(), nullable=True),
            sa.Column('match_method', sa.String(16), nullable=False),
            # When the carrier's report was true; the fetch time when it carries no revision.
            sa.Column('effective_at', sa.DateTime(), nullable=False),
            sa.Column('observed_at', sa.DateTime(), nullable=False),
            _id('supersedes_id', True),
            sa.Column('applied', sa.Integer(), nullable=False),
            sa.Column('is_final', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_carrier_charges'),
            sa.CheckConstraint(_choice('match_method', MATCH_METHODS), name='ck_carrier_charges_method'),
            # NULL seconds pass (a CHECK only rejects false); no grouping parentheses,
            # which PostgreSQL reflection does not round-trip for this grammar.
            sa.CheckConstraint('version >= 1 AND applied >= 0 AND applied <= 1 AND is_final >= 0 AND is_final <= 1 '
                               'AND billed_seconds >= 0 AND call_seconds >= 0', name='ck_carrier_charges_values'),
            sa.ForeignKeyConstraint(['call_record_id'], ['sip_call_records.id'],
                                    name='fk_carrier_charges_call', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['supersedes_id'], ['carrier_charges.id'],
                                    name='fk_carrier_charges_supersedes', ondelete='SET NULL'),
        ),
        'carrier_call_checks': (
            # The primary key is the SIP call record identity: one row per call.
            _id(),
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('checks', sa.Integer(), nullable=False),
            sa.Column('checked_at', sa.DateTime(), nullable=True),
            sa.Column('next_check_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_carrier_call_checks'),
            sa.CheckConstraint(_choice('state', CHECK_STATES), name='ck_carrier_call_checks_state'),
            sa.CheckConstraint('checks >= 0', name='ck_carrier_call_checks_counts'),
            sa.ForeignKeyConstraint(['id'], ['sip_call_records.id'],
                                    name='fk_carrier_call_checks_call', ondelete='CASCADE'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, column, kind in ADDED_COLUMNS:
        metadata.tables[table].append_column(sa.Column(column, kind, nullable=True))
    definitions = _definitions()
    for name in ORDER:
        sa.Table(name, metadata, *definitions[name])
    for index, name, columns, unique in INDEXES:
        table = metadata.tables[name]
        sa.Index(index, *(table.c[column] for column in columns), unique=unique)
    return metadata


def upgrade_charges(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table, view or column before any DDL; never adopt an existing object.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Carrier charge tables already exist before their migration.')
    for table, column, _ in ADDED_COLUMNS:
        if column in {item['name'] for item in inspector.get_columns(table)}:
            raise SchemaUpgradeError('A carrier charge column already exists before its migration.')
    for table, column, kind in ADDED_COLUMNS:
        operations.add_column(table, sa.Column(column, kind, nullable=True))
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)
