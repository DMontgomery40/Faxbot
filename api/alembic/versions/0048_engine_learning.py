"""Engine learning: per-number T.38 and audio memory, learning epochs, and what each sent call used."""
from alembic import op

revision = '0048_engine_learning'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0040_destination_schedule'
branch_labels = None
depends_on = None


def upgrade():
    learning = op.get_context().config.attributes['schema_engine_learning']
    learning.upgrade_engine_learning(op.get_bind(), op)


def downgrade():
    learning = op.get_context().config.attributes['schema_engine_learning']
    learning.downgrade_engine_learning(op.get_bind(), op)
