"""Registered forms: immutable form versions by content address, and the forms sent and received."""
from alembic import op

revision = '0038_registered_forms'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0037_case_ledger'
branch_labels = None
depends_on = None


def upgrade():
    forms = op.get_context().config.attributes['schema_forms']
    forms.upgrade_forms(op.get_bind(), op)


def downgrade():
    forms = op.get_context().config.attributes['schema_forms']
    forms.downgrade_forms(op.get_bind(), op)
