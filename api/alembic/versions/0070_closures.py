"""Copper-closure dates by commune, carrier notice dates per line, and country eligibility confirmations."""
from alembic import op
revision = '0070_closures'
down_revision = '0068_dialing_guard'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_closures'].upgrade_closures(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_closures'].downgrade_closures(op.get_bind(), op)
