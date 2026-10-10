"""Public test lines from Diagnostics, their replies, and the calls the answer cap ended."""
from alembic import op

revision = '0075_test_lines'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0072_route_families'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_test_lines'].upgrade_test_lines(op.get_bind(), op)


def downgrade():
    # Refused while any test fax, reply or capped call is recorded; otherwise the tables go.
    op.get_context().config.attributes['schema_test_lines'].downgrade_test_lines(op.get_bind(), op)
