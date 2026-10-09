"""Frozen 0032 rules in delivery; registration and validation belong to ``schema``.

Sending rules can hold a fax for approval (``outbound_holds``, 0029). This
revision adds the permission that decides those holds, and the one fact the
strict fallback of a rule-chosen route needs about each attempt:

- ``fax:approve`` ("Approve faxes"): a new installation permission. The built-in
  Owner and Administrator roles hold it, as they hold every other installation
  permission an owner does not keep for themselves. The access store checks the
  built-in roles against its catalogue at startup, so the change is a revision.
  Upgrading inserts the permission row and the two built-in role rows; no
  policy version, assignment or audit row changes, as in 0018.
- ``outbound_attempts.ended_before_data``: 1 when the provider or engine said
  the call ended before any fax data (busy, no answer, no fax machine, refused
  when the fax was handed over), 0 when it said data was exchanged, NULL when
  it did not say. A fax whose route a rule chose moves to the next account only
  after an attempt with 1 (owner's answer Q3); today's fallback for faxes no
  rule decides does not read it.

The downgrade removes both. It refuses while a role the owner made, or an
integration key limit, names ``fax:approve``, because removing it would quietly
change what that role or key may do. Runtime code reflects these tables; it
never imports this metadata.
"""
import sqlalchemy as sa

from .schema_access import _identity
from .schema_accounts import frozen_metadata as previous_metadata


REVISION = '0032_rules_delivery'
# This revision adds no table.
TABLES = frozenset()
# Literals belong to this revision, never the mutable runtime catalogue.
PERMISSION = 'fax:approve'
DESCRIPTION = 'Approve faxes held by sending rules'
ROLES = ('role_owner', 'role_administrator')
ROW_IDS = frozenset(_identity('role_permission', role, PERMISSION) for role in ROLES)
COLUMN = ('outbound_attempts', 'ended_before_data')


def _column():
    # Nullable and additive, like fax_jobs.urgent in 0021; the delivery store writes only 0, 1 or NULL.
    return sa.Column(COLUMN[1], sa.Integer(), nullable=True)


def frozen_metadata(*, dialect='sqlite'):
    metadata = previous_metadata(dialect=dialect)
    metadata.tables[COLUMN[0]].append_column(_column())
    return metadata


def _tables(connection):
    tables = previous_metadata(dialect=connection.dialect.name).tables
    return (tables['access_permissions'], tables['access_role_permissions'], tables['access_roles'],
            tables['access_key_grants'])


def upgrade_rules_delivery(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a catalogue or table that is not as
    # the earlier revision left it before changing anything.
    from .schema import SchemaUpgradeError
    permissions, members, _, _ = _tables(connection)
    inspector = sa.inspect(connection)
    table, column = COLUMN
    if not inspector.has_table(table) or column in {item['name'] for item in inspector.get_columns(table)}:
        raise SchemaUpgradeError('Delivery attempts are not in the expected state for their migration.')
    if connection.execute(sa.select(permissions.c.id).where(permissions.c.id == PERMISSION)).first() or \
            connection.execute(sa.select(members.c.id).where(members.c.permission_id == PERMISSION)).first():
        raise SchemaUpgradeError('The access catalogue is not as the earlier revision left it.')
    connection.execute(permissions.insert().values(id=PERMISSION, description=DESCRIPTION))
    for role in ROLES:
        connection.execute(members.insert().values(id=_identity('role_permission', role, PERMISSION), role_id=role,
                                                   permission_id=PERMISSION))
    operations.add_column(table, _column())


def downgrade_rules_delivery(connection, operations):
    from .schema import SchemaUpgradeError
    permissions, members, roles, grants = _tables(connection)
    custom = connection.execute(sa.select(members.c.id).join(roles, roles.c.id == members.c.role_id).where(
        members.c.permission_id == PERMISSION, roles.c.kind != 'builtin')).first()
    limited = connection.execute(sa.select(grants.c.id).where(grants.c.permission_id == PERMISSION)).first()
    if custom or limited:
        raise SchemaUpgradeError('A role or integration key you made holds Approve faxes; remove it there before '
                                 'undoing this revision.')
    connection.execute(members.delete().where(members.c.permission_id == PERMISSION))
    connection.execute(permissions.delete().where(permissions.c.id == PERMISSION))
    operations.drop_column(*COLUMN)
