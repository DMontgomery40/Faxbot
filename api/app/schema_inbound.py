"""Frozen 0010 inbound imports; registration and validation belong to ``schema``.

One row per received document Faxbot is acquiring from a source: a provider
notification (Phaxio, Sinch), a call answered on the SIP trunk, a generic
import, or a test fax. The row is the durable acquisition record next to the
``inbound_faxes`` row people see: it holds the authenticated source identity,
the fetch lease and retry schedule, the time the source reported against the
time Faxbot imported it, a bounded sanitized copy of the notification, and the
digest of the artifact once it is validated and stored.

``(source, account, operation_id, revision)`` is the source identity. The same
provider fax id under two accounts is two rows; a replay of one notification
resumes the same row. ``revision`` is ``''`` for the original; a declared new
source revision is a related row. Runtime code reflects the table; it never
imports this metadata.
"""
import sqlalchemy as sa

from .schema_sip import frozen_metadata as previous_metadata


REVISION = '0010_inbound_imports'
TABLES = frozenset({'inbound_imports'})
SOURCES = ('phaxio', 'sinch', 'sip', 'import', 'test')
STATES = ('pending', 'received', 'failed', 'conflict')
INDEXES = (
    ('uq_inbound_imports_source', ('source', 'account', 'operation_id', 'revision'), True),
    ('ix_inbound_imports_due', ('state', 'next_attempt_at'), False),
    ('ix_inbound_imports_inbound_fax', ('inbound_fax_id',), False),
)


def _definition(sources=SOURCES):
    """The 0010 table; a later revision passes its own source list (see schema_inbound_sources)."""
    sources = ' OR '.join(f"source = '{value}'" for value in sources)
    states = ' OR '.join(f"state = '{value}'" for value in STATES)
    return (
        sa.Column('id', sa.String(40), nullable=False),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column('account', sa.String(100), nullable=False),
        sa.Column('operation_id', sa.String(100), nullable=False),
        sa.Column('revision', sa.String(40), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('claim_token', sa.String(64), nullable=True),
        sa.Column('claim_expires_at', sa.DateTime(), nullable=True),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.Column('source_received_at', sa.DateTime(), nullable=True),
        sa.Column('acquired_at', sa.DateTime(), nullable=True),
        sa.Column('to_number', sa.String(64), nullable=True),
        sa.Column('from_number', sa.String(64), nullable=True),
        sa.Column('reported_pages', sa.Integer(), nullable=True),
        sa.Column('report', sa.Text(), nullable=True),
        sa.Column('artifact_digest', sa.String(64), nullable=True),
        sa.Column('artifact_size', sa.Integer(), nullable=True),
        sa.Column('artifact_media_type', sa.String(64), nullable=True),
        sa.Column('inbound_fax_id', sa.String(40), nullable=False),
        sa.Column('last_error', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_inbound_imports'),
        sa.CheckConstraint(sources, name='ck_inbound_imports_source'),
        sa.CheckConstraint(states, name='ck_inbound_imports_state'),
        # NULL counts pass (a CHECK only rejects false); no grouping parentheses,
        # which PostgreSQL reflection does not round-trip for this grammar.
        sa.CheckConstraint('attempts >= 0 AND reported_pages >= 0 AND artifact_size >= 0',
                           name='ck_inbound_imports_counts'),
        # A received (or later conflicting) import always names its stored artifact.
        sa.CheckConstraint("state = 'pending' OR state = 'failed' OR artifact_digest IS NOT NULL "
                           "AND artifact_size IS NOT NULL AND acquired_at IS NOT NULL",
                           name='ck_inbound_imports_artifact'),
        sa.ForeignKeyConstraint(['inbound_fax_id'], ['inbound_faxes.id'],
                                name='fk_inbound_imports_inbound_fax', ondelete='CASCADE'),
    )


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    table = sa.Table('inbound_imports', metadata, *_definition())
    for name, columns, unique in INDEXES:
        sa.Index(name, *(table.c[column] for column in columns), unique=unique)
    return metadata


def upgrade_inbound(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a
    # same-name table or view before any DDL; never adopt an existing object.
    if sa.inspect(connection).has_table('inbound_imports'):
        from .schema import SchemaUpgradeError
        raise SchemaUpgradeError('Inbound import table already exists before its migration.')
    operations.create_table('inbound_imports', *_definition())
    for name, columns, unique in INDEXES:
        operations.create_index(name, 'inbound_imports', list(columns), unique=unique)
