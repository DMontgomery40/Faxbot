"""Hierarchical configuration tables

Revision ID: 0002_hierarchical_config
Revises: 0001_initial
Create Date: 2025-09-26

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0002_hierarchical_config'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Global configuration
    op.create_table(
        'config_global',
        sa.Column('key', sa.String(200), primary_key=True, nullable=False),
        sa.Column('value_encrypted', sa.Text(), nullable=False),
        sa.Column('value_type', sa.String(20), nullable=False, server_default='string'),
        sa.Column('encrypted', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(50), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    # Tenant-level configuration
    op.create_table(
        'config_tenant',
        sa.Column('tenant_id', sa.String(100), nullable=False),
        sa.Column('key', sa.String(200), nullable=False),
        sa.Column('value_encrypted', sa.Text(), nullable=False),
        sa.Column('value_type', sa.String(20), nullable=False, server_default='string'),
        sa.Column('encrypted', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('tenant_id', 'key'),
    )
    op.create_index('idx_tenant_key', 'config_tenant', ['tenant_id', 'key'])

    # Department-level configuration
    op.create_table(
        'config_department',
        sa.Column('tenant_id', sa.String(100), nullable=False),
        sa.Column('department', sa.String(100), nullable=False),
        sa.Column('key', sa.String(200), nullable=False),
        sa.Column('value_encrypted', sa.Text(), nullable=False),
        sa.Column('value_type', sa.String(20), nullable=False, server_default='string'),
        sa.Column('encrypted', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('tenant_id', 'department', 'key'),
    )
    op.create_index('idx_dept_key', 'config_department', ['tenant_id', 'department', 'key'])

    # Group-level configuration
    op.create_table(
        'config_group',
        sa.Column('group_id', sa.String(100), nullable=False),
        sa.Column('key', sa.String(200), nullable=False),
        sa.Column('value_encrypted', sa.Text(), nullable=False),
        sa.Column('value_type', sa.String(20), nullable=False, server_default='string'),
        sa.Column('encrypted', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('group_id', 'key'),
    )
    op.create_index('idx_group_key', 'config_group', ['group_id', 'key'])
    op.create_index('idx_group_priority', 'config_group', ['group_id', 'priority'])

    # User-level configuration
    op.create_table(
        'config_user',
        sa.Column('user_id', sa.String(100), nullable=False),
        sa.Column('key', sa.String(200), nullable=False),
        sa.Column('value_encrypted', sa.Text(), nullable=False),
        sa.Column('value_type', sa.String(20), nullable=False, server_default='string'),
        sa.Column('encrypted', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('user_id', 'key'),
    )
    op.create_index('idx_user_key', 'config_user', ['user_id', 'key'])

    # Configuration audit trail (masked only; integrity fingerprint)
    op.create_table(
        'config_audit',
        sa.Column('id', sa.String(40), primary_key=True, nullable=False),
        sa.Column('level', sa.String(20), nullable=False),
        sa.Column('level_id', sa.String(200), nullable=True),
        sa.Column('key', sa.String(200), nullable=False),
        sa.Column('old_value_masked', sa.Text(), nullable=True),
        sa.Column('new_value_masked', sa.Text(), nullable=False),
        sa.Column('value_hmac', sa.String(64), nullable=False),  # hex sha256
        sa.Column('value_type', sa.String(20), nullable=False),
        sa.Column('changed_by', sa.String(100), nullable=False),
        sa.Column('changed_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
    )
    op.create_index('idx_audit_level', 'config_audit', ['level', 'level_id'])
    op.create_index('idx_audit_key', 'config_audit', ['key'])
    op.create_index('idx_audit_time', 'config_audit', ['changed_at'])
    op.create_index('idx_audit_user', 'config_audit', ['changed_by'])


def downgrade() -> None:
    op.drop_index('idx_audit_user', table_name='config_audit')
    op.drop_index('idx_audit_time', table_name='config_audit')
    op.drop_index('idx_audit_key', table_name='config_audit')
    op.drop_index('idx_audit_level', table_name='config_audit')
    op.drop_table('config_audit')
    op.drop_index('idx_user_key', table_name='config_user')
    op.drop_table('config_user')
    op.drop_index('idx_group_priority', table_name='config_group')
    op.drop_index('idx_group_key', table_name='config_group')
    op.drop_table('config_group')
    op.drop_index('idx_dept_key', table_name='config_department')
    op.drop_table('config_department')
    op.drop_index('idx_tenant_key', table_name='config_tenant')
    op.drop_table('config_tenant')
    op.drop_table('config_global')

