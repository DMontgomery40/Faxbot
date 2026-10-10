"""What error correction and T.38 did on each fax call (brief 98, item 1)."""
from alembic import op
revision = '0080_engine_package'
down_revision = '0079_pots_quote'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_engine_package'].upgrade_engine_package(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_engine_package'].downgrade_engine_package(op.get_bind(), op)
