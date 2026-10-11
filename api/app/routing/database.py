"""Shared transaction and reflection helpers for the 0008 delivery tables.

Writers serialize on one advisory lock (PostgreSQL) or an immediate write
transaction (SQLite). Runtime code reflects the migrated tables instead of
importing frozen migration metadata, once per database (``reflect``).
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import threading
import weakref

import sqlalchemy as sa


DELIVERY_LOCK_ID = 0x4641584456525953  # FAXDVRYS


class DeliveryStoreError(RuntimeError):
    """Sanitized storage failure; never includes SQL, values or URLs."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# {engine: (schema stamp, {key: what was built})}. Reflecting runs dozens of catalogue queries per table, and the
# stores and readers that call ``reflect`` are built on every request (one Capabilities read reflected about 90
# times), so each database's tables are reflected once per process and kept until its schema changes. Weak keys: a
# disposed installation engine (tests, a moved database) takes what was kept for it along.
_KEPT = weakref.WeakKeyDictionary()
_KEPT_LOCK = threading.Lock()
_NOTHING = object()


def _schema_stamp(engine):
    """A value that changes whenever the database's schema does, read with one small query.

    SQLite bumps ``schema_version`` on every CREATE, ALTER and DROP. On PostgreSQL every schema change Faxbot makes
    is a migration, and each migration rewrites ``alembic_version``; a database without that table (a test's bare
    tables) has no stamp, so what was kept for it stays until its engine goes.
    """
    with engine.connect() as connection:
        if connection.dialect.name == 'sqlite':
            return connection.exec_driver_sql('PRAGMA schema_version').scalar()
        try:
            return tuple(connection.exec_driver_sql('SELECT version_num FROM alembic_version').scalars())
        except sa.exc.ProgrammingError:
            return None


def once(engine, key, build):
    """``build()``, run once per engine and ``key`` and kept until the database's schema changes.

    Every call reads the schema stamp first (``_schema_stamp``), so a migration run while Faxbot serves, or by
    another process, is seen on the next call. Only what ``build`` returns is kept: when it raises, the next call
    builds again. ``build`` runs outside the lock, so a slow first build never holds up other readers; two threads
    may both build once, and the first one kept is what every caller gets. What is kept is shared between threads,
    so it must hold no per-request state: reflected Tables, or a store whose only state is its Tables.
    """
    stamp = _schema_stamp(engine)
    with _KEPT_LOCK:
        kept = _KEPT.get(engine)
        if kept is None or kept[0] != stamp:
            kept = _KEPT[engine] = (stamp, {})
        found = kept[1].get(key, _NOTHING)
    if found is _NOTHING:
        found = build()
        with _KEPT_LOCK:
            found = kept[1].setdefault(key, found)
    return found


def _reflected(bind, names):
    metadata = sa.MetaData()
    metadata.reflect(bind, only=list(names))
    return {name: metadata.tables[name] for name in names}


def reflect(engine, names):
    """{name: Table} for the migrated tables ``names``; DeliveryStoreError when any is missing or storage fails.

    Reflected once per engine and ``names`` (``once``), so a table not migrated yet raises every time, and again
    after any schema change. Callers only build statements from the Tables and never change them.
    """
    names = tuple(names)
    try:
        if not isinstance(engine, sa.engine.Engine):
            # A connection may be inside a migration's transaction, whose schema is not settled yet.
            return _reflected(engine, names)
        found = once(engine, ('reflect', names), lambda: _reflected(engine, names))
    except sa.exc.SQLAlchemyError:
        raise DeliveryStoreError('Delivery storage is unavailable.') from None
    return dict(found)


@contextmanager
def write_transaction(engine):
    """One serialized short write transaction; commit failures surface as errors."""
    try:
        with engine.connect() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            else:
                connection.begin()
                connection.execute(sa.text("SELECT set_config('lock_timeout', '10000ms', true)"))
                connection.execute(sa.text('SELECT pg_advisory_xact_lock(:key)'), {'key': DELIVERY_LOCK_ID})
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
    except sa.exc.SQLAlchemyError:
        raise DeliveryStoreError('Delivery storage could not complete the change.') from None


@contextmanager
def read_connection(engine):
    try:
        with engine.connect() as connection:
            yield connection
    except sa.exc.SQLAlchemyError:
        raise DeliveryStoreError('Delivery storage is unavailable.') from None
