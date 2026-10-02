"""Add effective backends and normalize missing historical indexes additively."""
from alembic import op

revision = "0002_schema_foundation"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    legacy = op.get_context().config.attributes["schema_legacy"]
    legacy.normalize_foundation(op.get_bind(), op)


def downgrade():
    raise RuntimeError("Destructive schema downgrade is not supported; restore a verified backup.")
