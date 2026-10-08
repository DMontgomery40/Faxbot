"""Routing learning: the codings each receiving machine takes, and collecting faxes by polling."""
from alembic import op

revision = '0059_routing_learning'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0058_shading_method'
branch_labels = None
depends_on = None


def upgrade():
    learning = op.get_context().config.attributes['schema_routing_learning']
    learning.upgrade_routing_learning(op.get_bind(), op)


def downgrade():
    learning = op.get_context().config.attributes['schema_routing_learning']
    learning.downgrade_routing_learning(op.get_bind(), op)
