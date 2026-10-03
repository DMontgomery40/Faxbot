"""Frozen 0006 admission table; registration is owned by schema startup."""
import sqlalchemy as sa

from .schema_access import frozen_metadata as access_metadata


REVISION = '0006_auth_admission'
TABLES = frozenset({'access_auth_buckets'})


def _definition():
    return (
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('next_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_access_auth_buckets'),
        sa.CheckConstraint('next_at >= updated_at', name='ck_access_auth_buckets_times'),
    )


def frozen_metadata(*, dialect='sqlite'):
    metadata = access_metadata(dialect=dialect)
    table = sa.Table('access_auth_buckets', metadata, *_definition())
    sa.Index('ix_access_auth_buckets_next_at', table.c.next_at)
    return metadata


def upgrade_authentication(connection, operations):
    # The guarded migration owns transaction/namespace validation. Refuse even
    # a same-name view before issuing any DDL; never adopt an existing object.
    if sa.inspect(connection).has_table('access_auth_buckets'):
        from .schema import SchemaUpgradeError
        raise SchemaUpgradeError('Authentication admission table already exists before its migration.')
    operations.create_table('access_auth_buckets', *_definition())
    operations.create_index('ix_access_auth_buckets_next_at', 'access_auth_buckets', ['next_at'])
