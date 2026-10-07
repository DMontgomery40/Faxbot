"""The number each sent fax attempt dialed, and the approved alternate captured when a fax is accepted."""
from alembic import op

revision = '0027_dialed_number'
# Builders' 0024 to 0026 come first at integration; the lead sets down_revision to the last of them.
down_revision = '0023_negotiation'
branch_labels = None
depends_on = None


def upgrade():
    dialed = op.get_context().config.attributes['schema_dialed']
    dialed.upgrade_dialed(op.get_bind(), op)


def downgrade():
    dialed = op.get_context().config.attributes['schema_dialed']
    dialed.downgrade_dialed(op.get_bind(), op)
