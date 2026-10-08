"""Frozen 0049 send once and reuse; registration and validation belong to ``schema``.

Three ways of sending fewer bytes to an enrolled partner share this revision
(``direct/distribute.py``, ``direct/reuse.py``):

- ``distribution_agreements``: "send once, we distribute". The receiving
  organization grants it to one partner, signed, naming the exact numbers its
  intake files for (``numbers``, a JSON list in international form) and the
  intake's name; the sender's administrator accepts it, signed, binding the
  offer's SHA-256 (``offer_digest``); either side withdraws it. Similar
  numbers or a shared name never grant anything: only these signed numbers do.
  ``role`` is this installation's side: ``receiver`` (our intake files what the
  partner sends) or ``sender`` (the partner's intake files what we send).
  ``state``: ``offered``, ``accepting``, ``active`` or ``withdrawn``.
  ``pending_statement_id`` names a signed statement the partner has not been
  told yet. The row holds the agreement's current terms; what was signed stays
  in ``distribution_statements``.
- ``distribution_statements``: append-only, every signed offer, acceptance
  and withdrawal, sent or received, kept exactly as signed.
- ``distribution_sends``: one document sent once to a partner's intake for
  several of its numbers, on each side. ``message_id`` is the first fax's
  direct delivery, which carried the document and the signed routing
  statement (``routing_statement``, naming every recipient). Rows are never
  updated.
- ``distribution_members``: one row per recipient of such a send. On the
  sender, the fax (``job_id``) that goes to that number with its own receipt;
  on the receiver, where its receiving rules file that number
  (``mailbox_label``), or why they cannot (``held_reason``), as its signed
  receipt said. Rows are never updated.
- ``direct_byte_savings``: append-only, one row per document a partner
  accepted without its full bytes: as a reference to a copy it already held
  (``reference``), or as the changes from an earlier version it held
  (``patch``, with ``base_sha256``). ``full_bytes`` is what the full document
  would have carried and ``sent_bytes`` what went. These are bytes, never
  money.

No stored row is changed. The downgrade drops the tables, and refuses while
any agreement, statement, send or saving is recorded, because those are what
two organizations signed. Runtime code reflects these tables; it never
imports this metadata.
"""
import sqlalchemy as sa

from .schema_certainty import frozen_metadata as previous_metadata


REVISION = '0049_send_once'
AGREEMENTS, STATEMENTS, SENDS, MEMBERS, SAVINGS = ('distribution_agreements', 'distribution_statements',
                                                   'distribution_sends', 'distribution_members',
                                                   'direct_byte_savings')
ORDER = (AGREEMENTS, STATEMENTS, SENDS, MEMBERS, SAVINGS)
TABLES = frozenset(ORDER)
# Rows two organizations signed or measured; the downgrade refuses while any is kept.
SIGNED = ORDER
ROLES = ('sender', 'receiver')
STATES = ('offered', 'accepting', 'active', 'withdrawn')
STATEMENT_KINDS = ('offer', 'acceptance', 'withdrawal')
CARRIAGES = ('reference', 'patch')
INDEXES = (
    ('ix_distribution_agreements_peer', AGREEMENTS, ('peer_id', 'role', 'state'), False),
    ('ix_distribution_statements_agreement', STATEMENTS, ('agreement_id', 'created_at'), False),
    ('uq_distribution_statements_digest', STATEMENTS, ('direction', 'digest'), True),
    ('uq_distribution_sends_message', SENDS, ('role', 'message_id'), True),
    ('ix_distribution_sends_document', SENDS, ('peer_id', 'document_sha256'), False),
    ('uq_distribution_members_recipient', MEMBERS, ('send_id', 'recipient_number'), True),
    ('ix_distribution_members_job', MEMBERS, ('job_id',), False),
    ('uq_direct_byte_savings_message', SAVINGS, ('message_id',), True),
    ('ix_direct_byte_savings_created', SAVINGS, ('created_at',), False),
)


