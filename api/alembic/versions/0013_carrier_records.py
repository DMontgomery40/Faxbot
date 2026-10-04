"""Add carrier billing records that match no call Faxbot recorded."""
from alembic import op

revision = '0013_carrier_records'
down_revision = '0012_carrier_charges'
branch_labels = None
depends_on = None


def upgrade():
    records = op.get_context().config.attributes['schema_records']
    records.upgrade_records(op.get_bind(), op)


def downgrade():
    raise RuntimeError('Destructive schema downgrade is not supported; restore a verified backup.')
