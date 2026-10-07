"""Frozen 0034 peer fax; registration and validation belong to ``schema``.

A fax to an enrolled Faxbot partner can go as the exact fax image the fax
engine would send, over the encrypted direct channel. This revision adds
nullable columns only, and touches no received-fax (inbound) table:

- ``direct_peers``, named by direction. What this installation decides for a
  partner: ``receive_fax_images`` (whether it accepts fax images from the
  partner: on by default, so NULL means on, 1 on and 0 turned off; every
  partner enrolled before this revision reads NULL and so is on, with no row
  rewritten) and ``receive_peer_calls`` (1 when it accepts a peer fax call from
  the partner; for later; NULL means no). ``told_partner_at``: when the partner
  last recorded our signed statement of those choices, or answered as a Faxbot
  without fax images; NULL means it still has to be told, which is every
  partner after this upgrade. What the partner said in a statement it signed
  (NULL means no): ``partner_receives_fax_images``, ``partner_peer_calls`` (it
  can take a peer fax call with IAF over a tunnel; for later) and
  ``partner_said_at`` (the signed time of its latest statement, so an older
  statement never overrides a newer one). ``peer_call_address`` is the
  partner's address inside the tunnel a peer fax call must use.
- ``direct_deliveries.kind``: NULL for an original document (every row before
  this revision), ``fax_image`` for a fax image.

Direct arrivals are filed as received faxes with the existing ``local`` import
source (Faxbot itself delivered them, with no provider) and an account of
``direct:`` plus the partner's enrollment id, so ``inbound_imports`` keeps its
0020 constraint and is never rebuilt here.

The downgrade drops the columns; it refuses while any fax image is recorded,
because dropping ``kind`` would lose which documents were fax images. Runtime
code reflects these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_negotiation import frozen_metadata as previous_metadata


REVISION = '0034_peer_fax'
TABLES = frozenset()
PEER_COLUMNS = (
    ('receive_fax_images', sa.Integer),
    ('receive_peer_calls', sa.Integer),
    ('partner_receives_fax_images', sa.Integer),
    ('partner_peer_calls', sa.Integer),
    ('partner_said_at', sa.DateTime),
    ('peer_call_address', lambda: sa.String(255)),
    ('told_partner_at', sa.DateTime),
)
DELIVERY_COLUMNS = (('kind', lambda: sa.String(16)),)


def _columns():
    return (('direct_peers', PEER_COLUMNS), ('direct_deliveries', DELIVERY_COLUMNS))


def _column(name, kind):
    return sa.Column(name, kind(), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    for table_name, columns in _columns():
        for name, kind in columns:
            metadata.tables[table_name].append_column(_column(name, kind))
    return metadata


def _refuse(message):
    from .schema import SchemaUpgradeError
    raise SchemaUpgradeError(message)


def upgrade_peer_fax(connection, operations):
    # The guarded migration owns the transaction; refuse any unexpected shape before DDL.
    inspector = sa.inspect(connection)
    for table_name, columns in _columns():
        present = ({column['name'] for column in inspector.get_columns(table_name)}
                   if inspector.has_table(table_name) else None)
        if present is None or any(name in present for name, _ in columns):
            _refuse('Direct delivery tables are not in the expected state for their migration.')
    for table_name, columns in _columns():
        for name, kind in columns:
            operations.add_column(table_name, _column(name, kind))


def downgrade_peer_fax(connection, operations):
    deliveries = sa.table('direct_deliveries', sa.column('kind'))
    if connection.execute(sa.select(sa.func.count()).select_from(deliveries)
                          .where(deliveries.c.kind.is_not(None))).scalar():
        _refuse('Fax images sent or received directly are recorded; this revision cannot be undone without losing '
                'which documents were fax images.')
    # No constraint or index names these columns, so SQLite drops them too.
    for table_name, columns in reversed(_columns()):
        for name, _ in reversed(columns):
            operations.drop_column(table_name, name)
