"""Rules in delivery: the Approve faxes permission, and whether each attempt ended before any fax data."""
from alembic import op

revision = '0032_rules_delivery'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0043_accounts'
branch_labels = None
depends_on = None


def upgrade():
    delivery = op.get_context().config.attributes['schema_rules_delivery']
    delivery.upgrade_rules_delivery(op.get_bind(), op)


def downgrade():
    # Refused while a role or integration key you made holds Approve faxes.
    delivery = op.get_context().config.attributes['schema_rules_delivery']
    delivery.downgrade_rules_delivery(op.get_bind(), op)
