"""Initial schema and validated additive adoption of pre-Alembic databases.

Revision identity is retained for installations already stamped 0001_initial.
"""
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # env.py supplies the same package in source and flattened runtime images.
    legacy = op.get_context().config.attributes["schema_legacy"]
    legacy.adopt_initial(op.get_bind(), op)


def downgrade():
    raise RuntimeError("Destructive schema downgrade is not supported; restore a verified backup.")
