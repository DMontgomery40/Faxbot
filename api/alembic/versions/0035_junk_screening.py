"""Junk screening: callers an administrator marked as junk, and every call Asterisk turned away before answering."""
from alembic import op

revision = '0035_junk_screening'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0034_peer_fax'
branch_labels = None
depends_on = None


def upgrade():
    screening = op.get_context().config.attributes['schema_screening']
    screening.upgrade_screening(op.get_bind(), op)


def downgrade():
    screening = op.get_context().config.attributes['schema_screening']
    screening.downgrade_screening(op.get_bind(), op)
