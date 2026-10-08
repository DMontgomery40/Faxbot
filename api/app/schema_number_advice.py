"""Frozen 0055 number advice; registration and validation belong to ``schema``.

Faxbot reads the public NPI registry (NPPES) as evidence, never as authority
(``routing/nppes.py``, M18): whether a number you might give up is still
printed on your own NPI record, and, before the first fax to a number, whether
the registry lists that number for the provider named on the fax. This
revision adds four tables and changes no stored row:

- ``organization_npis``: the organization's own NPIs, one per location if it
  has several. Append-only: removing one sets ``removed_at`` and
  ``removed_by_name`` once; adding it again is a new row. ``label`` is the
  name people gave the location ('' when none).
- ``nppes_reads``: append-only, one row per provider record read from the
  registry: the NPI, the name, the kind (``NPI-1`` a person, ``NPI-2`` an
  organization), why it was read (``own``: one of your NPIs; ``recipient``: a
  check before a first fax; ``lookup``: someone looked it up), when, and the
  record exactly as the registry returned it (``record``, JSON), so later
  readers (Direct and FHIR addresses among its endpoints) need no second read.
- ``nppes_numbers``: one row per number a read lists: ``kind`` is ``fax`` or
  ``phone``, ``address_purpose`` is ``location``, ``mailing`` or
  ``practice`` (a secondary practice location), with the address and its
  state. The newest read of an NPI is what counts; older reads stay as they
  were.

- ``jurisdiction_rates``: a carrier's US prices for calls that stay within one
  state (``intrastate_micros``) and calls between states
  (``interstate_micros``), by the start of the number called (``prefix``,
  E.164 digits without the plus, such as an area code and exchange
  ``1303555``), as the carrier publishes them (AnveoDirect's rate file has a
  column for each). ``route`` is the card identity (``sip-anveo``). Prices
  are integer micros in ``currency`` a minute, with the billing increment and
  minimum. A row is never changed: a newer import supersedes the route's rows
  (``superseded_at``). Faxbot uses them to choose which of your own numbers
  calls show (``routing/reply_number.py``, M25), never to price a fax.

No foreign key ties these rows to faxes or people. The downgrade drops the
tables and refuses while any NPI of yours or any jurisdiction price is
recorded, because those are what people entered. Runtime code reflects these tables; it never imports this
metadata.
"""
import sqlalchemy as sa

from .schema_trunks_sites import frozen_metadata as previous_metadata


REVISION = '0055_number_advice'
ORDER = ('organization_npis', 'nppes_reads', 'nppes_numbers', 'jurisdiction_rates')
MICROS = 1_000_000
TABLES = frozenset(ORDER)
PURPOSES = ('own', 'recipient', 'lookup')
KINDS = ('fax', 'phone')
ADDRESS_PURPOSES = ('location', 'mailing', 'practice')
INDEXES = (
    ('ix_organization_npis_npi', 'organization_npis', ('npi', 'added_at'), False),
    ('ix_nppes_reads_npi', 'nppes_reads', ('npi', 'read_at'), False),
    ('ix_nppes_numbers_number', 'nppes_numbers', ('number', 'kind'), False),
    ('ix_nppes_numbers_read', 'nppes_numbers', ('read_id',), False),
    ('ix_jurisdiction_rates_lookup', 'jurisdiction_rates', ('route', 'prefix', 'superseded_at'), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'organization_npis': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('npi', sa.String(10), nullable=False),
            sa.Column('label', sa.String(200), nullable=False),
            sa.Column('added_at', sa.DateTime(), nullable=False),
            sa.Column('added_by_name', sa.String(200), nullable=True),
            sa.Column('removed_at', sa.DateTime(), nullable=True),
            sa.Column('removed_by_name', sa.String(200), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_organization_npis'),
        ),
        'nppes_reads': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('npi', sa.String(10), nullable=False),
            sa.Column('name', sa.String(200), nullable=True),
            sa.Column('enumeration_type', sa.String(8), nullable=True),
            sa.Column('purpose', sa.String(16), nullable=False),
            sa.Column('read_at', sa.DateTime(), nullable=False),
            sa.Column('record', sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_nppes_reads'),
            sa.CheckConstraint(_choice('purpose', PURPOSES), name='ck_nppes_reads_purpose'),
        ),
        'nppes_numbers': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('read_id', sa.String(40), nullable=False),
            sa.Column('npi', sa.String(10), nullable=False),
            sa.Column('number', sa.String(32), nullable=False),
            sa.Column('kind', sa.String(8), nullable=False),
            sa.Column('address_purpose', sa.String(16), nullable=False),
            sa.Column('address', sa.String(300), nullable=True),
            sa.Column('state', sa.String(2), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_nppes_numbers'),
            sa.CheckConstraint(_choice('kind', KINDS), name='ck_nppes_numbers_kind'),
            sa.CheckConstraint(_choice('address_purpose', ADDRESS_PURPOSES), name='ck_nppes_numbers_purpose'),
            sa.ForeignKeyConstraint(['read_id'], ['nppes_reads.id'], name='fk_nppes_numbers_read',
                                    ondelete='CASCADE'),
        ),
        'jurisdiction_rates': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('route', sa.String(64), nullable=False),
            sa.Column('prefix', sa.String(16), nullable=False),
            sa.Column('currency', sa.String(3), nullable=False),
            sa.Column('interstate_micros', sa.Integer(), nullable=False),
            sa.Column('intrastate_micros', sa.Integer(), nullable=False),
            sa.Column('billing_increment_seconds', sa.Integer(), nullable=False),
            sa.Column('minimum_seconds', sa.Integer(), nullable=False),
            sa.Column('source_url', sa.String(512), nullable=True),
            sa.Column('captured_on', sa.DateTime(), nullable=False),
            sa.Column('superseded_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_jurisdiction_rates'),
            # As on cards (0008): zero or more and at most 100 a minute; increments of 1 s to 1 h.
            sa.CheckConstraint(f'interstate_micros >= 0 AND interstate_micros <= {100 * MICROS} '
                               f'AND intrastate_micros >= 0 AND intrastate_micros <= {100 * MICROS}',
                               name='ck_jurisdiction_rates_rates'),
            sa.CheckConstraint('billing_increment_seconds >= 1 AND billing_increment_seconds <= 3600 '
                               'AND minimum_seconds >= 0 AND minimum_seconds <= 3600',
                               name='ck_jurisdiction_rates_billing'),
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


def upgrade_number_advice(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a same-name table before any DDL;
    # never adopt an existing object.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('Number advice table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_number_advice(connection, operations):
    # NPIs and prices you entered are not dropped silently.
    if connection.execute(sa.select(sa.func.count()).select_from(sa.table('organization_npis'))).scalar():
        _refuse('Your NPI numbers are saved; this revision cannot be undone without losing them.')
    if connection.execute(sa.select(sa.func.count()).select_from(sa.table('jurisdiction_rates'))).scalar():
        _refuse('Prices by state are saved; this revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
