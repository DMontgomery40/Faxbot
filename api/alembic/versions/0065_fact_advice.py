"""Fact advice: answers about your numbers' dependencies, and each number's move as a checked plan."""
from alembic import op

revision = '0065_fact_advice'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0052_digital_routes'
branch_labels = None
depends_on = None


def upgrade():
    fact_advice = op.get_context().config.attributes['schema_fact_advice']
    fact_advice.upgrade_fact_advice(op.get_bind(), op)


def downgrade():
    # Refused while any answer or move is recorded; otherwise the tables go.
    fact_advice = op.get_context().config.attributes['schema_fact_advice']
    fact_advice.downgrade_fact_advice(op.get_bind(), op)
