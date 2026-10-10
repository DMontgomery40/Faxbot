"""Prices by caller ID, caller-ID eligibility, registered-sender pins and requests for originals."""
from alembic import op
revision = '0069_countries'
down_revision = '0066_analysis'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_countries'].upgrade_countries(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_countries'].downgrade_countries(op.get_bind(), op)
