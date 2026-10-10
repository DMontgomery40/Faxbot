"""Your fax lines with addresses and contracts, carrier lists of discontinued or grandfathered areas, and line
notice kinds."""
from alembic import op
revision = '0077_line_inventory'
down_revision = '0076_after_answer'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_line_inventory'].upgrade_line_inventory(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_line_inventory'].downgrade_line_inventory(op.get_bind(), op)
