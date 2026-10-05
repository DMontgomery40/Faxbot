"""Record which fax engine handled each trunk call, what SSL Fax did, and per-recipient fax limits."""
from alembic import op

revision = '0017_fax_engine'
down_revision = '0016_case_packet_sends'
branch_labels = None
depends_on = None


def upgrade():
    fax_engine = op.get_context().config.attributes['schema_fax_engine']
    fax_engine.upgrade_fax_engine(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
