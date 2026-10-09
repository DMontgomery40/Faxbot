"""Dense pages: receiving machines' longest pages, long-page settings, packed sends and split received faxes."""
from alembic import op

revision = '0028_dense_pages'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0030_receiving_accounts'
branch_labels = None
depends_on = None


def upgrade():
    dense = op.get_context().config.attributes['schema_dense_pages']
    dense.upgrade_dense_pages(op.get_bind(), op)


def downgrade():
    dense = op.get_context().config.attributes['schema_dense_pages']
    dense.downgrade_dense_pages(op.get_bind(), op)
