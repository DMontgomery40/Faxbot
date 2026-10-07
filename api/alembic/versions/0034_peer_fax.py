"""Peer fax: fax images delivered directly to enrolled partners, and direct arrivals filed as received faxes."""
from alembic import op

revision = '0034_peer_fax'
# In this branch 0034 follows 0023; the lead sets down_revision to the highest revision at integration.
down_revision = '0023_negotiation'
branch_labels = None
depends_on = None


def upgrade():
    peer_fax = op.get_context().config.attributes['schema_peer_fax']
    peer_fax.upgrade_peer_fax(op.get_bind(), op)


def downgrade():
    # Refused while any direct arrival is filed as a received fax or any fax image is recorded.
    peer_fax = op.get_context().config.attributes['schema_peer_fax']
    peer_fax.downgrade_peer_fax(op.get_bind(), op)
