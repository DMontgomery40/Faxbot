"""History: import failures, eFax deletion retries, email recipients and never-priced carrier records."""
from alembic import op

revision = '0022_history'
down_revision = '0021_capacity'
branch_labels = None
depends_on = None


def upgrade():
    history = op.get_context().config.attributes['schema_history']
    history.upgrade_history(op.get_bind(), op)


def downgrade():
    history = op.get_context().config.attributes['schema_history']
    history.downgrade_history(op.get_bind(), op)
