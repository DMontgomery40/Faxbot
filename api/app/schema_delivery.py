"""Frozen 0008 delivery route, intake and direct delivery tables.

Money is stored as integer millionths of the card's currency (``*_micros``) and
flags as 0/1 integers, so every column stays inside the frozen type grammar.
Registration and validation belong to ``schema``; runtime code reflects tables.
"""
import sqlalchemy as sa

from .schema_capabilities import frozen_metadata as capabilities_metadata


REVISION = '0008_delivery_routes'
# Creation order respects foreign keys.
ORDER = ('provider_rate_cards', 'direct_peers', 'delivery_destinations', 'delivery_attempt_costs',
         'intake_connectors', 'direct_deliveries', 'intake_items', 'case_documents')
TABLES = frozenset(ORDER)


def _id(name='id', nullable=False):
    return sa.Column(name, sa.String(40), nullable=nullable)


def _definitions():
    """Fresh column/constraint objects per call; SQLAlchemy binds them to one table."""
    return {
        'provider_rate_cards': (
            _id(),
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('direction', sa.String(16), nullable=False),
            sa.Column('label', sa.String(100), nullable=False),
            sa.Column('currency', sa.String(3), nullable=False),
            sa.Column('per_minute_micros', sa.Integer(), nullable=False),
            sa.Column('per_page_micros', sa.Integer(), nullable=False),
            sa.Column('per_call_micros', sa.Integer(), nullable=False),
            sa.Column('billing_increment_seconds', sa.Integer(), nullable=False),
            sa.Column('minimum_seconds', sa.Integer(), nullable=False),
            sa.Column('source_url', sa.String(512), nullable=True),
            sa.Column('captured_on', sa.DateTime(), nullable=False),
            sa.Column('superseded_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_provider_rate_cards'),
            sa.CheckConstraint("direction = 'outbound' OR direction = 'inbound'",
                               name='ck_provider_rate_cards_direction'),
            sa.CheckConstraint('per_minute_micros >= 0 AND per_page_micros >= 0 AND per_call_micros >= 0 '
                               'AND billing_increment_seconds >= 1 AND minimum_seconds >= 0',
                               name='ck_provider_rate_cards_amounts'),
        ),
        'direct_peers': (
            _id(),
            sa.Column('organization', sa.String(200), nullable=False),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('endpoint_url', sa.String(512), nullable=False),
            sa.Column('signing_key', sa.String(64), nullable=False),
            sa.Column('exchange_key', sa.String(64), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('challenge_hash', sa.String(64), nullable=True),
            sa.Column('challenge_job_id', sa.String(40), nullable=True),
            sa.Column('challenge_expires_at', sa.DateTime(), nullable=True),
            sa.Column('challenge_failures', sa.Integer(), nullable=False),
            sa.Column('verified_at', sa.DateTime(), nullable=True),
            sa.Column('expires_at', sa.DateTime(), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_peers'),
            sa.CheckConstraint("state = 'pending' OR state = 'verified' OR state = 'revoked'",
                               name='ck_direct_peers_state'),
            sa.CheckConstraint('challenge_failures >= 0 AND version >= 1', name='ck_direct_peers_counters'),
            sa.ForeignKeyConstraint(['challenge_job_id'], ['fax_jobs.id'],
                                    name='fk_direct_peers_challenge_job', ondelete='SET NULL'),
        ),
        'delivery_destinations': (
            _id(),
            sa.Column('phone_number', sa.String(32), nullable=False),
            sa.Column('display_name', sa.String(200), nullable=True),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('preferred_route', sa.String(64), nullable=True),
            sa.Column('direct_peer_id', sa.String(40), nullable=True),
            sa.Column('accepts_references', sa.Integer(), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_delivery_destinations'),
            sa.CheckConstraint('(accepts_references = 0 OR accepts_references = 1) AND version >= 1',
                               name='ck_delivery_destinations_flags'),
            sa.ForeignKeyConstraint(['direct_peer_id'], ['direct_peers.id'],
                                    name='fk_delivery_destinations_direct_peer', ondelete='SET NULL'),
        ),
        'delivery_attempt_costs': (
            # The primary key is the outbound attempt identity: one row per attempt.
            _id(),
            _id('job_id'),
            sa.Column('destination', sa.String(32), nullable=False),
            sa.Column('route', sa.String(64), nullable=False),
            sa.Column('route_reason', sa.String(32), nullable=False),
            sa.Column('provider_id', sa.String(64), nullable=False),
            sa.Column('provider_sid', sa.String(100), nullable=True),
            _id('rate_card_id', nullable=True),
            sa.Column('started_at', sa.DateTime(), nullable=True),
            sa.Column('connected_at', sa.DateTime(), nullable=True),
            sa.Column('ended_at', sa.DateTime(), nullable=True),
            sa.Column('billed_seconds', sa.Integer(), nullable=True),
            sa.Column('billed_pages', sa.Integer(), nullable=True),
            sa.Column('computed_cost_micros', sa.Integer(), nullable=True),
            sa.Column('reported_cost_micros', sa.Integer(), nullable=True),
            sa.Column('currency', sa.String(3), nullable=True),
            sa.Column('cost_basis', sa.String(16), nullable=True),
            sa.Column('outcome', sa.String(16), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_delivery_attempt_costs'),
            sa.CheckConstraint("outcome = 'pending' OR outcome = 'success' OR outcome = 'failed' "
                               "OR outcome = 'uncertain' OR outcome = 'cancelled'",
                               name='ck_delivery_attempt_costs_outcome'),
            sa.CheckConstraint("cost_basis IS NULL OR cost_basis = 'measured' OR cost_basis = 'estimated'",
                               name='ck_delivery_attempt_costs_basis'),
            sa.ForeignKeyConstraint(['id'], ['outbound_attempts.id'],
                                    name='fk_delivery_attempt_costs_attempt', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'],
                                    name='fk_delivery_attempt_costs_job', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['rate_card_id'], ['provider_rate_cards.id'],
                                    name='fk_delivery_attempt_costs_rate_card', ondelete='SET NULL'),
        ),
        'intake_connectors': (
            _id(),
            sa.Column('kind', sa.String(16), nullable=False),
            sa.Column('name', sa.String(100), nullable=False),
            sa.Column('enabled', sa.Integer(), nullable=False),
            sa.Column('match_number', sa.String(32), nullable=True),
            sa.Column('settings', sa.Text(), nullable=False),
            sa.Column('secret_envelope', sa.Text(), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_intake_connectors'),
            sa.CheckConstraint("kind = 'email'", name='ck_intake_connectors_kind'),
            sa.CheckConstraint('(enabled = 0 OR enabled = 1) AND version >= 1', name='ck_intake_connectors_flags'),
        ),
        'direct_deliveries': (
            _id(),
            sa.Column('direction', sa.String(16), nullable=False),
            sa.Column('message_id', sa.String(64), nullable=False),
            _id('peer_id', nullable=True),
            _id('job_id', nullable=True),
            _id('attempt_id', nullable=True),
            sa.Column('recipient_number', sa.String(32), nullable=False),
            sa.Column('digest', sa.String(64), nullable=False),
            sa.Column('size_bytes', sa.Integer(), nullable=False),
            sa.Column('manifest', sa.Text(), nullable=False),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('receipt', sa.Text(), nullable=True),
            sa.Column('document_path', sa.String(512), nullable=True),
            sa.Column('accepted_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_direct_deliveries'),
            sa.CheckConstraint("direction = 'outbound' OR direction = 'inbound'", name='ck_direct_deliveries_direction'),
            sa.CheckConstraint("state = 'sending' OR state = 'accepted' OR state = 'refused' OR state = 'uncertain'",
                               name='ck_direct_deliveries_state'),
            sa.CheckConstraint('size_bytes >= 0', name='ck_direct_deliveries_size'),
            sa.ForeignKeyConstraint(['peer_id'], ['direct_peers.id'],
                                    name='fk_direct_deliveries_peer', ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['job_id'], ['fax_jobs.id'],
                                    name='fk_direct_deliveries_job', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['attempt_id'], ['outbound_attempts.id'],
                                    name='fk_direct_deliveries_attempt', ondelete='SET NULL'),
        ),
        'intake_items': (
            _id(),
            sa.Column('source', sa.String(16), nullable=False),
            _id('inbound_fax_id', nullable=True),
            _id('direct_delivery_id', nullable=True),
            sa.Column('received_at', sa.DateTime(), nullable=False),
            sa.Column('pages', sa.Integer(), nullable=True),
            sa.Column('from_number', sa.String(64), nullable=True),
            sa.Column('to_number', sa.String(64), nullable=True),
            sa.Column('state', sa.String(16), nullable=False),
            sa.Column('attempts', sa.Integer(), nullable=False),
            sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
            sa.Column('last_error', sa.String(200), nullable=True),
            _id('connector_id', nullable=True),
            sa.Column('delivery_reference', sa.String(255), nullable=True),
            sa.Column('delivered_at', sa.DateTime(), nullable=True),
            sa.Column('claim_token', sa.String(64), nullable=True),
            sa.Column('claim_expires_at', sa.DateTime(), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_intake_items'),
            sa.CheckConstraint("source = 'fax' OR source = 'direct'", name='ck_intake_items_source'),
            sa.CheckConstraint("state = 'received' OR state = 'sending' OR state = 'delivered' OR state = 'failed'",
                               name='ck_intake_items_state'),
            sa.CheckConstraint('(inbound_fax_id IS NOT NULL AND direct_delivery_id IS NULL) '
                               'OR (inbound_fax_id IS NULL AND direct_delivery_id IS NOT NULL) '
                               'OR (inbound_fax_id IS NULL AND direct_delivery_id IS NULL)',
                               name='ck_intake_items_origin'),
            sa.CheckConstraint('attempts >= 0 AND version >= 1', name='ck_intake_items_counters'),
            sa.ForeignKeyConstraint(['inbound_fax_id'], ['inbound_faxes.id'],
                                    name='fk_intake_items_inbound_fax', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['direct_delivery_id'], ['direct_deliveries.id'],
                                    name='fk_intake_items_direct_delivery', ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['connector_id'], ['intake_connectors.id'],
                                    name='fk_intake_items_connector', ondelete='SET NULL'),
        ),
        'case_documents': (
            _id(),
            sa.Column('case_id', sa.String(100), nullable=False),
            sa.Column('recipient', sa.String(32), nullable=False),
            sa.Column('digest', sa.String(64), nullable=False),
            sa.Column('title', sa.String(200), nullable=False),
            sa.Column('page_count', sa.Integer(), nullable=False),
            sa.Column('first_page', sa.Integer(), nullable=True),
            sa.Column('last_page', sa.Integer(), nullable=True),
            _id('source_job_id', nullable=True),
            sa.Column('accepted_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_case_documents'),
            sa.CheckConstraint('page_count >= 1', name='ck_case_documents_pages'),
            sa.ForeignKeyConstraint(['source_job_id'], ['fax_jobs.id'],
                                    name='fk_case_documents_source_job', ondelete='SET NULL'),
        ),
    }


INDEXES = (
    ('ix_provider_rate_cards_provider', 'provider_rate_cards', ('provider_id', 'direction'), False),
    ('ix_direct_peers_phone_number', 'direct_peers', ('phone_number',), False),
    ('uq_direct_peers_signing_key', 'direct_peers', ('signing_key',), True),
    ('uq_delivery_destinations_phone_number', 'delivery_destinations', ('phone_number',), True),
    ('ix_delivery_attempt_costs_destination', 'delivery_attempt_costs', ('destination', 'created_at'), False),
    ('ix_delivery_attempt_costs_job', 'delivery_attempt_costs', ('job_id',), False),
    ('ix_delivery_attempt_costs_created_at', 'delivery_attempt_costs', ('created_at',), False),
    ('uq_direct_deliveries_message', 'direct_deliveries', ('direction', 'message_id'), True),
    ('ix_direct_deliveries_job', 'direct_deliveries', ('job_id',), False),
    ('uq_intake_items_inbound_fax', 'intake_items', ('inbound_fax_id',), True),
    ('uq_intake_items_direct_delivery', 'intake_items', ('direct_delivery_id',), True),
    ('ix_intake_items_state_next', 'intake_items', ('state', 'next_attempt_at'), False),
    ('ix_intake_items_received_at', 'intake_items', ('received_at',), False),
    ('uq_case_documents_identity', 'case_documents', ('case_id', 'recipient', 'digest'), True),
    ('ix_case_documents_source_job', 'case_documents', ('source_job_id',), False),
)


def frozen_metadata(*, dialect='sqlite'):
    metadata = capabilities_metadata(dialect=dialect)
    definitions = _definitions()
    tables = {name: sa.Table(name, metadata, *definitions[name]) for name in ORDER}
    for index, table, columns, unique in INDEXES:
        sa.Index(index, *(tables[table].c[column] for column in columns), unique=unique)
    return metadata


def upgrade_delivery(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse
    # any same-name object before DDL; never adopt an existing table or view.
    inspector = sa.inspect(connection)
    if any(inspector.has_table(name) for name in ORDER):
        from .schema import SchemaUpgradeError
        raise SchemaUpgradeError('Delivery route tables already exist before their migration.')
    definitions = _definitions()
    for name in ORDER:
        operations.create_table(name, *definitions[name])
    for index, table, columns, unique in INDEXES:
        operations.create_index(index, table, list(columns), unique=unique)
