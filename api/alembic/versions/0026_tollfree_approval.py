"""Toll-free approvals: who at a recipient agreed to faxes on its toll-free number, when, and the evidence."""
from alembic import op

revision = '0026_tollfree_approval'
# The lead sets down_revision to the highest revision at integration.
down_revision = '0025_shared_manifest'
branch_labels = None
depends_on = None


def upgrade():
    tollfree = op.get_context().config.attributes['schema_tollfree']
    tollfree.upgrade_tollfree(op.get_bind(), op)


def downgrade():
    tollfree = op.get_context().config.attributes['schema_tollfree']
    tollfree.downgrade_tollfree(op.get_bind(), op)
