"""Add bounded shared expensive-authentication admission state."""
from alembic import op

revision = '0006_auth_admission'
down_revision = '0005_access_control'
branch_labels = None
depends_on = None


def upgrade():
    authentication = op.get_context().config.attributes['schema_authentication']
    authentication.upgrade_authentication(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
