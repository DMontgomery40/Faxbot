"""Accounts: the subaddress a receiving rule matches and the one each received fax stated."""
from alembic import op

revision = '0043_accounts'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0041_intake_connectors'
branch_labels = None
depends_on = None


def upgrade():
    accounts = op.get_context().config.attributes['schema_accounts']
    accounts.upgrade_accounts(op.get_bind(), op)


def downgrade():
    # Refused while any rule or received fax records a subaddress; otherwise they go.
    accounts = op.get_context().config.attributes['schema_accounts']
    accounts.downgrade_accounts(op.get_bind(), op)
