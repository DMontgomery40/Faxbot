"""Add carrier charges for SIP trunk calls, the SIP Call-ID of each call and monthly plan fees."""
from alembic import op

revision = '0012_carrier_charges'
down_revision = '0011_work_items'
branch_labels = None
depends_on = None


def upgrade():
    charges = op.get_context().config.attributes['schema_charges']
    charges.upgrade_charges(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
