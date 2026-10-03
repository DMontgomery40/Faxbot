"""Add frozen access identities and conservative legacy credential enrollment."""
from alembic import op

revision = '0005_access_control'
down_revision = '0004_outbound_delivery'
branch_labels = None
depends_on = None


def upgrade():
    access = op.get_context().config.attributes['schema_access']
    access.upgrade_access(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
