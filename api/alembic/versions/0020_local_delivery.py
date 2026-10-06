"""Deliver faxes to the installation's own numbers inside Faxbot: a ``local`` received-fax source and a per-fax call request."""
from alembic import op

revision = '0020_local_delivery'
down_revision = '0019_retired_permissions'
branch_labels = None
depends_on = None


def upgrade():
    local = op.get_context().config.attributes['schema_local_delivery']
    local.upgrade_local_delivery(op.get_bind(), op)


def downgrade():
    # Refused while any fax delivered inside Faxbot is recorded; otherwise the column and source go.
    local = op.get_context().config.attributes['schema_local_delivery']
    local.downgrade_local_delivery(op.get_bind(), op)
