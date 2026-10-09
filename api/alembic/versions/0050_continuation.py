"""Continuation: what each call confirmed about its pages, and the pages a person chose to send again."""
from alembic import op

revision = '0050_continuation'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0051_invoices'
branch_labels = None
depends_on = None


def upgrade():
    continuation = op.get_context().config.attributes['schema_continuation']
    continuation.upgrade_continuation(op.get_bind(), op)


def downgrade():
    # Refused while any continuation is recorded; otherwise the tables go.
    continuation = op.get_context().config.attributes['schema_continuation']
    continuation.downgrade_continuation(op.get_bind(), op)
