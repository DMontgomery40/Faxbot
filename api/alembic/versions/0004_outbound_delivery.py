"""Add durable outbound records without authorizing historical sends."""
from alembic import op

revision = '0004_outbound_delivery'
down_revision = '0003_configuration'
branch_labels = None
depends_on = None


def upgrade():
    outbound = op.get_context().config.attributes['schema_outbound']
    outbound.upgrade_outbound(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
