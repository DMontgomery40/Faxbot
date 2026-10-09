"""Polled transmission: faxes held for another site to collect, their collections, and the polling password and
timetable for collecting."""
from alembic import op

revision = '0062_polled_transmit'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0063_encoder_tuning'
branch_labels = None
depends_on = None


def upgrade():
    polled = op.get_context().config.attributes['schema_polled_transmit']
    polled.upgrade_polled_transmit(op.get_bind(), op)


def downgrade():
    polled = op.get_context().config.attributes['schema_polled_transmit']
    polled.downgrade_polled_transmit(op.get_bind(), op)
