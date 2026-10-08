"""Frozen 0052 digital routes; registration and validation belong to ``schema``.

A fax recipient may also receive by Direct Secure Messaging or through a FHIR
endpoint (``digital/``, D20). This revision adds five tables and changes no
stored row:

- ``digital_addresses``: a recipient's Direct address or FHIR endpoint, one
  row per (number, kind, address), never changed. ``kind`` is ``direct`` or
  ``fhir``; ``address`` is the Direct address in lower case or the FHIR base
  address. ``account_key`` names the HISP account or FHIR client to use (NULL:
  the first one of that kind). ``source`` says where it came from: ``entered``
  by the administrator, or ``nppes`` with the ``npi`` and the ``evidence``
  sentence of the registry record it was read from. The id is the route key's
  second half (``direct:<id>``, ``fhir:<id>``), so rules keep their meaning.
- ``digital_address_events``: append-only; the newest row for an address is its
  state. ``suggested`` (found, not usable), ``confirmed`` (the administrator
  confirmed it; only now may a fax use it), ``withdrawn`` and ``dismissed``.
  ``recorded_by`` and ``recorded_by_name`` are the person, as they were then.
- ``digital_messages``: one row per Direct message or FHIR document sent or
  received. ``message_key`` is the SHA-256 of the account, direction and
  message ID (a Direct Message-ID can be long), unique, so a message is
  recorded and filed once. ``state`` moves forward only (compare-and-set);
  what moved it is an event.
- ``digital_message_events``: append-only evidence for each message: the
  hand-over to the HISP or FHIR server, each delivery notice (processed,
  dispatched, failed, a delivery status report), a timeout, a document filed.
  ``dedupe_key`` is unique, so a repeated notice has no second effect.
  ``details`` holds structured facts only (dispositions, status codes, ids),
  never document content.
- ``digital_trust_bundles``: append-only trust anchors per HISP account (PEM
  text and its SHA-256); the newest row is in force. Bundles live here, not in
  the configuration, because they can be large.

No foreign key ties these rows to faxes, accounts or people; history stays
when a fax or account goes. The downgrade drops the tables and refuses while
any address was confirmed or any message was recorded, because those are what
people decided and what happened. Runtime code reflects these tables; it never
imports this metadata.
"""
import sqlalchemy as sa

from .schema_rules_delivery import frozen_metadata as previous_metadata


REVISION = '0052_digital_routes'
ORDER = ('digital_addresses', 'digital_address_events', 'digital_messages', 'digital_message_events',
         'digital_trust_bundles')
TABLES = frozenset(ORDER)
KINDS = ('direct', 'fhir')
SOURCES = ('entered', 'nppes')
ACTIONS = ('suggested', 'confirmed', 'withdrawn', 'dismissed')
DIRECTIONS = ('out', 'in')
STATES = ('sending', 'submitted', 'processed', 'dispatched', 'delivered', 'failed', 'uncertain', 'refused',
          'filed', 'not_filed')
