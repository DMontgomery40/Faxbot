"""webhook DLQ table

Revision ID: 0003
Revises: 0002
Create Date: 2025-09-26 10:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    """Create webhook_dlq table for dead letter queue."""
    op.create_table('webhook_dlq',
        sa.Column('id', sa.String(40), nullable=False, primary_key=True),
        sa.Column('provider_id', sa.String(40), nullable=False),
        sa.Column('external_id', sa.String(100), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('status', sa.String(20), nullable=False, server_default='queued'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('headers_meta', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.String(10), nullable=False, server_default='0'),
        sa.Column('last_retry_at', sa.DateTime(), nullable=True),
        sa.Column('next_retry_at', sa.DateTime(), nullable=True),
    )

    # Create indexes for efficient queries
    op.create_index('ix_webhook_dlq_provider_id', 'webhook_dlq', ['provider_id'])
    op.create_index('ix_webhook_dlq_status', 'webhook_dlq', ['status'])
    op.create_index('ix_webhook_dlq_external_id', 'webhook_dlq', ['external_id'])
    op.create_index('ix_webhook_dlq_next_retry_at', 'webhook_dlq', ['next_retry_at'])


def downgrade():
    """Drop webhook_dlq table."""
    op.drop_index('ix_webhook_dlq_next_retry_at', table_name='webhook_dlq')
    op.drop_index('ix_webhook_dlq_external_id', table_name='webhook_dlq')
    op.drop_index('ix_webhook_dlq_status', table_name='webhook_dlq')
    op.drop_index('ix_webhook_dlq_provider_id', table_name='webhook_dlq')
    op.drop_table('webhook_dlq')