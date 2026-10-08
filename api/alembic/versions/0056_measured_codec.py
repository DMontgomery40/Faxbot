"""Measured codec: the fax coding each attempt asked for, what was measured on its pages, and why."""
from alembic import op

revision = '0056_measured_codec'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0054_engine_extras'
branch_labels = None
depends_on = None


def upgrade():
    measured = op.get_context().config.attributes['schema_measured_codec']
    measured.upgrade_measured_codec(op.get_bind(), op)


def downgrade():
    measured = op.get_context().config.attributes['schema_measured_codec']
    measured.downgrade_measured_codec(op.get_bind(), op)
