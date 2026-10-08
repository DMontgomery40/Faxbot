"""Frozen 0051 invoices and provider charge reads; registration and validation belong to ``schema``.

Faxbot reads what a provider charged for each fax where the provider reports
it, and compares the provider's monthly invoice with what it can attribute to
faxes where the provider reports nothing per fax (M27, B2). This revision adds
five tables and changes no stored row:

- ``provider_invoices``: append-only, one row per invoice total the
  administrator entered for one provider account (``account_key``, the
  accounts' key: ``humblefax``, ``sinch-uk``) and one billing period. The
  period is kept as entered (``first_day`` and ``last_day``, local dates, both
  inclusive) and as the UTC bounds Faxbot attributed faxes in
  (``period_start`` inclusive, ``period_end`` exclusive). ``total`` is the
  decimal text entered ("13.20"), at most twelve whole digits and six
  decimals, read with Decimal and never as a float, so no invoice can overflow
  an integer column. A correction is a new version for the same account and
  first day naming the row it replaced (``supersedes_id``). An optional file
  (the invoice itself) is kept on disk, named by its SHA-256 (``file_digest``).
- ``provider_received_charges``: append-only, the provider's charge for one
  received fax (Sinch and Phaxio report one on the fax itself), keyed by the
  received fax and the provider's fax id (``charge_id``). Money is integer
  millionths of the currency with the provider's text beside it. A different
  amount reported later is a new version that supersedes the one in effect;
  an older report is kept with ``applied`` 0. A fax with no price yet has no
  row: unknown stays unknown.
- ``provider_received_checks``: the working state of reading one received
  fax's charge (one row per received fax): when it was last asked and when to
  ask again, and whether it is ``waiting``, ``settled`` or ``unreported`` (the
  provider never priced it within a week).
- ``provider_fax_sweeps``: append-only, one row per time Faxbot listed one
  account's faxes at its provider for a window, and how that went
  (``complete``, ``partial``: the listing had more pages than Faxbot reads,
  ``unavailable``, ``unsupported``: the provider publishes no list).
- ``provider_unrecorded_faxes``: append-only, a fax the provider listed that
  Faxbot had no record of when it looked. A later listing that reports a
  different status or price adds a version; a fax Faxbot later records (a
  late notification) is left out when read, never deleted. Nothing here ever
  sends, fetches or changes a fax.
No foreign key ties these rows to faxes or accounts: an account may be removed
while its invoices stay. The downgrade drops the tables and refuses while any
invoice is recorded, because those are what people entered. Runtime code
reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_rules_delivery import frozen_metadata as previous_metadata


REVISION = '0051_invoices'
ORDER = ('provider_invoices', 'provider_received_charges', 'provider_received_checks', 'provider_fax_sweeps',
         'provider_unrecorded_faxes')
TABLES = frozenset(ORDER)
# Rows people entered; the downgrade refuses while any is kept.
DECIDED = ('provider_invoices',)
CHECK_STATES = ('waiting', 'settled', 'unreported')
SWEEP_OUTCOMES = ('complete', 'partial', 'unavailable', 'unsupported')
DIRECTIONS = ('sent', 'received')
INDEXES = (
    ('uq_provider_invoices_version', 'provider_invoices', ('account_key', 'first_day', 'version'), True),
    ('ix_provider_invoices_created', 'provider_invoices', ('created_at',), False),
    ('uq_provider_received_charges_version', 'provider_received_charges', ('inbound_fax_id', 'charge_id', 'version'),
     True),
    ('ix_provider_received_charges_provider', 'provider_received_charges', ('provider_id', 'charge_id'), False),
    ('ix_provider_received_checks_due', 'provider_received_checks', ('state', 'next_check_at'), False),
    ('ix_provider_fax_sweeps_account', 'provider_fax_sweeps', ('account_key', 'started_at'), False),
    ('uq_provider_unrecorded_faxes_version', 'provider_unrecorded_faxes',
     ('account_key', 'provider_fax_id', 'version'), True),
    ('ix_provider_unrecorded_faxes_time', 'provider_unrecorded_faxes', ('provider_time',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'provider_invoices': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('account_key', sa.String(64), nullable=False),
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('first_day', sa.String(10), nullable=False),
            sa.Column('last_day', sa.String(10), nullable=False),
            sa.Column('period_start', sa.DateTime(), nullable=False),
            sa.Column('period_end', sa.DateTime(), nullable=False),
            sa.Column('total', sa.String(32), nullable=False),
            sa.Column('currency', sa.String(3), nullable=False),
            sa.Column('note', sa.String(500), nullable=True),
            sa.Column('file_digest', sa.String(64), nullable=True),
            sa.Column('file_name', sa.String(200), nullable=True),
            sa.Column('file_type', sa.String(64), nullable=True),
            sa.Column('file_size', sa.Integer(), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('supersedes_id', sa.String(40), nullable=True),
            sa.Column('entered_by', sa.String(40), nullable=True),
            sa.Column('entered_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_provider_invoices'),
            sa.CheckConstraint('version >= 1 AND file_size >= 0', name='ck_provider_invoices_values'),
        ),
        'provider_received_charges': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('inbound_fax_id', sa.String(40), nullable=False),
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('account_key', sa.String(64), nullable=True),
            sa.Column('charge_id', sa.String(100), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('amount_micros', sa.Integer(), nullable=False),
            sa.Column('raw_amount', sa.String(32), nullable=False),
            sa.Column('currency', sa.String(3), nullable=False),
            sa.Column('applied', sa.Integer(), nullable=False),
            sa.Column('is_final', sa.Integer(), nullable=False),
            sa.Column('supersedes_id', sa.String(40), nullable=True),
            sa.Column('effective_at', sa.DateTime(), nullable=False),
            sa.Column('observed_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_provider_received_charges'),
            sa.CheckConstraint('version >= 1 AND applied >= 0 AND applied <= 1 AND is_final >= 0 AND is_final <= 1',
                               name='ck_provider_received_charges_values'),
        ),
        'provider_received_checks': (
            # The primary key is the received fax's id: one row per received fax.
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('account_key', sa.String(64), nullable=True),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('checks', sa.Integer(), nullable=False),
            sa.Column('checked_at', sa.DateTime(), nullable=True),
            sa.Column('next_check_at', sa.DateTime(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_provider_received_checks'),
            sa.CheckConstraint(_choice('state', CHECK_STATES), name='ck_provider_received_checks_state'),
            sa.CheckConstraint('checks >= 0', name='ck_provider_received_checks_counts'),
        ),
        'provider_fax_sweeps': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('account_key', sa.String(64), nullable=False),
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('window_start', sa.DateTime(), nullable=False),
            sa.Column('window_end', sa.DateTime(), nullable=False),
            sa.Column('outcome', sa.String(16), nullable=False),
            sa.Column('listed', sa.Integer(), nullable=False),
            sa.Column('unrecorded', sa.Integer(), nullable=False),
            sa.Column('started_at', sa.DateTime(), nullable=False),
            sa.Column('finished_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_provider_fax_sweeps'),
            sa.CheckConstraint(_choice('outcome', SWEEP_OUTCOMES), name='ck_provider_fax_sweeps_outcome'),
            sa.CheckConstraint('listed >= 0 AND unrecorded >= 0', name='ck_provider_fax_sweeps_counts'),
        ),
        'provider_unrecorded_faxes': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('sweep_id', sa.String(40), nullable=False),
            sa.Column('account_key', sa.String(64), nullable=False),
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('provider_fax_id', sa.String(100), nullable=False),
            sa.Column('direction', sa.String(8), nullable=False),
            sa.Column('from_number', sa.String(64), nullable=True),
            sa.Column('to_number', sa.String(64), nullable=True),
            sa.Column('provider_time', sa.DateTime(), nullable=True),
            sa.Column('pages', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(32), nullable=True),
            # NULL: the provider gave no price (unknown, never zero).
            sa.Column('amount_micros', sa.Integer(), nullable=True),
            sa.Column('raw_amount', sa.String(32), nullable=True),
            sa.Column('currency', sa.String(3), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_provider_unrecorded_faxes'),
            sa.CheckConstraint(_choice('direction', DIRECTIONS), name='ck_provider_unrecorded_faxes_direction'),
            sa.CheckConstraint('version >= 1 AND pages >= 0', name='ck_provider_unrecorded_faxes_values'),
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


def upgrade_invoices(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('An invoice or provider charge table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_invoices(connection, operations):
    # Invoices are what people entered; never dropped silently.
    if any(connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar() for name in DECIDED):
        _refuse('Invoices are recorded; this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
