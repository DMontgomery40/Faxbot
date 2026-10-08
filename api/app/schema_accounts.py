"""Frozen 0043 accounts; registration and validation belong to ``schema``.

Receiving rules can route by the subaddress the sender stated (ITU-T T.33, the
T.30 SUB frame): one fax number plus a subaddress per department reaches the
right mailbox. A subaddress is what the sending machine says, never proof of
who sent the fax, so it chooses a mailbox and never grants access. This
revision adds two nullable columns and changes no stored row:

- ``inbound_rule_options.subaddress``: the subaddress a receiving rule matches,
  exactly, after the same normalizing as a received one (T.30's numeric
  characters, at most 20). NULL matches any fax, as before.
- ``inbound_fax_routing.subaddress``: the subaddress the sender stated on that
  fax, as Faxbot read it when the fax was placed; NULL when it stated none or
  the fax did not come over a fax call.

Extra provider accounts themselves live in the encrypted configuration
revision (``accounts.py``), so they need no table. The downgrade drops both
columns; it refuses while either holds a value, because that is how faxes were
placed. Runtime code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_intake_sources import frozen_metadata as previous_metadata


REVISION = '0043_accounts'
TABLES = frozenset()
SUBADDRESS_LENGTH = 20
ADDED_COLUMNS = (
    ('inbound_rule_options', 'subaddress'),
    ('inbound_fax_routing', 'subaddress'),
)


def _column(name):
    return sa.Column(name, sa.String(SUBADDRESS_LENGTH), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, column in ADDED_COLUMNS:
        metadata.tables[table].append_column(_column(column))
    return metadata


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_accounts(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    for table, column in ADDED_COLUMNS:
        if not inspector.has_table(table) or column in {item['name'] for item in inspector.get_columns(table)}:
            _refuse('Receiving rule tables are not in the expected state for their migration.')
    for table, column in ADDED_COLUMNS:
        operations.add_column(table, _column(column))


def downgrade_accounts(connection, operations):
    # How each received fax was placed is evidence; never drop it silently.
    for table, column in ADDED_COLUMNS:
        values = sa.table(table, sa.column(column))
        if connection.execute(sa.select(sa.func.count()).select_from(values)
                              .where(values.c[column].is_not(None))).scalar():
            _refuse('Receiving rules or received faxes record a subaddress; this revision cannot be undone without '
                    'losing them.')
    # No constraint or index names these columns, so SQLite drops them too.
    for table, column in reversed(ADDED_COLUMNS):
        operations.drop_column(table, column)
