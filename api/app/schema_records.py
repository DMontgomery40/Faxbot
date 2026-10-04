"""Frozen 0013 carrier records; registration and validation belong to ``schema``.

``carrier_records`` keeps the trunk carrier's priced billing records that
match no call Faxbot recorded, such as a received fax whose hand-over failed
before its call was recorded. Without them spending would be under-reported.
Rows are append-only, like ``carrier_charges``: a different amount, or the
received fax a record was matched to, is a new version naming the row it
replaced; an older report is kept without taking effect (``applied`` 0).
Runtime code reflects this table; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_charges import frozen_metadata as previous_metadata


REVISION = '0013_carrier_records'
ORDER = ('carrier_records',)
TABLES = frozenset(ORDER)
DIRECTIONS = ('inbound', 'outbound')
INDEXES = (
    ('uq_carrier_records_version', 'carrier_records', ('provider_id', 'record_id', 'version'), True),
    ('ix_carrier_records_started', 'carrier_records', ('started_at',), False),
    ('ix_carrier_records_inbound_fax', 'carrier_records', ('inbound_fax_id',), False),
)


def _id(name='id', nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _definitions():
    return {
        'carrier_records': (
            _id(),
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('record_id', sa.String(100), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('direction', sa.String(16), nullable=False),
            sa.Column('calling', sa.String(32), nullable=True),
            sa.Column('called', sa.String(32), nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=False),
            sa.Column('answered_at', sa.DateTime(), nullable=True),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.Column('amount_micros', sa.Integer(), nullable=False),
            sa.Column('raw_amount', sa.String(32), nullable=False),
            sa.Column('currency', sa.String(3), nullable=False),
            sa.Column('billed_seconds', sa.Integer(), nullable=True),
            sa.Column('call_seconds', sa.Integer(), nullable=True),
            # The received fax this record was matched to by number and time, if exactly one.
            _id('inbound_fax_id', True),
            sa.Column('effective_at', sa.DateTime(), nullable=False),
            sa.Column('observed_at', sa.DateTime(), nullable=False),
            _id('supersedes_id', True),
            sa.Column('applied', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_carrier_records'),
            sa.CheckConstraint("direction = 'inbound' OR direction = 'outbound'", name='ck_carrier_records_direction'),
            # NULL seconds pass (a CHECK only rejects false); no grouping parentheses.
            sa.CheckConstraint('version >= 1 AND applied >= 0 AND applied <= 1 AND billed_seconds >= 0 '
                               'AND call_seconds >= 0', name='ck_carrier_records_values'),
            sa.ForeignKeyConstraint(['inbound_fax_id'], ['inbound_faxes.id'],
                                    name='fk_carrier_records_inbound_fax', ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['supersedes_id'], ['carrier_records.id'],
                                    name='fk_carrier_records_supersedes', ondelete='SET NULL'),
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


def upgrade_records(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view before any DDL; never adopt an existing object.
    from .schema import SchemaUpgradeError
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        raise SchemaUpgradeError('Carrier record table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)
