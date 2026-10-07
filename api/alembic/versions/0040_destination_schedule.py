"""Destination schedule: a fax's send-by time and the hours each recipient takes faxes."""
from alembic import op

revision = '0040_destination_schedule'
down_revision = '0030_receiving_accounts'
branch_labels = None
depends_on = None


def upgrade():
    schedule = op.get_context().config.attributes['schema_destination_schedule']
    schedule.upgrade_destination_schedule(op.get_bind(), op)


def downgrade():
    # Refused while any recipient schedule or send-by time is recorded; otherwise they go.
    schedule = op.get_context().config.attributes['schema_destination_schedule']
    schedule.downgrade_destination_schedule(op.get_bind(), op)
