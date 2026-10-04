"""Allow every built-in provider, including eFax, as the source of a received fax."""
from alembic import op

revision = '0015_inbound_sources'
down_revision = '0014_send_together'
branch_labels = None
depends_on = None


def upgrade():
    sources = op.get_context().config.attributes['schema_inbound_sources']
    sources.upgrade_sources(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
