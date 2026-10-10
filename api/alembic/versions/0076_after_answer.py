"""Digits after answer: the keys Faxbot presses once a recipient's phone menu answers, and what each call pressed."""
from alembic import op

revision = '0076_after_answer'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0075_test_lines'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_after_answer'].upgrade_after_answer(op.get_bind(), op)


def downgrade():
    # Refused while any recipient setting or call record is kept; otherwise the tables go.
    op.get_context().config.attributes['schema_after_answer'].downgrade_after_answer(op.get_bind(), op)
