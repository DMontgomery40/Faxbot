"""Which payload formats a recipient's decoder reads: the capacity layout only for a decoder that reads it."""
from alembic import op
revision = '0071_codec_decoder'
down_revision = '0070_closures'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_codec_decoder'].upgrade_codec_decoder(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_codec_decoder'].downgrade_codec_decoder(op.get_bind(), op)
