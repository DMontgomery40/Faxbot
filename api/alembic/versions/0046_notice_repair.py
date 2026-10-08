"""Notice faxes, resumable transfers and repaired calls between enrolled partners."""
from alembic import op

revision = '0046_notice_repair'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0044_partner_relay'
branch_labels = None
depends_on = None


def upgrade():
    notice = op.get_context().config.attributes['schema_notice_repair']
    notice.upgrade_notice_repair(op.get_bind(), op)


def downgrade():
    # Refused while any notice fax, transfer or repaired call is recorded; otherwise the tables and columns go.
    notice = op.get_context().config.attributes['schema_notice_repair']
    notice.downgrade_notice_repair(op.get_bind(), op)
