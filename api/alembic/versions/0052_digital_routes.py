"""Digital routes: recipients' Direct addresses and FHIR endpoints, the messages sent and received, trust bundles."""
from alembic import op

revision = '0052_digital_routes'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0055_number_advice'
branch_labels = None
depends_on = None


def upgrade():
    digital = op.get_context().config.attributes['schema_digital_routes']
    digital.upgrade_digital_routes(op.get_bind(), op)


def downgrade():
    # Refused while any address was confirmed or any message was recorded; otherwise the tables go.
    digital = op.get_context().config.attributes['schema_digital_routes']
    digital.downgrade_digital_routes(op.get_bind(), op)
