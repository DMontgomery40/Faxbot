"""Add owned work items for received documents, their history and the work permissions."""
from alembic import op

revision = '0011_work_items'
down_revision = '0010_inbound_imports'
branch_labels = None
depends_on = None


def upgrade():
    work = op.get_context().config.attributes['schema_work']
    work.upgrade_work(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
