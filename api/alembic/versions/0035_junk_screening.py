"""Junk screening: callers an administrator marked as junk, and every call Asterisk turned away before answering."""
from alembic import op

revision = '0035_junk_screening'
# Builder AK's branch; the lead re-chains it after the other wave revisions at integration.
down_revision = '0023_negotiation'
branch_labels = None
depends_on = None


def upgrade():
    screening = op.get_context().config.attributes['schema_screening']
    screening.upgrade_screening(op.get_bind(), op)


def downgrade():
    screening = op.get_context().config.attributes['schema_screening']
    screening.downgrade_screening(op.get_bind(), op)
