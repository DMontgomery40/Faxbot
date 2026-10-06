"""Fax call negotiation: what each engine reported a call negotiated, with its scope in each column's name."""
from alembic import op

revision = '0023_negotiation'
# Builder V's 0022_history comes first at integration; the lead sets down_revision to it.
down_revision = '0022_history'
branch_labels = None
depends_on = None


def upgrade():
    negotiation = op.get_context().config.attributes['schema_negotiation']
    negotiation.upgrade_negotiation(op.get_bind(), op)


def downgrade():
    negotiation = op.get_context().config.attributes['schema_negotiation']
    negotiation.downgrade_negotiation(op.get_bind(), op)