def _choice(column, values):
    return ' OR '.join(f"{column} = '{value}'" for value in values)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        AGREEMENTS: (
            sa.Column('id', sa.String(32), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('role', sa.String(8), nullable=False),
            sa.Column('state', sa.String(12), nullable=False),
            sa.Column('numbers', sa.Text(), nullable=False),
            sa.Column('intake', sa.String(200), nullable=False),
            sa.Column('offer_digest', sa.String(64), nullable=False),
            sa.Column('pending_statement_id', sa.String(40), nullable=True),
            sa.Column('withdrawn_by', sa.String(8), nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_distribution_agreements'),
            sa.CheckConstraint(_choice('role', ROLES), name='ck_distribution_agreements_role'),
            sa.CheckConstraint(_choice('state', STATES), name='ck_distribution_agreements_state'),
            sa.CheckConstraint("withdrawn_by = 'us' OR withdrawn_by = 'partner'",
                               name='ck_distribution_agreements_withdrawn'),
            sa.CheckConstraint('version >= 1', name='ck_distribution_agreements_version'),
            sa.ForeignKeyConstraint(['peer_id'], ['direct_peers.id'], name='fk_distribution_agreements_peer'),
        ),
        STATEMENTS: (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('agreement_id', sa.String(32), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('direction', sa.String(8), nullable=False),
            sa.Column('kind', sa.String(12), nullable=False),
            sa.Column('statement', sa.Text(), nullable=False),
            sa.Column('signature', sa.String(128), nullable=False),
            sa.Column('digest', sa.String(64), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_distribution_statements'),
            sa.CheckConstraint("direction = 'sent' OR direction = 'received'",
                               name='ck_distribution_statements_direction'),
            sa.CheckConstraint(_choice('kind', STATEMENT_KINDS), name='ck_distribution_statements_kind'),
            sa.ForeignKeyConstraint(['agreement_id'], ['distribution_agreements.id'],
                                    name='fk_distribution_statements_agreement'),
            sa.ForeignKeyConstraint(['peer_id'], ['direct_peers.id'], name='fk_distribution_statements_peer'),
        ),
        SENDS: (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('role', sa.String(8), nullable=False),
            sa.Column('message_id', sa.String(32), nullable=False),
            sa.Column('agreement_id', sa.String(32), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('document_sha256', sa.String(64), nullable=False),
            sa.Column('size_bytes', sa.Integer(), nullable=False),
            sa.Column('routing_statement', sa.Text(), nullable=False),
            sa.Column('routing_signature', sa.String(128), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_distribution_sends'),
            sa.CheckConstraint(_choice('role', ROLES), name='ck_distribution_sends_role'),
            sa.CheckConstraint('size_bytes >= 1', name='ck_distribution_sends_size'),
            sa.ForeignKeyConstraint(['agreement_id'], ['distribution_agreements.id'],
                                    name='fk_distribution_sends_agreement'),
            sa.ForeignKeyConstraint(['peer_id'], ['direct_peers.id'], name='fk_distribution_sends_peer'),
        ),
        MEMBERS: (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('send_id', sa.String(40), nullable=False),
            sa.Column('place', sa.Integer(), nullable=False),
            sa.Column('recipient_number', sa.String(32), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('mailbox_label', sa.String(200), nullable=True),
            sa.Column('held_reason', sa.String(300), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_distribution_members'),
            sa.CheckConstraint('place >= 0', name='ck_distribution_members_place'),
            sa.ForeignKeyConstraint(['send_id'], ['distribution_sends.id'], name='fk_distribution_members_send'),
        ),
        SAVINGS: (
            sa.Column('id', sa.String(40), nullable=False),
            sa.Column('message_id', sa.String(32), nullable=False),
            sa.Column('peer_id', sa.String(40), nullable=False),
            sa.Column('job_id', sa.String(40), nullable=True),
            sa.Column('send_id', sa.String(40), nullable=True),
            sa.Column('carriage', sa.String(12), nullable=False),
            sa.Column('document_sha256', sa.String(64), nullable=False),
            sa.Column('base_sha256', sa.String(64), nullable=True),
            sa.Column('full_bytes', sa.Integer(), nullable=False),
            sa.Column('sent_bytes', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_byte_savings'),
            sa.CheckConstraint(_choice('carriage', CARRIAGES), name='ck_direct_byte_savings_carriage'),
            sa.CheckConstraint('full_bytes >= 1 AND sent_bytes >= 0', name='ck_direct_byte_savings_bytes'),
            sa.CheckConstraint("carriage = 'reference' OR base_sha256 IS NOT NULL",
                               name='ck_direct_byte_savings_base'),
            sa.ForeignKeyConstraint(['peer_id'], ['direct_peers.id'], name='fk_direct_byte_savings_peer'),
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


def upgrade_send_once(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A send-once or reuse table already exists before its migration.')
    if not inspector.has_table('direct_peers'):
        _refuse('The direct delivery partner table is missing before the send-once migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)


def downgrade_send_once(connection, operations):
    # What two organizations signed, and what was measured, is never dropped silently.
    if any(connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar() for name in SIGNED):
        _refuse('Send-once agreements, sends or saved bytes with partners are recorded; this revision cannot be '
                'undone without losing them.')
    for index, table, _, _ in reversed(INDEXES):
        operations.drop_index(index, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
