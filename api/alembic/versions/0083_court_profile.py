"""Where the California court send profile applies, and each fax sent under it."""
from alembic import op
revision = '0083_court_profile'
down_revision = '0082_partner_layouts'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_court_profile'].upgrade_court_profile(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_court_profile'].downgrade_court_profile(op.get_bind(), op)
