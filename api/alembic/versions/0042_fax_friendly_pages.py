"""Fax-friendly pages: one row for each attempt whose shaded areas were lightened, and each recipient's choice."""
from alembic import op

revision = '0042_fax_friendly_pages'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0041_intake_connectors'
branch_labels = None
depends_on = None


def upgrade():
    friendly = op.get_context().config.attributes['schema_friendly_pages']
    friendly.upgrade_friendly_pages(op.get_bind(), op)


def downgrade():
    friendly = op.get_context().config.attributes['schema_friendly_pages']
    friendly.downgrade_friendly_pages(op.get_bind(), op)
