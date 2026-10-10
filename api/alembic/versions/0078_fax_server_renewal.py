"""Another fax server's call records, its renewal and its number routing."""
from alembic import op
revision = '0078_fax_server_renewal'
down_revision = '0077_line_inventory'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_fax_server_renewal'].upgrade_fax_server_renewal(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_fax_server_renewal'].downgrade_fax_server_renewal(op.get_bind(), op)
