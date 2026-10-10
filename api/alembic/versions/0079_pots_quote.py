"""The per-line price of a POTS-replacement box someone quoted you."""
from alembic import op
revision = '0079_pots_quote'
down_revision = '0078_fax_server_renewal'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_pots_quote'].upgrade_pots_quote(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_pots_quote'].downgrade_pots_quote(op.get_bind(), op)
