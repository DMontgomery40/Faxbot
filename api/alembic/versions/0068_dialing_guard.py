"""Where Faxbot may dial: classes of numbers, the countries already delivered to, and faxes the guard held."""
from alembic import op

revision = '0068_dialing_guard'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0066_analysis'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_dialing'].upgrade_dialing(op.get_bind(), op)


def downgrade():
    # Refused while an administrator's choice or a held fax is recorded; otherwise the tables go.
    op.get_context().config.attributes['schema_dialing'].downgrade_dialing(op.get_bind(), op)
