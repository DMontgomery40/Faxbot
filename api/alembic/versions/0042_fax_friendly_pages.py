"""Fax-friendly pages: one row each time light shading was left out of a fax's pages and specks removed."""
from alembic import op

revision = '0042_fax_friendly_pages'
# Later revisions of this wave come first at integration; the lead sets down_revision to the highest.
down_revision = '0028_dense_pages'
branch_labels = None
depends_on = None


def upgrade():
    friendly = op.get_context().config.attributes['schema_friendly_pages']
    friendly.upgrade_friendly_pages(op.get_bind(), op)


def downgrade():
    friendly = op.get_context().config.attributes['schema_friendly_pages']
    friendly.downgrade_friendly_pages(op.get_bind(), op)
