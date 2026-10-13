"""Which encoded pages a partner's Faxbot reads, and what you last told it."""
from alembic import op
revision = '0082_partner_layouts'
down_revision = '0079_pots_quote'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_partner_layouts'].upgrade_partner_layouts(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_partner_layouts'].downgrade_partner_layouts(op.get_bind(), op)
