"""Case ledger: originals, sources, versions and purposes, recipient acknowledgements, repairs and checklists."""
from alembic import op

revision = '0037_case_ledger'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0036_engine_frames'
branch_labels = None
depends_on = None


def upgrade():
    case_ledger = op.get_context().config.attributes['schema_case_ledger']
    case_ledger.upgrade_case_ledger(op.get_bind(), op)


def downgrade():
    case_ledger = op.get_context().config.attributes['schema_case_ledger']
    case_ledger.downgrade_case_ledger(op.get_bind(), op)
