"""Engine frames: what the far end's fax machine said on each built-in engine call, and approved IAF fax servers."""
from alembic import op

revision = '0036_engine_frames'
# Builder AK's branch: after 0035 here; the lead re-chains both after the other wave revisions at integration.
down_revision = '0035_junk_screening'
branch_labels = None
depends_on = None


def upgrade():
    frames = op.get_context().config.attributes['schema_engine_frames']
    frames.upgrade_engine_frames(op.get_bind(), op)


def downgrade():
    frames = op.get_context().config.attributes['schema_engine_frames']
    frames.downgrade_engine_frames(op.get_bind(), op)
