"""One index page for a shared call: the recipient's agreement per number, and each call's page layout."""
from alembic import op

revision = '0025_shared_manifest'
# 0024 comes first at integration; the lead sets down_revision to it.
down_revision = '0023_negotiation'
branch_labels = None
depends_on = None


def upgrade():
    manifest = op.get_context().config.attributes['schema_shared_manifest']
    manifest.upgrade_shared_manifest(op.get_bind(), op)


def downgrade():
    manifest = op.get_context().config.attributes['schema_shared_manifest']
    manifest.downgrade_shared_manifest(op.get_bind(), op)
