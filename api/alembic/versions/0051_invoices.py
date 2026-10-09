"""Invoices and provider charge reads: invoice totals, received-fax charges and faxes Faxbot has no record of."""
from alembic import op

revision = '0051_invoices'
# Chained after the integration head when merged (revision numbers follow merge order, not number order).
down_revision = '0033_trunks_sites'
branch_labels = None
depends_on = None


def upgrade():
    invoices = op.get_context().config.attributes['schema_invoices']
    invoices.upgrade_invoices(op.get_bind(), op)


def downgrade():
    # Refused while any invoice is recorded; otherwise the tables go.
    invoices = op.get_context().config.attributes['schema_invoices']
    invoices.downgrade_invoices(op.get_bind(), op)
