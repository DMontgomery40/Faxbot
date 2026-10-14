"""Fax servers that send through Faxbot (RightFax and others, store and forward), and one record per call."""
from alembic import op
revision = '0086_fax_servers'
down_revision = '0085_copier_extensions'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_fax_servers'].upgrade_fax_servers(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_fax_servers'].downgrade_fax_servers(op.get_bind(), op)
