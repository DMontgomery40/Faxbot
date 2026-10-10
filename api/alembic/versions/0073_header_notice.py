"""The header notice: a notice line on every page, a cover sent as that notice, and recipients that need a cover."""
from alembic import op

revision = '0073_header_notice'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0068_dialing_guard'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_header_notice'].upgrade_header_notice(op.get_bind(), op)


def downgrade():
    # Refused while any notice, noticed fax or cover requirement is recorded; otherwise the tables go.
    op.get_context().config.attributes['schema_header_notice'].downgrade_header_notice(op.get_bind(), op)
