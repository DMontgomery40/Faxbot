"""Shared transaction and reflection helpers for the 0008 delivery tables.

Writers serialize on one advisory lock (PostgreSQL) or an immediate write
transaction (SQLite). Runtime code reflects the migrated tables instead of
importing frozen migration metadata.
"""
from contextlib import contextmanager
from datetime import datetime, timezone

import sqlalchemy as sa


DELIVERY_LOCK_ID = 0x4641584456525953  # FAXDVRYS


class DeliveryStoreError(RuntimeError):
    """Sanitized storage failure; never includes SQL, values or URLs."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def reflect(engine, names):
    metadata = sa.MetaData()
    try:
        metadata.reflect(engine, only=list(names))
    except sa.exc.SQLAlchemyError:
        raise DeliveryStoreError('Delivery storage is unavailable.') from None
    return {name: metadata.tables[name] for name in names}


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
