"""Line windows a partner grants, and what it accepts before a fax is paid for."""
from alembic import op
revision = '0084_partner_admission'
down_revision = '0083_court_profile'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_partner_admission'].upgrade_partner_admission(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_partner_admission'].downgrade_partner_admission(op.get_bind(), op)
