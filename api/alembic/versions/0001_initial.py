"""Initial schema

Revision ID: 0001_initial
Revises: 
Create Date: 2025-09-08

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'fax_jobs',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('to_number', sa.String(length=64), nullable=False, index=True),
        sa.Column('file_name', sa.String(length=255), nullable=False),
        sa.Column('tiff_path', sa.String(length=512), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('pages', sa.Integer(), nullable=True),
        sa.Column('backend', sa.String(length=20), nullable=False),
        sa.Column('provider_sid', sa.String(length=100), nullable=True),
        sa.Column('pdf_url', sa.String(length=512), nullable=True),
        sa.Column('pdf_token', sa.String(length=128), nullable=True),
        sa.Column('pdf_token_expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_fax_jobs_to_number', 'fax_jobs', ['to_number'])
    op.create_index('ix_fax_jobs_status', 'fax_jobs', ['status'])

    op.create_table(
        'api_keys',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('key_id', sa.String(length=32), nullable=False),
        sa.Column('key_hash', sa.String(length=200), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=True),
        sa.Column('owner', sa.String(length=100), nullable=True),
        sa.Column('scopes', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
    )
    op.create_index('ix_api_keys_key_id', 'api_keys', ['key_id'], unique=True)

    op.create_table(
        'inbound_faxes',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('from_number', sa.String(length=64), nullable=True),
        sa.Column('to_number', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('backend', sa.String(length=20), nullable=False),
        sa.Column('provider_sid', sa.String(length=100), nullable=True),
        sa.Column('pages', sa.Integer(), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('pdf_path', sa.String(length=512), nullable=True),
        sa.Column('tiff_path', sa.String(length=512), nullable=True),
        sa.Column('mailbox_label', sa.String(length=100), nullable=True),
        sa.Column('retention_until', sa.DateTime(), nullable=True),
        sa.Column('pdf_token', sa.String(length=128), nullable=True),
        sa.Column('pdf_token_expires_at', sa.DateTime(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_inbound_faxes_to_number', 'inbound_faxes', ['to_number'])
    op.create_index('ix_inbound_faxes_status', 'inbound_faxes', ['status'])

    op.create_table(
        'mailboxes',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('label', sa.String(length=100), nullable=False),
        sa.Column('allowed_scopes', sa.String(length=200), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ux_mailboxes_label', 'mailboxes', ['label'], unique=True)

    op.create_table(
        'inbound_rules',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('to_number', sa.String(length=64), nullable=False),
        sa.Column('mailbox_label', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_inbound_rules_to_number', 'inbound_rules', ['to_number'])

    op.create_table(
        'inbound_events',
        sa.Column('id', sa.String(length=40), primary_key=True),
        sa.Column('provider_sid', sa.String(length=100), nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_unique_constraint('uix_inbound_events_sid_type', 'inbound_events', ['provider_sid', 'event_type'])


def downgrade() -> None:
    op.drop_constraint('uix_inbound_events_sid_type', 'inbound_events', type_='unique')
    op.drop_table('inbound_events')
    op.drop_index('ix_inbound_rules_to_number', table_name='inbound_rules')
    op.drop_table('inbound_rules')
    op.drop_index('ux_mailboxes_label', table_name='mailboxes')
    op.drop_table('mailboxes')
    op.drop_index('ix_inbound_faxes_status', table_name='inbound_faxes')
    op.drop_index('ix_inbound_faxes_to_number', table_name='inbound_faxes')
    op.drop_table('inbound_faxes')
    op.drop_index('ix_api_keys_key_id', table_name='api_keys')
    op.drop_table('api_keys')
    op.drop_index('ix_fax_jobs_status', table_name='fax_jobs')
    op.drop_index('ix_fax_jobs_to_number', table_name='fax_jobs')
    op.drop_table('fax_jobs')

