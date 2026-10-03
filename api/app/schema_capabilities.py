"""Frozen 0007 single-use capability table; registration is owned by schema startup.

A capability is a short-lived, one-use secret bound to the principal (and, for
browser flows, the session) that minted it: a terminal handshake ticket or a
mobile pairing code. Only a digest of the secret is stored. ``credential`` keeps
the minting credential's nonsecret evidence and permission, so redemption is
authorized again against current policy and a revoked key or session fails.
"""
import sqlalchemy as sa

from .schema_authentication import frozen_metadata as authentication_metadata


REVISION = '0007_access_capabilities'
TABLES = frozenset({'access_capabilities'})
INDEXES = (
    ('uq_access_capabilities_secret_hash', ('secret_hash',), True),
    ('ix_access_capabilities_expires_at', ('expires_at',), False),
    ('ix_access_capabilities_principal', ('principal_id',), False),
)


def _definition():
    return (
        sa.Column('id', sa.String(40), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('secret_hash', sa.String(64), nullable=False),
        sa.Column('principal_id', sa.String(40), nullable=False),
        sa.Column('session_id', sa.String(40), nullable=True),
        sa.Column('issued_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.Column('metadata', sa.Text(), nullable=False),
        sa.Column('credential', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_access_capabilities'),
        sa.CheckConstraint("kind = 'terminal' OR kind = 'pairing'", name='ck_access_capabilities_kind'),
        sa.CheckConstraint('expires_at >= issued_at AND (consumed_at IS NULL OR consumed_at >= issued_at)',
                           name='ck_access_capabilities_times'),
        sa.ForeignKeyConstraint(['principal_id'], ['access_principals.id'],
                                name='fk_access_capabilities_principal', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['session_id'], ['access_sessions.id'],
                                name='fk_access_capabilities_session', ondelete='RESTRICT'),
    )


def frozen_metadata(*, dialect='sqlite'):
    metadata = authentication_metadata(dialect=dialect)
    table = sa.Table('access_capabilities', metadata, *_definition())
    for name, columns, unique in INDEXES:
        sa.Index(name, *(table.c[column] for column in columns), unique=unique)
    return metadata


def upgrade_capabilities(connection, operations):
    # The guarded migration owns transaction/namespace validation. Refuse even
    # a same-name view before issuing any DDL; never adopt an existing object.
    if sa.inspect(connection).has_table('access_capabilities'):
        from .schema import SchemaUpgradeError
        raise SchemaUpgradeError('Capability table already exists before its migration.')
    operations.create_table('access_capabilities', *_definition())
    for name, columns, unique in INDEXES:
        operations.create_index(name, 'access_capabilities', list(columns), unique=unique)
