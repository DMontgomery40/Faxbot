"""Engine extras: the subaddress a call asked for, peer fax calls, and diverted calls for receiving rules."""
from alembic import op

revision = '0054_engine_extras'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0033_trunks_sites'
branch_labels = None
depends_on = None


def upgrade():
    extras = op.get_context().config.attributes['schema_engine_extras']
    extras.upgrade_engine_extras(op.get_bind(), op)


def downgrade():
    # Refused while any call or received fax records one of these values.
    extras = op.get_context().config.attributes['schema_engine_extras']
    extras.downgrade_engine_extras(op.get_bind(), op)
