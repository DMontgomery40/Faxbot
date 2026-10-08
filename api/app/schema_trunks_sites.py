"""Frozen 0033 trunks and sites; registration and validation belong to ``schema``.

Several trunks, sites and regions need no table: trunk accounts live in the
configuration revision (``accounts.py``), sites and regions in the
organization's rules document, and the trunk a call went over in
``sip_call_records.trunk_key`` (0030). This revision adds the one table that
origin-rated quotes need (provider-rules design §3.7, B10):

- ``provider_rate_rows``: a rate card's prices by where a call starts and the
  number it calls. ``origin`` is a site key, ``country:<ISO code>`` or ``any``;
  ``destination_prefix`` is E.164 digits without the plus ('' matches every
  number). Prices are integer micros in the card's currency, with the billing
  increment and minimum, where the price was read and when. A row belongs to
  the card it was entered for; ``routing/origin_rates.py`` reads the rows of
  every card of the same route, so a new version of a card keeps its rows. A
  row is never changed: a new price supersedes it (``superseded_at``), so a
  quote shown earlier can still be explained.

The downgrade drops the table; it refuses while it holds a row, because those
are prices you entered. Runtime code reflects this table; it never imports
this metadata.
"""
import sqlalchemy as sa

from .schema_rules_delivery import frozen_metadata as previous_metadata


REVISION = '0033_trunks_sites'
ORDER = ('provider_rate_rows',)
TABLES = frozenset(ORDER)
MICROS = 1_000_000
INDEXES = (
    ('ix_provider_rate_rows_lookup', 'provider_rate_rows', ('card_id', 'origin', 'destination_prefix'), False),
)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'provider_rate_rows': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('card_id', sa.String(40), nullable=False),
            sa.Column('origin', sa.String(40), nullable=False),
            sa.Column('destination_prefix', sa.String(16), nullable=False),
            sa.Column('per_minute_micros', sa.Integer(), nullable=False),
            sa.Column('per_page_micros', sa.Integer(), nullable=False),
            sa.Column('per_call_micros', sa.Integer(), nullable=False),
            sa.Column('billing_increment_seconds', sa.Integer(), nullable=False),
            sa.Column('minimum_seconds', sa.Integer(), nullable=False),
            sa.Column('source_url', sa.String(512), nullable=True),
            sa.Column('captured_on', sa.DateTime(), nullable=False),
            sa.Column('superseded_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_provider_rate_rows'),
            # As on cards (0008): zero or more and at most 100 per unit; increments of 1 s to 1 h.
            sa.CheckConstraint(
                f'per_minute_micros >= 0 AND per_minute_micros <= {100 * MICROS} AND per_page_micros >= 0 '
                f'AND per_page_micros <= {100 * MICROS} AND per_call_micros >= 0 AND per_call_micros <= {100 * MICROS}',
                name='ck_provider_rate_rows_rates'),
            sa.CheckConstraint('billing_increment_seconds >= 1 AND billing_increment_seconds <= 3600 '
                               'AND minimum_seconds >= 0 AND minimum_seconds <= 3600',
                               name='ck_provider_rate_rows_billing'),
            sa.ForeignKeyConstraint(['card_id'], ['provider_rate_cards.id'], name='fk_provider_rate_rows_card',
                                    ondelete='CASCADE'),
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


def upgrade_trunks_sites(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a same-name table before any DDL;
    # never adopt an existing object.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('Rate row table already exists before its migration.')
    if not inspector.has_table('provider_rate_cards'):
        _refuse('Rate card table is not in the expected state for its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_trunks_sites(connection, operations):
    # Prices you entered are not dropped silently.
    if connection.execute(sa.select(sa.func.count()).select_from(sa.table('provider_rate_rows'))).scalar():
        _refuse('Prices by where calls start are saved; this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
