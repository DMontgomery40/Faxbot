"""Add single-use terminal and pairing capabilities."""
from alembic import op

revision = '0007_access_capabilities'
down_revision = '0006_auth_admission'
branch_labels = None
depends_on = None


def upgrade():
    capabilities = op.get_context().config.attributes['schema_capabilities']
    capabilities.upgrade_capabilities(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
