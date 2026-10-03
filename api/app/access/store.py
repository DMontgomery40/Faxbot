"""Reflected current access state and transaction-scoped installation locking."""
from contextlib import contextmanager
import re
from threading import Lock

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from .catalog import BUILTIN_ROLE_PERMISSIONS, CATALOGUE_VERSION, PERMISSIONS
from .types import AccessError, AccessUnavailableError, InvalidTransactionError


_MARKERS = object()
_LISTENER_LOCK = Lock()


def _invalidate_markers(connection):
    connection.info.pop(_MARKERS, None)


def _invalidate_sql_boundary(connection, cursor, statement, parameters, context, executemany):
    # Raw SQL transaction control does not update RootTransaction. Shared
    # callbacks capture no store and occur once per engine, even across stores.
    # PostgreSQL accepts multiple control statements in one driver call. Inspect
    # every segment conservatively; a false positive only requires a fresh lock.
    for segment in statement.split(';'):
        prefix = re.sub(r'\A(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*', '', segment, flags=re.S)
        command = re.match(r'[A-Za-z]+', prefix)
        if command and command.group().upper() in {'BEGIN', 'COMMIT', 'ROLLBACK', 'ABORT', 'END'}:
            _invalidate_markers(connection)
            break


class AccessStore:
    LOCK_TIMEOUT_MS = 1000
    TABLE_NAMES = (
        "access_state", "access_principals", "access_users", "access_groups",
        "access_memberships", "access_permissions", "access_roles", "access_role_permissions",
        "access_resources", "access_assignments", "access_key_bindings", "access_key_grants",
        "access_sessions", "access_audit", "api_keys", "mailboxes", "fax_jobs", "inbound_faxes",
        "inbound_rules", "access_mailbox_routes",
    )

    def __init__(self, engine: Engine):
        self.engine = engine
        self._marker_key = object()
        if engine.dialect.name not in {"sqlite", "postgresql"}:
            raise AccessUnavailableError()
        with _LISTENER_LOCK:
            for event, listener in (('begin', _invalidate_markers), ('commit', _invalidate_markers),
                                    ('rollback', _invalidate_markers),
                                    ('before_cursor_execute', _invalidate_sql_boundary)):
                if not sa.event.contains(engine, event, listener):
                    sa.event.listen(engine, event, listener)
        try:
            metadata = sa.MetaData()
            metadata.reflect(engine, only=self.TABLE_NAMES)
            self.tables = {name: metadata.tables[name] for name in self.TABLE_NAMES}
            with self.transaction() as connection:
                self._validate_catalogue_on(connection)
        except AccessError:
            raise
        except (sa.exc.SQLAlchemyError, KeyError):
            raise AccessUnavailableError() from None

    def _validate_catalogue_on(self, connection):
        state = self.tables["access_state"]
        if connection.execute(sa.select(state.c.catalogue_version)).scalars().all() != [CATALOGUE_VERSION]:
            raise AccessUnavailableError()
        permissions = self.tables["access_permissions"]
        if frozenset(connection.execute(sa.select(permissions.c.id)).scalars()) != PERMISSIONS:
            raise AccessUnavailableError()
        roles, members = self.tables["access_roles"], self.tables["access_role_permissions"]
        builtin_ids = set(connection.execute(sa.select(roles.c.id).where(roles.c.kind == "builtin")).scalars())
        if builtin_ids != set(BUILTIN_ROLE_PERMISSIONS):
            raise AccessUnavailableError()
        for role_id, expected in BUILTIN_ROLE_PERMISSIONS.items():
            actual = frozenset(connection.execute(sa.select(members.c.permission_id).where(members.c.role_id == role_id)).scalars())
            if actual != expected:
                raise AccessUnavailableError()

    def _transaction_on(self, connection):
        if (not isinstance(connection, Connection) or connection.closed or connection.engine is not self.engine
                or connection.get_nested_transaction() is not None):
            raise InvalidTransactionError()
        transaction = connection.get_transaction()
        if transaction is None or not transaction.is_active:
            raise InvalidTransactionError()
        return transaction

    @staticmethod
    def _physical_transaction_on(connection):
        driver = connection.connection.driver_connection
        if connection.dialect.name == 'sqlite':
            active = driver.in_transaction
        else:
            # SQLAlchemy's RootTransaction can remain active after raw SQL
            # ROLLBACK. Both supported psycopg drivers expose server status.
            if hasattr(driver, 'get_transaction_status'):
                active = driver.get_transaction_status() == 2
            else:
                status = getattr(getattr(driver, 'info', None), 'transaction_status', None)
                active = status is not None and int(status) == 2
        if not active:
            raise InvalidTransactionError()

    def lock_on(self, connection: Connection) -> int:
        transaction = self._transaction_on(connection)
        try:
            dialect = connection.dialect.name
            state = self.tables["access_state"]
            if dialect == "sqlite":
                if connection.exec_driver_sql("PRAGMA read_uncommitted").scalar_one() != 0:
                    raise InvalidTransactionError()
                if connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
                    raise InvalidTransactionError()
                connection.exec_driver_sql(f"PRAGMA busy_timeout = {self.LOCK_TIMEOUT_MS}")
                # Existing caller transactions can be deferred; this acquires the
                # same reserved write lock as standalone BEGIN IMMEDIATE.
                connection.execute(state.update().where(state.c.id == "state").values(policy_version=state.c.policy_version))
                query = sa.select(state.c.policy_version).where(state.c.id == "state")
            elif dialect == "postgresql":
                if connection.get_isolation_level() != "READ COMMITTED":
                    raise InvalidTransactionError()
                connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{self.LOCK_TIMEOUT_MS}ms'")
                query = sa.select(state.c.policy_version).where(state.c.id == "state").with_for_update()
            else:
                raise InvalidTransactionError()
            version = connection.execute(query).scalar_one()
            self._physical_transaction_on(connection)
            connection.info.setdefault(_MARKERS, {})[self._marker_key] = transaction
            return version
        except AccessError:
            raise
        except sa.exc.SQLAlchemyError:
            raise AccessUnavailableError() from None

    def require_lock_on(self, connection: Connection) -> int:
        transaction = self._transaction_on(connection)
        if connection.info.get(_MARKERS, {}).get(self._marker_key) is not transaction:
            raise InvalidTransactionError()
        self._physical_transaction_on(connection)
        try:
            state = self.tables["access_state"]
            return connection.execute(sa.select(state.c.policy_version).where(state.c.id == "state")).scalar_one()
        except sa.exc.SQLAlchemyError:
            raise AccessUnavailableError() from None

    @contextmanager
    def transaction(self):
        try:
            with self.engine.connect() as connection:
                if connection.dialect.name == "postgresql":
                    connection = connection.execution_options(isolation_level="READ COMMITTED")
                if connection.dialect.name == "sqlite":
                    connection.exec_driver_sql(f"PRAGMA busy_timeout = {self.LOCK_TIMEOUT_MS}")
                    connection.commit()
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    connection.begin()
                try:
                    self.lock_on(connection)
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except sa.exc.SQLAlchemyError:
            raise AccessUnavailableError() from None
