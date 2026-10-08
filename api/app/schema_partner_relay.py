"""Frozen 0044 partner relay; registration and validation belong to ``schema``.

An enrolled partner (often the same organization's office in another country)
can send another partner's faxes as local calls in its own country
(``direct/relay.py``). Both sides opt in with signed statements, and both keep
the same three kinds of record:

- ``relay_agreements``: one row per agreement on this installation, with its
  ``role`` here (``relay``: we send faxes for the partner; ``sender``: the
  partner sends ours) and its current ``state`` (``offered``, ``accepting``,
  ``active``, ``withdrawn``). ``terms`` is the canonical JSON the relay signed
  (destinations, monthly page and spending limits, hours, sending together).
  ``reply_number`` is the number the sender asked to have printed on its
  faxes, ``principal_id`` the relay's "Relayed for {partner}" sender, and
  ``marketing`` the sender's marketing-fax details (JSON) or NULL. A state
  change bumps ``version``; what each side actually said is never rewritten,
  because it lives in ``relay_statements``.
- ``relay_statements``: append-only. Every signed statement sent or received
  (an offer, an acceptance, a withdrawal, a price statement, a quote request,
  an outcome receipt), exactly as signed, with its signature and SHA-256.
  No row is ever updated or deleted by Faxbot.
- ``relay_faxes``: one row per relayed fax on each side. On the relay, the
  fax it queued as its own (``job_id``) and the charge its route reported or
  estimated; on the sender, its own fax and attempt, the cost the signed price
  statement gives, and what its own route would have cost. ``state`` moves
  from ``sending``/``accepted`` to one outcome: ``delivered``,
  ``failed_before_data``, ``uncertain`` or ``refused``; an ``uncertain`` row
  can still settle once the relay knows.

The downgrade drops the three tables and refuses while any holds a row,
because agreements and receipts are what two organizations signed. Runtime
code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_friendly_pages import frozen_metadata as previous_metadata


REVISION = '0044_partner_relay'
AGREEMENTS, STATEMENTS, FAXES = 'relay_agreements', 'relay_statements', 'relay_faxes'
TABLES = frozenset({AGREEMENTS, STATEMENTS, FAXES})
ORDER = (AGREEMENTS, STATEMENTS, FAXES)
INDEXES = (
    ('ix_relay_agreements_peer', AGREEMENTS, ('peer_id', 'role', 'state'), False),
    ('ix_relay_statements_agreement', STATEMENTS, ('agreement_id', 'kind', 'created_at'), False),
    ('ix_relay_statements_peer', STATEMENTS, ('peer_id', 'kind', 'created_at'), False),
    ('uq_relay_statements_digest', STATEMENTS, ('direction', 'digest'), True),
    ('uq_relay_faxes_message', FAXES, ('role', 'message_id'), True),
    ('ix_relay_faxes_agreement', FAXES, ('agreement_id', 'created_at'), False),
    ('ix_relay_faxes_job', FAXES, ('job_id',), False),
    ('ix_relay_faxes_state', FAXES, ('role', 'state', 'updated_at'), False),
)


def _id(name='id', *, nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _definitions():
    return {
        AGREEMENTS: (
            sa.Column('id', sa.String(32), nullable=False),
            _id('peer_id'),
            sa.Column('role', sa.String(16), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('terms', sa.Text(), nullable=False),
            sa.Column('send_together', sa.Integer(), nullable=False),
            sa.Column('reply_number', sa.String(32), nullable=True),
            sa.Column('marketing', sa.Text(), nullable=True),
            _id('principal_id', nullable=True),
            _id('price_statement_id', nullable=True),
            sa.Column('withdrawn_by', sa.String(16), nullable=True),
            _id('pending_statement_id', nullable=True),
            sa.Column('told_at', sa.DateTime(), nullable=True),
            sa.Column('actor_name', sa.String(200), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_relay_agreements'),
            sa.CheckConstraint("role = 'relay' OR role = 'sender'", name='ck_relay_agreements_role'),
            sa.CheckConstraint("state = 'offered' OR state = 'accepting' OR state = 'active' OR state = 'withdrawn'",
                               name='ck_relay_agreements_state'),
            sa.CheckConstraint('send_together = 0 OR send_together = 1', name='ck_relay_agreements_together'),
            sa.CheckConstraint("withdrawn_by = 'us' OR withdrawn_by = 'partner'", name='ck_relay_agreements_withdrawn'),
            sa.CheckConstraint('version >= 1', name='ck_relay_agreements_version'),
            sa.ForeignKeyConstraint(['peer_id'], ['direct_peers.id'], name='fk_relay_agreements_peer'),
        ),
        STATEMENTS: (
            _id(),
            sa.Column('agreement_id', sa.String(32), nullable=True),
            _id('peer_id'),
            sa.Column('direction', sa.String(8), nullable=False),
            sa.Column('kind', sa.String(24), nullable=False),
            sa.Column('message_id', sa.String(32), nullable=True),
            sa.Column('statement', sa.Text(), nullable=False),
            sa.Column('signature', sa.String(128), nullable=False),
            sa.Column('digest', sa.String(64), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_relay_statements'),
            sa.CheckConstraint("direction = 'sent' OR direction = 'received'", name='ck_relay_statements_direction'),
            sa.CheckConstraint("kind = 'offer' OR kind = 'acceptance' OR kind = 'withdrawal' OR kind = 'price' "
                               "OR kind = 'quote_request' OR kind = 'outcome'", name='ck_relay_statements_kind'),
            sa.ForeignKeyConstraint(['peer_id'], ['direct_peers.id'], name='fk_relay_statements_peer'),
        ),
        FAXES: (
            _id(),
            sa.Column('role', sa.String(16), nullable=False),
            sa.Column('message_id', sa.String(32), nullable=False),
            sa.Column('agreement_id', sa.String(32), nullable=False),
            _id('peer_id'),
            sa.Column('job_id', sa.String(32), nullable=False),
            sa.Column('attempt_id', sa.String(32), nullable=True),
            sa.Column('destination', sa.String(32), nullable=False),
            sa.Column('pages', sa.Integer(), nullable=True),
            sa.Column('state', sa.String(24), nullable=False),
            sa.Column('shared', sa.Integer(), nullable=False),
            sa.Column('cost_micros', sa.Integer(), nullable=True),
            sa.Column('cost_currency', sa.String(3), nullable=True),
            sa.Column('own_route_micros', sa.Integer(), nullable=True),
            sa.Column('own_route_currency', sa.String(3), nullable=True),
            sa.Column('charge_micros', sa.Integer(), nullable=True),
            sa.Column('charge_currency', sa.String(3), nullable=True),
            sa.Column('charge_basis', sa.String(16), nullable=True),
            sa.Column('delivered_pages', sa.Integer(), nullable=True),
            sa.Column('seconds', sa.Integer(), nullable=True),
            _id('outcome_statement_id', nullable=True),
            sa.Column('outcome_at', sa.DateTime(), nullable=True),
            sa.Column('told_at', sa.DateTime(), nullable=True),
            sa.Column('detail', sa.String(300), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_relay_faxes'),
            sa.CheckConstraint("role = 'relay' OR role = 'sender'", name='ck_relay_faxes_role'),
            sa.CheckConstraint("state = 'sending' OR state = 'accepted' OR state = 'delivered' "
                               "OR state = 'failed_before_data' OR state = 'uncertain' OR state = 'refused'",
                               name='ck_relay_faxes_state'),
            sa.CheckConstraint('shared = 0 OR shared = 1', name='ck_relay_faxes_shared'),
            sa.CheckConstraint("charge_basis = 'reported' OR charge_basis = 'estimated'",
                               name='ck_relay_faxes_charge_basis'),
            sa.ForeignKeyConstraint(['peer_id'], ['direct_peers.id'], name='fk_relay_faxes_peer'),
        ),
    }


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    definitions = _definitions()
    tables = {name: sa.Table(name, metadata, *definitions[name]) for name in ORDER}
    for name, table, columns, unique in INDEXES:
        sa.Index(name, *(tables[table].c[column] for column in columns), unique=unique)
    return metadata


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_partner_relay(connection, operations):
    # The guarded migration owns the transaction; refuse a same-name table before any DDL.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        _refuse('A partner relay table already exists before its migration.')
    if not inspector.has_table('direct_peers'):
        _refuse('The direct delivery partner table is missing before the partner relay migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for name, table, columns, unique in INDEXES:
        operations.create_index(name, table, list(columns), unique=unique)


def downgrade_partner_relay(connection, operations):
    # What two organizations agreed and signed is never dropped silently.
    if any(connection.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar() for name in ORDER):
        _refuse('Partner relay agreements or receipts are recorded; this revision cannot be undone without '
                'losing them.')
    for name, table, _, _ in reversed(INDEXES):
        operations.drop_index(name, table_name=table)
    for name in reversed(ORDER):
        operations.drop_table(name)
