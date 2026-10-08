"""Send once to a partner's intake, reuse documents a partner holds, and send only the changes."""
from alembic import op

revision = '0049_send_once'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0046_notice_repair'
branch_labels = None
depends_on = None


def upgrade():
    send_once = op.get_context().config.attributes['schema_send_once']
    send_once.upgrade_send_once(op.get_bind(), op)


def downgrade():
    # Refused while any agreement, send or saving is recorded; otherwise the tables go.
    send_once = op.get_context().config.attributes['schema_send_once']
    send_once.downgrade_send_once(op.get_bind(), op)
