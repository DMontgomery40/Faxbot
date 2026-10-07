"""Receiving accounts and rules: the account each fax came in on, the trunk of each call, rule options and placement."""
from alembic import op

revision = '0030_receiving_accounts'
down_revision = '0029_routing_rules'
branch_labels = None
depends_on = None


def upgrade():
    receiving = op.get_context().config.attributes['schema_receiving_rules']
    receiving.upgrade_receiving_accounts(op.get_bind(), op)


def downgrade():
    # Refused while any receiving account, rule option or placement is recorded; otherwise they go.
    receiving = op.get_context().config.attributes['schema_receiving_rules']
    receiving.downgrade_receiving_accounts(op.get_bind(), op)
