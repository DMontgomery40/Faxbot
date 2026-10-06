"""Record what each case packet left out, so its savings can be counted."""
from alembic import op

revision = '0016_case_packet_sends'
down_revision = '0015_inbound_sources'
branch_labels = None
depends_on = None


def upgrade():
    case_packets = op.get_context().config.attributes['schema_case_packets']
    case_packets.upgrade_case_packets(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
