"""Remove three permissions that guard nothing: host:actions, tunnels:read and tunnels:manage."""
from alembic import op

revision = '0019_retired_permissions'
down_revision = '0018_terminal_owner_only'
branch_labels = None
depends_on = None


def upgrade():
    retired = op.get_context().config.attributes['schema_retired_permissions']
    retired.upgrade_retired_permissions(op.get_bind(), op)


def downgrade():
    # Only this revision's rows come back; earlier revisions before 0018 still refuse a downgrade.
    retired = op.get_context().config.attributes['schema_retired_permissions']
    retired.downgrade_retired_permissions(op.get_bind(), op)
