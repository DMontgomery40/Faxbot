"""Partner relay: agreements, signed statements and relayed faxes between enrolled partners."""
from alembic import op

revision = '0044_partner_relay'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0042_fax_friendly_pages'
branch_labels = None
depends_on = None


def upgrade():
    relay = op.get_context().config.attributes['schema_partner_relay']
    relay.upgrade_partner_relay(op.get_bind(), op)


def downgrade():
    # Refused while any agreement, statement or relayed fax is recorded; otherwise the tables go.
    relay = op.get_context().config.attributes['schema_partner_relay']
    relay.downgrade_partner_relay(op.get_bind(), op)
