"""Give the Terminal to the Owner role only: the built-in Host Operator role no longer holds host:terminal."""
from alembic import op

revision = '0018_terminal_owner_only'
down_revision = '0017_fax_engine'
branch_labels = None
depends_on = None


def upgrade():
    terminal = op.get_context().config.attributes['schema_terminal']
    terminal.upgrade_terminal_owner_only(op.get_bind(), op)


def downgrade():
    # Only this revision's one row comes back; earlier revisions still refuse a downgrade.
    terminal = op.get_context().config.attributes['schema_terminal']
    terminal.downgrade_terminal_owner_only(op.get_bind(), op)