SECURITY = ('faxbot', 'hisp')
INDEXES = (
    ('ix_digital_addresses_identity', 'digital_addresses', ('phone_number', 'kind', 'address'), True),
    ('ix_digital_address_events_address', 'digital_address_events', ('address_id', 'created_at'), False),
    ('ix_digital_messages_key', 'digital_messages', ('message_key',), True),
    ('ix_digital_messages_job', 'digital_messages', ('job_id', 'created_at'), False),
    ('ix_digital_messages_state', 'digital_messages', ('state', 'updated_at'), False),
    ('ix_digital_message_events_dedupe', 'digital_message_events', ('dedupe_key',), True),
    ('ix_digital_message_events_message', 'digital_message_events', ('message_row_id', 'created_at'), False),
    ('ix_digital_trust_bundles_account', 'digital_trust_bundles', ('account_key', 'created_at'), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'digital_addresses': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('kind', sa.String(8), nullable=False),
            sa.Column('address', sa.String(512), nullable=False),
            sa.Column('account_key', sa.String(32), nullable=True),
            sa.Column('organization', sa.String(200), nullable=True),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('npi', sa.String(10), nullable=True),
            sa.Column('evidence', sa.Text(), nullable=True),
            sa.Column('created_by', sa.String(40), nullable=True),
            sa.Column('created_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_digital_addresses'),
            sa.CheckConstraint(_choice('kind', KINDS), name='ck_digital_addresses_kind'),
            sa.CheckConstraint(_choice('source', SOURCES), name='ck_digital_addresses_source'),
        ),
        'digital_address_events': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('address_id', sa.String(40), nullable=False),
            sa.Column('action', sa.String(16), nullable=False),
            sa.Column('note', sa.Text(), nullable=True),
            sa.Column('recorded_by', sa.String(40), nullable=True),
            sa.Column('recorded_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_digital_address_events'),
            sa.CheckConstraint(_choice('action', ACTIONS), name='ck_digital_address_events_action'),
        ),
        'digital_messages': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('direction', sa.String(8), nullable=False),
            sa.Column('kind', sa.String(8), nullable=False),
            sa.Column('account_key', sa.String(32), nullable=False),
            sa.Column('address_id', sa.String(40), nullable=True),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('attempt_id', sa.String(40), nullable=True),
            sa.Column('message_id', sa.String(512), nullable=False),
            sa.Column('message_key', sa.String(64), nullable=False),
            sa.Column('counterpart', sa.String(512), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('security', sa.String(16), nullable=True),
            sa.Column('digest', sa.String(64), nullable=True),
            sa.Column('size', sa.Integer(), nullable=True),
            sa.Column('pages', sa.Integer(), nullable=True),
            sa.Column('certificate_sha256', sa.String(64), nullable=True),
            sa.Column('remote_id', sa.String(512), nullable=True),
            sa.Column('detail', sa.String(300), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.Column('submitted_at', sa.DateTime(), nullable=True),
            sa.Column('settled_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id', name='pk_digital_messages'),
            sa.CheckConstraint(_choice('direction', DIRECTIONS), name='ck_digital_messages_direction'),
            sa.CheckConstraint(_choice('kind', KINDS), name='ck_digital_messages_kind'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_digital_messages_state'),
            sa.CheckConstraint('security IS NULL OR ' + _choice('security', SECURITY),
                               name='ck_digital_messages_security'),
        ),
        'digital_message_events': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('message_row_id', sa.String(40), nullable=False),
            sa.Column('kind', sa.String(24), nullable=False),
            sa.Column('dedupe_key', sa.String(64), nullable=False),
            sa.Column('details', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_digital_message_events'),
        ),
        'digital_trust_bundles': (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('account_key', sa.String(32), nullable=False),
            sa.Column('source_url', sa.String(512), nullable=True),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('sha256', sa.String(64), nullable=False),
            sa.Column('anchors', sa.Integer(), nullable=False),
            sa.Column('created_by', sa.String(40), nullable=True),
            sa.Column('created_by_name', sa.String(200), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_digital_trust_bundles'),
            sa.CheckConstraint('anchors >= 0', name='ck_digital_trust_bundles_anchors'),
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


def upgrade_digital_routes(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A digital route table already exists before its migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_digital_routes(connection, operations):
    # Confirmed addresses and recorded messages are what people decided and what happened; never dropped silently.
    events = sa.table('digital_address_events', sa.column('action'))
    confirmed = connection.execute(sa.select(sa.func.count()).select_from(events)
                                   .where(events.c.action == 'confirmed')).scalar()
    messages = connection.execute(sa.select(sa.func.count()).select_from(sa.table('digital_messages'))).scalar()
    if confirmed or messages:
        _refuse('Confirmed Direct or FHIR addresses, or messages sent or received by them, are recorded; this '
                'revision cannot be undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
