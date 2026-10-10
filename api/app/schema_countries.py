"""Frozen 0069 countries: prices by caller ID, caller-ID eligibility and registered-sender pins.

Registration and validation belong to ``schema``. This revision adds four
append-only tables and changes no stored row:

- ``origin_class_rates``: a carrier's rate deck priced by the caller ID a call
  presents (``routing/origin_classes.py``). One row per destination prefix and
  origination class of one imported deck: ``route`` is the sending card it
  belongs to (``sip-telnyx``, an account key), ``destination_prefix`` the
  E.164 digits it covers, ``origination_type`` one of ``local``, ``eea``,
  ``non_surcharged`` or ``surcharged`` (the four Telnyx defines), and
  ``origin_prefixes`` the caller-ID prefixes that qualify, comma-separated
  digits ('' for the row every other caller ID gets). ``import_id`` groups
  the rows of one import, with its ``deck_format``, ``source_url`` and the
  date the deck was published or read (``captured_on``). A newer import of the
  same route supersedes the older rows (``superseded_at``); rows are never
  changed otherwise.
- ``caller_id_eligibility``: what the administrator confirmed about one
  caller ID on one account, newest counting: ``state`` ``confirmed`` (they
  hold the number and may send faxes from it on that account) or
  ``withdrawn``; ``bought_here`` ``yes`` when the number was bought on that
  account, so the carrier rates calls from it to its own country as local;
  ``evidence`` is what they wrote (an order, an invoice, a portal page).
- ``registered_sender_pins``: a recipient that recognises your faxes by the
  number they come from. ``recipient`` is its fax number, ``account`` the
  sending account, ``caller_id`` and ``station_id`` the identity registered
  with it; the newest row per recipient counts and ``state`` ``removed`` ends
  the pin.
- ``original_requests``: a recipient asking for the original of a pinned fax:
  ``state`` ``requested``, ``sent`` or ``cancelled``, newest counting.

No foreign key ties these rows to faxes. The downgrade drops the tables and
refuses while a confirmation, pin or request is recorded, because those are
what people entered. Runtime code reflects these tables; it never imports
this metadata.
"""
import sqlalchemy as sa

from .schema_analysis import frozen_metadata as previous_metadata


REVISION = '0069_countries'
ORDER = ('origin_class_rates', 'caller_id_eligibility', 'registered_sender_pins', 'original_requests')
TABLES = frozenset(ORDER)
ORIGINATION_TYPES = ('local', 'eea', 'non_surcharged', 'surcharged')
DECK_FORMATS = ('twilio', 'faxbot')
ELIGIBILITY_STATES = ('confirmed', 'withdrawn')
YES_NO = ('yes', 'no')
PIN_STATES = ('active', 'removed')
REQUEST_STATES = ('requested', 'sent', 'cancelled')
INDEXES = (
    ('ix_origin_class_rates_lookup', 'origin_class_rates', ('route', 'destination_prefix', 'superseded_at'), False),
    ('ix_origin_class_rates_import', 'origin_class_rates', ('import_id',), False),
    ('ix_caller_id_eligibility_caller', 'caller_id_eligibility', ('account', 'caller_id', 'created_at'), False),
    ('ix_registered_sender_pins_recipient', 'registered_sender_pins', ('recipient', 'created_at'), False),
    ('ix_original_requests_job', 'original_requests', ('job_id', 'created_at'), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _who(prefix):
    return (sa.Column(f'{prefix}_by', sa.String(40), nullable=True),
            sa.Column(f'{prefix}_by_name', sa.String(200), nullable=True))


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'origin_class_rates': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('import_id', sa.String(40), nullable=False),
            sa.Column('route', sa.String(64), nullable=False),
            sa.Column('destination_prefix', sa.String(16), nullable=False),
            sa.Column('origination_type', sa.String(16), nullable=False),
            sa.Column('origin_prefixes', sa.Text(), nullable=False),
            sa.Column('description', sa.String(200), nullable=True),
            sa.Column('currency', sa.String(3), nullable=False),
            sa.Column('per_minute_micros', sa.Integer(), nullable=False),
            sa.Column('billing_increment_seconds', sa.Integer(), nullable=False),
            sa.Column('minimum_seconds', sa.Integer(), nullable=False),
            sa.Column('deck_format', sa.String(16), nullable=False),
            sa.Column('source_url', sa.String(512), nullable=True),
            sa.Column('captured_on', sa.DateTime(), nullable=False),
            sa.Column('superseded_at', sa.DateTime(), nullable=True),
            *_who('imported'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_origin_class_rates'),
            sa.CheckConstraint(_choice('origination_type', ORIGINATION_TYPES),
                               name='ck_origin_class_rates_type'),
            sa.CheckConstraint(_choice('deck_format', DECK_FORMATS), name='ck_origin_class_rates_format'),
            sa.CheckConstraint('per_minute_micros >= 0', name='ck_origin_class_rates_price'),
        ),
        'caller_id_eligibility': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('account', sa.String(64), nullable=False),
            sa.Column('caller_id', sa.String(32), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('bought_here', sa.String(8), nullable=False),
            sa.Column('evidence', sa.Text(), nullable=True),
            sa.Column('evidence_url', sa.String(512), nullable=True),
            *_who('recorded'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_caller_id_eligibility'),
            sa.CheckConstraint(_choice('state', ELIGIBILITY_STATES), name='ck_caller_id_eligibility_state'),
            sa.CheckConstraint(_choice('bought_here', YES_NO), name='ck_caller_id_eligibility_bought'),
        ),
        'registered_sender_pins': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('recipient', sa.String(32), nullable=False),
            sa.Column('account', sa.String(64), nullable=False),
            sa.Column('caller_id', sa.String(32), nullable=False),
            sa.Column('station_id', sa.String(32), nullable=True),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('note', sa.Text(), nullable=True),
            *_who('recorded'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_registered_sender_pins'),
            sa.CheckConstraint(_choice('state', PIN_STATES), name='ck_registered_sender_pins_state'),
        ),
        'original_requests': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=False),
            sa.Column('recipient', sa.String(32), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('note', sa.Text(), nullable=True),
            *_who('recorded'),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_original_requests'),
            sa.CheckConstraint(_choice('state', REQUEST_STATES), name='ck_original_requests_state'),
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


def upgrade_countries(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A caller-ID price or registered-sender table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_countries(connection, operations):
    # Confirmations, pins and requests are what people entered; never dropped silently. Imported decks can be
    # imported again from their source, so they do not block.
    for table in ('caller_id_eligibility', 'registered_sender_pins', 'original_requests'):
        if connection.execute(sa.select(sa.func.count()).select_from(sa.table(table))).scalar():
            _refuse('Caller-ID confirmations, registered-sender pins or requests for originals are recorded; this '
                    'revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
