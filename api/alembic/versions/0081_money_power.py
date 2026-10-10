"""Dated exchange rates, NTT Data Connect, and faxes held while on battery that go by themselves."""
from alembic import op
revision = '0081_money_power'
down_revision = '0079_pots_quote'
branch_labels = None
depends_on = None


def upgrade():
    op.get_context().config.attributes['schema_money_power'].upgrade_money_power(op.get_bind(), op)


def downgrade():
    op.get_context().config.attributes['schema_money_power'].downgrade_money_power(op.get_bind(), op)
