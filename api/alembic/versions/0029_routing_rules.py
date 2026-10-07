"""Sending rules per scope: published revisions, drafts, each fax's routing decision, attempt choices and holds."""
from alembic import op

revision = '0029_routing_rules'
# Wave A's 0024-0028 come first at integration; the lead sets down_revision to the last of them.
down_revision = '0023_negotiation'
branch_labels = None
depends_on = None


def upgrade():
    rules = op.get_context().config.attributes['schema_routing_rules']
    rules.upgrade_routing_rules(op.get_bind(), op)


def downgrade():
    # Refused while any revision, decision or hold is stored; otherwise the tables go.
    rules = op.get_context().config.attributes['schema_routing_rules']
    rules.downgrade_routing_rules(op.get_bind(), op)
