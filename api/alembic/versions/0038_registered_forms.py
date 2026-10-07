"""Registered forms: immutable form versions by content address, and the forms sent and received."""
from alembic import op

revision = '0038_registered_forms'
# In this branch 0038 follows 0023; the lead sets down_revision to the highest revision at integration.
down_revision = '0023_negotiation'
branch_labels = None
depends_on = None


def upgrade():
    forms = op.get_context().config.attributes['schema_forms']
    forms.upgrade_forms(op.get_bind(), op)


def downgrade():
    forms = op.get_context().config.attributes['schema_forms']
    forms.downgrade_forms(op.get_bind(), op)
