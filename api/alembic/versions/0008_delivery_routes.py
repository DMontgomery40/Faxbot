"""Add delivery routes, costs, intake queue and direct delivery records."""
from alembic import op

revision = '0008_delivery_routes'
down_revision = '0007_access_capabilities'
branch_labels = None
depends_on = None


def upgrade():
    delivery = op.get_context().config.attributes['schema_delivery']
    delivery.upgrade_delivery(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
