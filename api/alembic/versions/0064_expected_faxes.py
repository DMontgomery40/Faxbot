"""Expected faxes: expectations held before anything arrives, their matches, imports and outage reconciliation."""
from alembic import op

revision = '0064_expected_faxes'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0056_measured_codec'
branch_labels = None
depends_on = None


def upgrade():
    expected = op.get_context().config.attributes['schema_expected_faxes']
    expected.upgrade_expected_faxes(op.get_bind(), op)


def downgrade():
    # Refused while any expectation, import run or outage is recorded; otherwise the tables go.
    expected = op.get_context().config.attributes['schema_expected_faxes']
    expected.downgrade_expected_faxes(op.get_bind(), op)
