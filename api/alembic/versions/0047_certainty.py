"""Certainty: an owned record for each uncertain sent fax, its checks, and who settled it."""
from alembic import op

revision = '0047_certainty'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0043_accounts'
branch_labels = None
depends_on = None


def upgrade():
    certainty = op.get_context().config.attributes['schema_certainty']
    certainty.upgrade_certainty(op.get_bind(), op)


def downgrade():
    # Refused while any uncertain sent fax is recorded; otherwise the tables go.
    certainty = op.get_context().config.attributes['schema_certainty']
    certainty.downgrade_certainty(op.get_bind(), op)
