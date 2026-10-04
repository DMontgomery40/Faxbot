"""Add sending short faxes to the same number together in one call."""
from alembic import op

revision = '0014_send_together'
down_revision = '0013_carrier_records'
branch_labels = None
depends_on = None


def upgrade():
    batching = op.get_context().config.attributes['schema_batching']
    batching.upgrade_batching(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
