"""Add durable acquisition records for received faxes and imported documents."""
from alembic import op

revision = '0010_inbound_imports'
down_revision = '0009_sip_call_records'
branch_labels = None
depends_on = None


def upgrade():
    inbound = op.get_context().config.attributes['schema_inbound']
    inbound.upgrade_inbound(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
