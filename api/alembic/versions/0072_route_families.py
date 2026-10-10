"""Route families, shared upstreams, receive owners and the UPS Faxbot reads (brief 92, RF)."""
from alembic import op

revision = '0072_route_families'
down_revision = '0074_station_check'
branch_labels = None
depends_on = None


def upgrade():
    families = op.get_context().config.attributes['schema_route_families']
    families.upgrade_route_families(op.get_bind(), op)


def downgrade():
    families = op.get_context().config.attributes['schema_route_families']
    families.downgrade_route_families(op.get_bind(), op)
