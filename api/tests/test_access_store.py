"""Current-policy transaction invariants on isolated real databases."""
import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.access.store import AccessStore
from api.app.access.types import InvalidTransactionError


def test_lock_is_bound_to_the_active_transaction(database):
    upgrade_schema(database)
    store = AccessStore(database)
    with database.connect() as connection:
        with pytest.raises(InvalidTransactionError):
            store.require_lock_on(connection)
        with connection.begin():
            assert store.lock_on(connection) == 1
            assert store.require_lock_on(connection) == 1
        with connection.begin():
            with pytest.raises(InvalidTransactionError):
                store.require_lock_on(connection)
    with store.transaction() as connection:
        assert store.require_lock_on(connection) == 1
        assert connection.execute(sa.text('SELECT policy_version FROM access_state')).scalar_one() == 1


def test_caller_transactions_require_lock_and_reject_nested_or_wrong_store(database):
    upgrade_schema(database)
    store = AccessStore(database)
    other = AccessStore(database)
    with database.begin() as connection:
        with pytest.raises(InvalidTransactionError):
            store.require_lock_on(connection)
        store.lock_on(connection)
        with pytest.raises(InvalidTransactionError):
            other.require_lock_on(connection)
        with connection.begin_nested():
            with pytest.raises(InvalidTransactionError):
                store.lock_on(connection)
    if database.dialect.name == 'postgresql':
        with database.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            with connection.begin(), pytest.raises(InvalidTransactionError):
                store.lock_on(connection)
    else:
        with database.connect().execution_options(isolation_level='READ UNCOMMITTED') as connection:
            with connection.begin(), pytest.raises(InvalidTransactionError):
                store.lock_on(connection)
        with database.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            with connection.begin(), pytest.raises(InvalidTransactionError):
                store.lock_on(connection)


def test_two_stores_serialize_and_lock_failure_is_bounded_and_safe(database):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from api.app.access.types import AccessUnavailableError
    upgrade_schema(database)
    holder = AccessStore(database)
    contender = AccessStore(database)
    contender.LOCK_TIMEOUT_MS = 150
    def take_lock():
        started = time.monotonic()
        with pytest.raises(AccessUnavailableError) as caught:
            with contender.transaction():
                pass
        assert str(caught.value) == 'access_unavailable'
        return time.monotonic() - started
    with ThreadPoolExecutor(max_workers=1) as executor:
        with holder.transaction():
            elapsed = executor.submit(take_lock).result(timeout=3)
            assert 0.1 <= elapsed < 3
    with contender.transaction() as connection:
        assert contender.require_lock_on(connection) == 1


@pytest.mark.parametrize('damage', ['permission', 'builtin_member', 'builtin_kind', 'catalogue_version'])
def test_invalid_catalogue_is_rejected_at_store_initialization(database, damage):
    from api.app.access.types import AccessUnavailableError
    upgrade_schema(database)
    with database.begin() as connection:
        if damage == 'permission':
            connection.execute(sa.text("INSERT INTO access_permissions (id,description) VALUES ('unknown','unknown')"))
        elif damage == 'builtin_member':
            connection.execute(sa.text("DELETE FROM access_role_permissions WHERE role_id='role_owner' AND permission_id='owner:recover'"))
        elif damage == 'builtin_kind':
            connection.execute(sa.text("UPDATE access_roles SET kind='custom' WHERE id='role_owner'"))
        else:
            if database.dialect.name == 'sqlite':
                connection.exec_driver_sql('PRAGMA ignore_check_constraints = ON')
            else:
                connection.exec_driver_sql('ALTER TABLE access_state DROP CONSTRAINT ck_access_state_versions')
            connection.execute(sa.text('UPDATE access_state SET catalogue_version=2'))
    with pytest.raises(AccessUnavailableError) as caught:
        AccessStore(database)
    assert str(caught.value) == 'access_unavailable'


def test_sql_ended_transaction_cannot_reuse_a_lock_marker(database):
    upgrade_schema(database)
    store = AccessStore(database)
    with database.connect() as connection:
        connection.begin()
        store.lock_on(connection)
        connection.exec_driver_sql('ROLLBACK')
        with pytest.raises(InvalidTransactionError):
            store.require_lock_on(connection)
        connection.rollback()


def test_sql_restarted_transaction_cannot_reuse_a_lock_marker(database):
    upgrade_schema(database)
    store = AccessStore(database)
    with database.connect() as connection:
        connection.begin()
        store.lock_on(connection)
        connection.exec_driver_sql('ROLLBACK')
        connection.exec_driver_sql('BEGIN')
        with pytest.raises(InvalidTransactionError):
            store.require_lock_on(connection)
        connection.rollback()


@pytest.mark.parametrize('database', ['postgresql'], indirect=True)
def test_postgresql_abort_chain_requires_a_fresh_access_lock(database):
    upgrade_schema(database)
    store = AccessStore(database)
    with database.connect() as connection:
        connection.begin()
        assert store.lock_on(connection) == 1
        original_transaction = connection.exec_driver_sql('SELECT txid_current()').scalar_one()
        connection.exec_driver_sql('ABORT AND CHAIN')
        assert connection.in_transaction()
        assert connection.exec_driver_sql('SELECT txid_current()').scalar_one() != original_transaction
        with pytest.raises(InvalidTransactionError):
            store.require_lock_on(connection)

        assert store.lock_on(connection) == 1
        assert store.require_lock_on(connection) == 1
        connection.exec_driver_sql('ABORT AND CHAIN')
        with pytest.raises(InvalidTransactionError):
            store.require_lock_on(connection)
        connection.rollback()

        connection.begin()
        with pytest.raises(InvalidTransactionError):
            store.require_lock_on(connection)
        assert store.lock_on(connection) == 1
        assert store.require_lock_on(connection) == 1
        connection.exec_driver_sql('ABORT')
        with pytest.raises(InvalidTransactionError):
            store.require_lock_on(connection)
        connection.rollback()
