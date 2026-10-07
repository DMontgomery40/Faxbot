"""Dense pages: receiving machines' longest pages, long-page settings, packed sends and split received faxes."""
from alembic import op

revision = '0028_dense_pages'
# Later revisions of this wave come first at integration; the lead sets down_revision to the highest.
down_revision = '0023_negotiation'
branch_labels = None
depends_on = None


def upgrade():
    dense = op.get_context().config.attributes['schema_dense_pages']
    dense.upgrade_dense_pages(op.get_bind(), op)


def downgrade():
    dense = op.get_context().config.attributes['schema_dense_pages']
    dense.downgrade_dense_pages(op.get_bind(), op)
