"""Frozen 0082 partner layouts: which encoded pages a partner's Faxbot reads, and what you last told it.

Registration and validation belong to ``schema``. A partner's Faxbot announces, in the capabilities it signs, which
encoded-page variants its decoder reads (``payload_layouts``, ``codec/variants.py``); the encoded-page chooser then
sends that partner only those. Releases before the tolerant capability parser refuse a statement that carries a key
they do not know, so this installation sends the key only to a partner whose own statement carried it, and learns a
partner's from the signed answer to its own statement. This revision adds two nullable columns to ``direct_peers``
and changes no stored row:

- ``partner_payload_layouts``: what the partner's newest signed statement announced, as a JSON list of variant
  tokens; NULL when its newest statement did not say (an older partner), which leaves it the variants every Faxbot
  decodes. It follows ``partner_said_at``: an older statement never overrides a newer one.
- ``told_payload_layouts``: what this installation last told the partner it reads, as the same JSON list, or an
  empty string when the statement went without the key (the partner had not announced it); NULL means this release
  has not told the partner yet, so every partner is told once after the upgrade, and again whenever the variants
  this installation reads change.

The downgrade drops the columns. Runtime code reflects this table; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_pots_quote import frozen_metadata as previous_metadata


REVISION = '0082_partner_layouts'
TABLES = frozenset()
# Up to 32 tokens of at most 28 characters each, as JSON (``direct/crypto.py``, ``MAX_VARIANTS``).
LENGTH = 1024
COLUMNS = (
    ('direct_peers', 'partner_payload_layouts'),
    ('direct_peers', 'told_payload_layouts'),
)


def _column(name):
    return sa.Column(name, sa.String(LENGTH), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, name in COLUMNS:
        metadata.tables[table].append_column(_column(name))
    return metadata


def upgrade_partner_layouts(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    for table, name in COLUMNS:
        if not inspector.has_table(table) or name in {column['name'] for column in inspector.get_columns(table)}:
            from .schema import SchemaUpgradeError
            raise SchemaUpgradeError('The partner table is not in the expected state for its migration.')
    for table, name in COLUMNS:
        operations.add_column(table, _column(name))


def downgrade_partner_layouts(connection, operations):
    # No constraint or index names these columns, so SQLite drops them too.
    for table, name in reversed(COLUMNS):
        operations.drop_column(table, name)
