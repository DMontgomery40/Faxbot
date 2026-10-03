"""Frozen revision0003 configuration/profile schema; independent of the ORM."""
import sqlalchemy as sa

from .schema_legacy import frozen_metadata as legacy_metadata

REVISION = '0003_configuration'
TABLES = frozenset({
    'configuration_revisions', 'provider_profiles', 'configuration_state',
    'fax_job_bindings', 'inbound_fax_bindings',
})
PROVIDER_COLUMNS = {
    'fax_jobs': ('backend', 'outbound_backend'),
    'inbound_faxes': ('backend', 'inbound_backend'),
}


def frozen_metadata(*, dialect='sqlite'):
    metadata = legacy_metadata()
    # SQLite never enforced the declared VARCHAR length. Preserve its historical
    # declarations instead of rebuilding referenced core tables just to relabel it.
    if dialect == 'postgresql':
        for table, columns in PROVIDER_COLUMNS.items():
            for column in columns:
                metadata.tables[table].c[column].type = sa.String(255)
    sa.Table('configuration_revisions', metadata,
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('parent_id', sa.String(40), sa.ForeignKey('configuration_revisions.id', ondelete='RESTRICT')),
        sa.Column('format_version', sa.Integer, nullable=False),
        sa.Column('key_id', sa.String(64), nullable=False),
        sa.Column('envelope', sa.Text, nullable=False),
        sa.Column('actor', sa.String(100), nullable=False),
        sa.Column('created_at', sa.DateTime, nullable=False),
    )
    sa.Table('provider_profiles', metadata,
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('account_id', sa.String(40), nullable=False),
        sa.Column('provider_id', sa.String(255), nullable=False),
        sa.Column('format_version', sa.Integer, nullable=False),
        sa.Column('key_id', sa.String(64), nullable=False),
        sa.Column('envelope', sa.Text, nullable=False),
        sa.Column('created_at', sa.DateTime, nullable=False),
    )
    sa.Table('configuration_state', metadata,
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('installation_id', sa.String(40), nullable=False),
        sa.Column('generation', sa.Integer, nullable=False),
        sa.Column('active_revision_id', sa.String(40), sa.ForeignKey('configuration_revisions.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('pending_revision_id', sa.String(40), sa.ForeignKey('configuration_revisions.id', ondelete='RESTRICT')),
        sa.Column('updated_at', sa.DateTime, nullable=False),
    )
    for name, parent in [('fax_job_bindings', 'fax_jobs'), ('inbound_fax_bindings', 'inbound_faxes')]:
        table = sa.Table(name, metadata,
            sa.Column('id', sa.String(40), sa.ForeignKey(parent + '.id', ondelete='CASCADE'), primary_key=True),
            sa.Column('revision_id', sa.String(40), sa.ForeignKey('configuration_revisions.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('profile_id', sa.String(40), sa.ForeignKey('provider_profiles.id', ondelete='RESTRICT'), nullable=False),
        )
        sa.Index('ix_' + name + '_profile_id', table.c.profile_id)
    return metadata


def upgrade_configuration(connection, operations):
    if connection.dialect.name == 'postgresql':
        for table, columns in PROVIDER_COLUMNS.items():
            for column in columns:
                operations.alter_column(table, column, existing_type=sa.String(20), type_=sa.String(255))
    metadata = frozen_metadata(dialect=connection.dialect.name)
    for table in metadata.sorted_tables:
        if table.name in TABLES:
            table.create(connection)
