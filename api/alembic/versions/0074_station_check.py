"""The station check: stations each number answers as, what to do when it differs, and what each call showed."""
from alembic import op

revision = '0074_station_check'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0073_header_notice'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_station_check'].upgrade_station_check(op.get_bind(), op)


def downgrade():
    # Refused while any station, setting or result is recorded; otherwise the tables go.
    op.get_context().config.attributes['schema_station_check'].downgrade_station_check(op.get_bind(), op)
