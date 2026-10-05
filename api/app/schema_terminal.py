"""Frozen 0018: the Terminal belongs to the Owner role; registration and validation belong to ``schema``.

The console Terminal is a shell inside Faxbot's container. Until this revision the
built-in Host Operator role held ``host:terminal`` along with restarting Faxbot; from
here only the Owner role holds it by default, and an owner can still give it to
someone on purpose through a role of their own. The access store checks the
built-in roles against its catalogue at startup, so the change is a revision.

It deletes one built-in role-permission row, (role_host_operator, host:terminal).
That is a role-policy change, not history: no table, permission, assignment, key
grant, policy version or audit row changes, and the revision number itself records
it. Downgrading puts the row back with the same identity. Runtime code reflects
these tables; it never imports this metadata.
"""
import sqlalchemy as sa

from .schema_access import _identity
from .schema_fax_engine import frozen_metadata as previous_metadata


REVISION = '0018_terminal_owner_only'
# This revision adds no table.
TABLES = frozenset()
ROLE = 'role_host_operator'
PERMISSION = 'host:terminal'
ROW_ID = _identity('role_permission', ROLE, PERMISSION)


def frozen_metadata(*, dialect='sqlite'):
    return previous_metadata(dialect=dialect)


def _members(connection):
    return frozen_metadata(dialect=connection.dialect.name).tables['access_role_permissions']


def _held(connection, members):
    return connection.execute(sa.select(members.c.id).where(
        members.c.role_id == ROLE, members.c.permission_id == PERMISSION)).scalars().all()


def upgrade_terminal_owner_only(connection, operations):
    # The guarded migration owns transaction and namespace validation. Refuse a role
    # that is not as the earlier revision left it before changing anything.
    from .schema import SchemaUpgradeError
    members = _members(connection)
    if _held(connection, members) != [ROW_ID]:
        raise SchemaUpgradeError('The Host Operator role is not as the earlier revision left it.')
    connection.execute(members.delete().where(members.c.id == ROW_ID))


def downgrade_terminal_owner_only(connection, operations):
    from .schema import SchemaUpgradeError
    members = _members(connection)
    if _held(connection, members):
        raise SchemaUpgradeError('The Host Operator role already holds the terminal.')
    connection.execute(members.insert().values(id=ROW_ID, role_id=ROLE, permission_id=PERMISSION))
