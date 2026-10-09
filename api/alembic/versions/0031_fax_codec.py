"""Experimental fax payload codec: recipient opt-in with its history, payload sends and decode results."""
from alembic import op

revision = '0031_fax_codec'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0028_dense_pages'
branch_labels = None
depends_on = None


def upgrade():
    codec = op.get_context().config.attributes['schema_fax_codec']
    codec.upgrade_fax_codec(op.get_bind(), op)


def downgrade():
    codec = op.get_context().config.attributes['schema_fax_codec']
    codec.downgrade_fax_codec(op.get_bind(), op)
