"""Intake connectors: email mailboxes and watched folders that file documents and send faxes once."""
from alembic import op

revision = '0041_intake_connectors'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0040_destination_schedule'
branch_labels = None
depends_on = None


def upgrade():
    sources = op.get_context().config.attributes['schema_intake_sources']
    sources.upgrade_intake_sources(op.get_bind(), op)


def downgrade():
    # Refused while any connector, sender or item is recorded; otherwise the tables go.
    sources = op.get_context().config.attributes['schema_intake_sources']
    sources.downgrade_intake_sources(op.get_bind(), op)
