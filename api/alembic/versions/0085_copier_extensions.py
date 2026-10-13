"""Copiers that register to Faxbot's Asterisk and fax through Faxbot."""
from alembic import op
revision = '0085_copier_extensions'
down_revision = '0084_partner_admission'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_copier_extensions'].upgrade_copier_extensions(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_copier_extensions'].downgrade_copier_extensions(op.get_bind(), op)
