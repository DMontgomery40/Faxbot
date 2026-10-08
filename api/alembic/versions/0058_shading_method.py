"""Shading method: whether each attempt's shaded areas were kept with a pattern or made white."""
from alembic import op

revision = '0058_shading_method'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0056_measured_codec'
branch_labels = None
depends_on = None


def upgrade():
    shading = op.get_context().config.attributes['schema_shading_method']
    shading.upgrade_shading_method(op.get_bind(), op)


def downgrade():
    # Refused while any attempt records how its shaded areas were sent.
    shading = op.get_context().config.attributes['schema_shading_method']
    shading.downgrade_shading_method(op.get_bind(), op)
