"""Add immutable configuration/profile storage and preserve long provider IDs."""
from alembic import op

revision = '0003_configuration'
down_revision = '0002_schema_foundation'
branch_labels = None
depends_on = None


def upgrade():
    configuration = op.get_context().config.attributes['schema_configuration']
    configuration.upgrade_configuration(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
