"""Experimental fax payload codec: recipient opt-in with its history, payload sends and decode results."""
from alembic import op

revision = '0031_fax_codec'
# Builder AG's branch chains after 0023; the lead rewires it after the other wave revisions at integration.
down_revision = '0023_negotiation'
branch_labels = None
depends_on = None


def upgrade():
    codec = op.get_context().config.attributes['schema_fax_codec']
    codec.upgrade_fax_codec(op.get_bind(), op)


def downgrade():
    codec = op.get_context().config.attributes['schema_fax_codec']
    codec.downgrade_fax_codec(op.get_bind(), op)
