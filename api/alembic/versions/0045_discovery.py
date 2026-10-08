"""Partner discovery: hints from calls, lookups, suggestions, introductions and directory publications."""
from alembic import op

revision = '0045_discovery'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0040_destination_schedule'
branch_labels = None
depends_on = None


def upgrade():
    discovery = op.get_context().config.attributes['schema_discovery']
    discovery.upgrade_discovery(op.get_bind(), op)


def downgrade():
    # Refused while any publication, introduction or consent is recorded; otherwise the tables go.
    discovery = op.get_context().config.attributes['schema_discovery']
    discovery.downgrade_discovery(op.get_bind(), op)
