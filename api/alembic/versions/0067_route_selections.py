"""The account and pages each attempt was bound to after its accounts were compared (brief 84, JO)."""
from alembic import op

revision = '0067_route_selections'
down_revision = '0066_analysis'
branch_labels = None
depends_on = None


def upgrade():
    selections = op.get_context().config.attributes['schema_route_selections']
    selections.upgrade_route_selections(op.get_bind(), op)


def downgrade():
    selections = op.get_context().config.attributes['schema_route_selections']
    selections.downgrade_route_selections(op.get_bind(), op)
