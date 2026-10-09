"""Persist opt-in operational analysis and singleton scheduling."""
from alembic import op
revision = '0066_analysis'
down_revision = '0056_measured_codec'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_analysis'].upgrade_analysis(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_analysis'].downgrade_analysis(op.get_bind(), op)
