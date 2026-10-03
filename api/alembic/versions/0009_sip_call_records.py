"""Add per-call records for faxes placed or received over the SIP trunk."""
from alembic import op

revision = '0009_sip_call_records'
down_revision = '0008_delivery_routes'
branch_labels = None
depends_on = None


def upgrade():
    sip = op.get_context().config.attributes['schema_sip']
    sip.upgrade_sip(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
