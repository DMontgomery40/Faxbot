"""Frozen 0054 engine extras; registration and validation belong to ``schema``.

Three engine additions (brief 68) need somewhere to keep what happened on each
call. This revision adds nullable columns only and changes no stored row:

- ``sip_call_records.subaddress``: the T.33 subaddress (SUB) a sent call asked
  the far end's machine for (a notice fax's notice ID, or one the fax's sending
  rules chose; patch 0005). It records the request: whether it was carried is
  read from the same attempt's far-end DIS (``fax_call_frames``, bit 49).
- ``sip_call_records.peer_id``: the enrolled partner a peer fax call went to,
  or came from, inside an encrypted tunnel with no carrier (M1b); NULL for every
  carrier call.
- ``inbound_rule_options.diverted_from`` and ``diversion_unsigned``: a receiving
  rule that matches a call forwarded from this number (X4). With
  ``diversion_unsigned`` NULL it matches only a verified forwarding; 1 also
  matches one that is not verified (never one whose signature failed).
- ``inbound_fax_routing.diverted_from`` and ``diversion``: the number a received
  call said it was forwarded from, and how far that was checked: ``signed``,
  ``unanchored``, ``unchecked``, ``failed`` or ``stated`` (headers only), as
  Faxbot read them when it placed the fax.

The downgrade drops the columns; it refuses while any of them holds a value,
because that is how calls went and how faxes were placed. Runtime code reflects
these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_digital_routes import frozen_metadata as previous_metadata


REVISION = '0054_engine_extras'
TABLES = frozenset()
SUBADDRESS_LENGTH = 20
DIVERSION_STATES = ('signed', 'unchecked', 'failed', 'stated')
ADDED_COLUMNS = (
    ('sip_call_records', 'subaddress', lambda: sa.String(SUBADDRESS_LENGTH)),
    ('sip_call_records', 'peer_id', lambda: sa.String(64)),
    ('inbound_rule_options', 'diverted_from', lambda: sa.String(32)),
    ('inbound_rule_options', 'diversion_unsigned', sa.Integer),
    ('inbound_fax_routing', 'diverted_from', lambda: sa.String(32)),
    ('inbound_fax_routing', 'diversion', lambda: sa.String(16)),
)


def _column(name, kind):
    return sa.Column(name, kind(), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table, column, kind in ADDED_COLUMNS:
        metadata.tables[table].append_column(_column(column, kind))
    return metadata


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_engine_extras(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    for table, column, _ in ADDED_COLUMNS:
        if not inspector.has_table(table) or column in {item['name'] for item in inspector.get_columns(table)}:
            _refuse('Call record and receiving rule tables are not in the expected state for their migration.')
    for table, column, kind in ADDED_COLUMNS:
        operations.add_column(table, _column(column, kind))


def downgrade_engine_extras(connection, operations):
    # How calls went and how received faxes were placed is evidence; never drop it silently.
    for table, column, _ in ADDED_COLUMNS:
        values = sa.table(table, sa.column(column))
        if connection.execute(sa.select(sa.func.count()).select_from(values)
                              .where(values.c[column].is_not(None))).scalar():
            _refuse('Calls or received faxes record a subaddress, a partner call or a forwarded call; this revision '
                    'cannot be undone without losing them.')
    # No constraint or index names these columns, so SQLite drops them too.
    for table, column, _ in reversed(ADDED_COLUMNS):
        operations.drop_column(table, column)
