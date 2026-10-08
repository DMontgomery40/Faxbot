"""Guided setup: plans an administrator previewed, and each time one was applied."""
from alembic import op

revision = '0053_setup_plans'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0049_send_once'
branch_labels = None
depends_on = None


def upgrade():
    plans = op.get_context().config.attributes['schema_setup_plans']
    plans.upgrade_setup_plans(op.get_bind(), op)


def downgrade():
    # Refused while any plan is recorded; otherwise the tables go.
    plans = op.get_context().config.attributes['schema_setup_plans']
    plans.downgrade_setup_plans(op.get_bind(), op)
