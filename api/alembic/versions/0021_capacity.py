"""Capacity inputs: urgent faxes and how many calls at once a recipient's number takes."""
from alembic import op

revision = '0021_capacity'
down_revision = '0020_local_delivery'
branch_labels = None
depends_on = None


def upgrade():
    capacity = op.get_context().config.attributes['schema_capacity']
    capacity.upgrade_capacity(op.get_bind(), op)


def downgrade():
    capacity = op.get_context().config.attributes['schema_capacity']
    capacity.downgrade_capacity(op.get_bind(), op)
