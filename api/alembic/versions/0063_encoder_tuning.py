"""Encoder tuning: your per-number lossless tuning choices, and what the engine reported tuning on each call."""
from alembic import op

revision = '0063_encoder_tuning'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0065_fact_advice'
branch_labels = None
depends_on = None


def upgrade():
    tuning = op.get_context().config.attributes['schema_encoder_tuning']
    tuning.upgrade_encoder_tuning(op.get_bind(), op)


def downgrade():
    tuning = op.get_context().config.attributes['schema_encoder_tuning']
    tuning.downgrade_encoder_tuning(op.get_bind(), op)
