"""Number advice: your NPIs, NPPES reads and the numbers they list, and US prices by jurisdiction."""
from alembic import op

revision = '0055_number_advice'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0053_setup_plans'
branch_labels = None
depends_on = None


def upgrade():
    advice = op.get_context().config.attributes['schema_number_advice']
    advice.upgrade_number_advice(op.get_bind(), op)


def downgrade():
    # Refused while an NPI or a price by jurisdiction you entered is saved.
    advice = op.get_context().config.attributes['schema_number_advice']
    advice.downgrade_number_advice(op.get_bind(), op)
