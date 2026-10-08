"""Trunks and sites: prices by where calls start (provider_rate_rows) for origin-rated quotes."""
from alembic import op

revision = '0033_trunks_sites'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0032_rules_delivery'
branch_labels = None
depends_on = None


def upgrade():
    trunks = op.get_context().config.attributes['schema_trunks_sites']
    trunks.upgrade_trunks_sites(op.get_bind(), op)


def downgrade():
    # Refused while a price you entered is saved.
    trunks = op.get_context().config.attributes['schema_trunks_sites']
    trunks.downgrade_trunks_sites(op.get_bind(), op)
