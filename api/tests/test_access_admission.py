"""Internal pre-KDF budgets on real isolated migrated SQLite/PostgreSQL stores.

No route, KDF, session issuance or production database is used.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, asdict
from datetime import datetime, timedelta, timezone
import hmac
import importlib
from threading import Barrier, Event

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.app.schema import HEAD, SchemaUpgradeError, upgrade_schema
from api.app.schema_access import frozen_metadata as access_metadata
from api.app.access.store import AccessStore
from api.app.access.types import (
    AccessUnavailableError, AuthenticationError, InvalidTransactionError,
)


NOW = datetime(2026, 10, 3, 12)
KEY = bytes(range(32))


def modules():
    for name in ('api.app.schema_authentication', 'api.app.access.admission'):
        assert importlib.util.find_spec(name) is not None, 'bounded admission dependency is missing'
    return (importlib.import_module('api.app.schema_authentication'),
            importlib.import_module('api.app.access.admission'))


class World:
    def __init__(self, engine):
        schema, self.module = modules()
        upgrade_schema(engine)
        with engine.connect() as c:
            assert c.execute(sa.text('SELECT version_num FROM alembic_version')).scalar_one() == HEAD
        self.buckets = schema.frozen_metadata(dialect=engine.dialect.name).tables['access_auth_buckets']
        self.engine, self.store = engine, AccessStore(engine)
        self.service = self.module.AuthenticationAdmission(self.store, installation_key=KEY, clock=lambda: NOW)
        self.audit = self.store.tables['access_audit']
        self.baseline_audit = len(self.rows(self.audit))

    def rows(self, table):
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(sa.select(table).order_by(table.c.id)).mappings()]

    def state(self):
        with self.engine.connect() as c:
            version = c.execute(sa.select(self.store.tables['access_state'].c.policy_version)).scalar_one()
        return self.rows(self.buckets), self.rows(self.audit), version

    def reserve(self, kind='password_login', subject='alice', *, now=NOW):
        with self.store.transaction() as c:
            return self.service.reserve_on(c, kind, subject, now=now)

    def seed(self, count, *, next_at=NOW, updated_at=NOW, start=0):
        with self.engine.begin() as c:
            c.execute(self.buckets.insert(), [dict(id=f'{i:064x}', next_at=next_at,
                updated_at=updated_at) for i in range(start, start + count)])


@pytest.fixture
def world(database):
    return World(database)


def safe_error(failure, code):
    assert failure.value.args == (code,)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


def test_frozen_schema_operations_are_additive_and_refuse_existing_table(database):
    schema, _ = modules()
    from api.tests.test_access_schema import at_revision
    at_revision(database, '0005_access_control')
    with database.begin() as c:
        before = c.execute(sa.select(sa.func.count()).select_from(sa.table('access_audit'))).scalar_one()
        schema.upgrade_authentication(c, Operations(MigrationContext.configure(c)))
        table = schema.frozen_metadata(dialect=database.dialect.name).tables['access_auth_buckets']
        assert c.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
        assert c.execute(sa.text('SELECT version_num FROM alembic_version')).scalar_one() == '0005_access_control'
        assert c.execute(sa.select(sa.func.count()).select_from(sa.table('access_audit'))).scalar_one() == before
        inspector = sa.inspect(c)
        assert inspector.get_pk_constraint(table.name)['name'] == 'pk_access_auth_buckets'
        assert [(col['name'], col['nullable']) for col in inspector.get_columns(table.name)] == [
            ('id', False), ('next_at', False), ('updated_at', False)]
        assert [(index['name'], index['column_names'], index['unique']) for index in inspector.get_indexes(table.name)] == [
            ('ix_access_auth_buckets_next_at', ['next_at'], False)]
        assert not inspector.get_foreign_keys(table.name)
        assert {check['name'] for check in inspector.get_check_constraints(table.name)} == {'ck_access_auth_buckets_times'}
    with database.begin() as c:
        with pytest.raises(SchemaUpgradeError) as failure:
            schema.upgrade_authentication(c, Operations(MigrationContext.configure(c)))
        assert 'private' not in str(failure.value)
        assert c.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as c:
            c.execute(table.insert().values(id='bad', next_at=NOW, updated_at=NOW + timedelta(seconds=1)))


def test_new_head_metadata_preserves_frozen_access_tables(database):
    schema, _ = modules()
    previous = access_metadata(dialect=database.dialect.name)
    new = schema.frozen_metadata(dialect=database.dialect.name)
    assert schema.TABLES == frozenset({'access_auth_buckets'})
    assert set(new.tables) == set(previous.tables) | {'access_auth_buckets'}
    for name in previous.tables:
        assert str(sa.schema.CreateTable(new.tables[name]).compile(dialect=database.dialect)) == str(
            sa.schema.CreateTable(previous.tables[name]).compile(dialect=database.dialect))


def test_falsey_callable_clock_is_preserved(world):
    class FixedClock:
        def __bool__(self):
            return False
        def __call__(self):
            return NOW
    service = world.module.AuthenticationAdmission(world.store, installation_key=KEY, clock=FixedClock())
    assert service.reserve('password_login', 'alice').allowed
    assert {r['updated_at'] for r in world.rows(world.buckets)} == {NOW}


def test_reserved_view_refused_before_any_new_ddl(database):
    schema, _ = modules()
    from api.tests.test_access_schema import at_revision
    at_revision(database, '0005_access_control')
    with database.begin() as c:
        c.exec_driver_sql("CREATE VIEW access_auth_buckets AS SELECT 'private-view-marker' AS id")
        with pytest.raises(SchemaUpgradeError) as failure:
            schema.upgrade_authentication(c, Operations(MigrationContext.configure(c)))
        assert 'private-view-marker' not in str(failure.value)
        assert c.execute(sa.text('SELECT id FROM access_auth_buckets')).scalar_one() == 'private-view-marker'


def test_password_burst_refill_and_equal_boundary(world):
    assert [world.reserve().allowed for _ in range(5)] == [True] * 5
    before = world.state()
    denied = world.reserve()
    assert asdict(denied) == {'allowed': False, 'retry_after_seconds': 12}
    assert world.state() == before
    assert world.reserve(now=NOW + timedelta(seconds=11, microseconds=999999)).retry_after_seconds == 1
    assert world.reserve(now=NOW + timedelta(seconds=12)).allowed
    assert world.reserve(now=NOW + timedelta(seconds=12)).retry_after_seconds == 12


def test_header_key_has_ten_unit_burst_and_200ms_refill(world):
    for _ in range(10):
        assert world.reserve('key_request', '012345abcdef').allowed
    before = world.state()
    assert world.reserve('key_request', '012345abcdef').retry_after_seconds == 1
    assert world.reserve('key_request', '012345abcdef',
        now=NOW + timedelta(milliseconds=199, microseconds=999)).retry_after_seconds == 1
    assert world.state() == before
    assert world.reserve('key_request', '012345abcdef', now=NOW + timedelta(milliseconds=200)).allowed


def test_password_change_shares_bucket_but_accounts_and_key_purposes_do_not(world):
    for _ in range(5):
        assert world.reserve('password_login', 'alice').allowed
    assert world.reserve('password_change', 'alice').retry_after_seconds == 12
    assert world.reserve('password_login', 'bob').allowed
    for _ in range(5):
        assert world.reserve('key_login', '012345abcdef').allowed
    assert not world.reserve('key_login', '012345abcdef').allowed
    for _ in range(9):
        assert world.reserve('key_request', '012345abcdef').allowed
    # Global capacity 20 was consumed; its denial did not use the 10th subject unit.
    assert not world.reserve('key_request', '012345abcdef').allowed
    assert world.reserve('key_request', '012345abcdef', now=NOW + timedelta(milliseconds=100)).allowed


def test_global_burst_then_sustained_rate_is_not_a_fixed_minute_window(world):
    for i in range(20):
        assert world.reserve('key_request', f'{i:012x}').allowed
    assert world.reserve('key_request', 'ffffffffffff', now=NOW + timedelta(milliseconds=99)).retry_after_seconds == 1
    # Hand-derived: burst20 plus one request each100ms over60seconds =620.
    for tick in range(1, 601):
        assert world.reserve('key_request', f'{tick + 100:012x}',
            now=NOW + timedelta(milliseconds=100 * tick)).allowed
    assert not world.reserve('key_request', 'ffffffffffff', now=NOW + timedelta(seconds=60)).allowed
    assert world.reserve('key_request', 'ffffffffffff', now=NOW + timedelta(seconds=60, milliseconds=100)).allowed
    assert len(world.rows(world.audit)) == world.baseline_audit


def test_denied_global_does_not_create_subject_or_charge_either_candidate(world):
    for i in range(20):
        assert world.reserve('password_login', f'user{i}').allowed
    before = world.state()
    for _ in range(10):
        assert not world.reserve('password_login', 'not-yet-reserved').allowed
    assert world.state() == before


def test_audit_is_bounded_nonsecret_and_does_not_change_policy(world):
    before = world.state()[2]
    assert world.service.reserve('password_login', 'synthetic-private-login').allowed
    assert world.service.reserve('password_change', 'synthetic-private-login').allowed
    assert world.service.reserve('key_login', 'bootstrap').allowed
    assert world.service.reserve('key_request', '012345abcdef').allowed
    assert world.state()[2] == before
    events = [r for r in world.rows(world.audit) if r['operation'] == 'authentication.admit']
    assert len(events) == 3
    for event, kind in zip(sorted(events, key=lambda r: r['details']),
                           ['key_login', 'password_change', 'password_login']):
        assert event['details'] == '{"kind":"' + kind + '"}'
        assert event['target_kind'] == 'authentication'
        assert all(event[field] is None for field in (
            'target_id', 'actor_principal_id', 'actor_key_binding_id', 'actor_session_id'))
        assert event['policy_version_before'] == event['policy_version_after'] == before
    rows = world.rows(world.buckets)
    expected = hmac.digest(KEY, b'faxbot/access/auth-admission/v1\0password\0synthetic-private-login', 'sha256').hex()
    assert expected in {r['id'] for r in rows}
    assert all(r['id'] == 'global' or (len(r['id']) == 64 and r['id'] == bytes.fromhex(r['id']).hex()) for r in rows)
    assert 'synthetic-private-login' not in repr((rows, events, world.service))
    receipt = world.service.reserve('key_request', '012345abcdef')
    assert set(asdict(receipt)) == {'allowed', 'retry_after_seconds'}
    with pytest.raises(FrozenInstanceError):
        receipt.allowed = False


def test_shared_key_identity_survives_service_instance_and_key_rotation(world):
    other = world.module.AuthenticationAdmission(AccessStore(world.engine), installation_key=KEY, clock=lambda: NOW)
    for i in range(5):
        assert (world.service if i % 2 else other).reserve('password_login', 'alice').allowed
    assert not other.reserve('password_change', 'alice').allowed
    before_ids = {r['id'] for r in world.rows(world.buckets)}
    rotated = world.module.AuthenticationAdmission(AccessStore(world.engine), installation_key=b'\xff' * 32, clock=lambda: NOW)
    assert rotated.reserve('password_login', 'alice').allowed
    assert len({r['id'] for r in world.rows(world.buckets)} - before_ids) == 1
    assert len([r for r in world.rows(world.buckets) if r['id'] == 'global']) == 1


class HostileString(str):
    def __len__(self):
        raise AssertionError('caller subclass reached')


class DateSubclass(datetime):
    pass


class BytesSubclass(bytes):
    pass


def test_invalid_inputs_reject_before_writes_and_do_not_disclose(world):
    bad = [
        (None, 'alice'), (True, 'alice'), (b'password_login', 'alice'),
        (HostileString('password_login'), 'alice'), ('x' * 1_000_000, 'alice'),
        ('other', 'alice'), ('password_login', None), ('password_login', True),
        ('password_login', b'alice'), ('password_login', HostileString('alice')),
        ('password_login', 'Alice'), ('password_login', ' alice'),
        ('password_login', 'alice '), ('password_login', ''),
        ('password_login', 'é'), ('password_login', '\ud800'),
        ('password_login', 'x' * 1_000_000), ('key_request', 'bootstrap'),
        ('key_request', '012345ABCDEf'), ('key_request', 'x' * 12),
        ('key_login', '012345abcdef0'), ('key_login', 'BOOTSTRAP'),
    ]
    before = world.state()
    for kind, subject in bad:
        with pytest.raises(AuthenticationError) as failure:
            world.service.reserve(kind, subject)
        safe_error(failure, 'unauthenticated')
    for now in (None, True, '2026-10-03', DateSubclass(2026, 10, 3),
                NOW.replace(tzinfo=timezone.utc), datetime.min, datetime.max):
        with world.store.transaction() as c:
            with pytest.raises(AuthenticationError) as failure:
                world.service.reserve_on(c, 'password_login', 'alice', now=now)
            safe_error(failure, 'unauthenticated')
    assert world.state() == before


def test_wrong_keys_and_missing_table_fail_without_backend_context(database):
    _, admission = modules()
    from api.tests.test_access_schema import at_revision
    at_revision(database, '0005_access_control')
    store = AccessStore(database)
    for key in (None, True, '', bytearray(32), BytesSubclass(KEY), b'x' * 31,
                b'x' * 33, b'x' * 1_000_000):
        with pytest.raises(AccessUnavailableError) as failure:
            admission.AuthenticationAdmission(store, installation_key=key)
        safe_error(failure, 'access_unavailable')
    with pytest.raises(AccessUnavailableError) as failure:
        admission.AuthenticationAdmission(store, installation_key=KEY)
    safe_error(failure, 'access_unavailable')


def test_two_store_workers_share_one_burst(world):
    stores = [AccessStore(world.engine), AccessStore(world.engine)]
    services = [world.module.AuthenticationAdmission(s, installation_key=KEY, clock=lambda: NOW) for s in stores]
    barrier = Barrier(2)
    def reserve(service):
        barrier.wait(timeout=5)
        return [service.reserve('password_login', 'alice').allowed for _ in range(8)]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(reserve, services))
    assert sum(sum(result) for result in results) == 5
    assert len(world.rows(world.buckets)) == 2
    assert len(world.rows(world.audit)) == world.baseline_audit + 5


def test_clock_is_sampled_only_after_store_lock(world, monkeypatch):
    held, release = Event(), Event()
    original = world.store.lock_on
    def wait_lock(c):
        held.set()
        assert release.wait(5)
        return original(c)
    monkeypatch.setattr(world.store, 'lock_on', wait_lock)
    samples = []
    service = world.module.AuthenticationAdmission(world.store, installation_key=KEY,
        clock=lambda: samples.append(NOW) or NOW)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(service.reserve, 'password_login', 'alice')
        assert held.wait(5)
        try:
            assert samples == []
        finally:
            release.set()
        assert future.result(timeout=5).allowed
    assert samples == [NOW]


def test_clock_regression_never_resets_debt_and_retry_ceil_is_exact(world):
    for _ in range(5):
        assert world.reserve().allowed
    before = world.state()
    denied = world.reserve(now=NOW - timedelta(microseconds=1))
    assert not denied.allowed and denied.retry_after_seconds == 13
    assert world.state() == before
    assert world.reserve(now=NOW + timedelta(seconds=12)).allowed


def test_future_updated_clock_wait_and_retry_are_bounded(world):
    assert world.reserve().allowed
    with world.engine.begin() as c:
        c.execute(world.buckets.update().values(next_at=datetime.max, updated_at=datetime.max))
    before = world.state()
    denied = world.reserve()
    assert not denied.allowed and denied.retry_after_seconds == 86400
    assert world.state() == before


def test_corrupt_debt_overflow_fails_closed(world):
    assert world.reserve().allowed
    with world.engine.begin() as c:
        c.execute(world.buckets.update().values(next_at=datetime.min, updated_at=datetime.min))
    before = world.state()
    with pytest.raises(AccessUnavailableError) as failure:
        world.reserve()
    safe_error(failure, 'access_unavailable')
    assert world.state() == before


def test_corrupt_time_order_refuses_even_if_check_was_bypassed(world):
    assert world.reserve().allowed
    with world.engine.begin() as c:
        if world.engine.dialect.name == 'sqlite':
            c.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
        else:
            c.exec_driver_sql('ALTER TABLE access_auth_buckets DROP CONSTRAINT ck_access_auth_buckets_times')
        c.execute(world.buckets.update().values(next_at=NOW, updated_at=NOW + timedelta(seconds=1)))
        if world.engine.dialect.name == 'sqlite':
            c.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')
    before = world.state()
    with pytest.raises(AccessUnavailableError) as failure:
        world.reserve()
    safe_error(failure, 'access_unavailable')
    assert world.state() == before


def test_crypto_and_clock_failures_retain_no_backend_context_or_writes(world, monkeypatch):
    before = world.state()
    def backend_error(*args, **kwargs):
        raise RuntimeError('private-key-subject-backend-marker')
    with monkeypatch.context() as guard:
        guard.setattr(hmac, 'digest', backend_error)
        with pytest.raises(AccessUnavailableError) as failure:
            world.service.reserve('password_login', 'alice')
        safe_error(failure, 'access_unavailable')
    for result in (b'x' * 31, 'private-hmac-marker', BytesSubclass(b'x' * 32)):
        with monkeypatch.context() as guard:
            guard.setattr(hmac, 'digest', lambda *args, **kwargs: result)
            with pytest.raises(AccessUnavailableError) as failure:
                world.service.reserve('password_login', 'alice')
            safe_error(failure, 'access_unavailable')
    broken_clock = world.module.AuthenticationAdmission(world.store, installation_key=KEY, clock=backend_error)
    with pytest.raises(AccessUnavailableError) as failure:
        broken_clock.reserve('password_login', 'alice')
    safe_error(failure, 'access_unavailable')
    assert world.state() == before


def test_cleanup_is_ordered_bounded_and_preserves_active_debt(world):
    idle = NOW - timedelta(minutes=11)
    world.seed(301, next_at=idle, updated_at=idle)
    world.seed(1, start=1000, next_at=NOW + timedelta(seconds=60))
    assert world.reserve().allowed
    ids = {r['id'] for r in world.rows(world.buckets)}
    assert len(ids) == 104
    assert not ({f'{i:064x}' for i in range(200)} & ids)
    assert {f'{i:064x}' for i in range(200, 301)} <= ids
    assert f'{1000:064x}' in ids


def test_cleanup_cannot_delete_current_pair_before_it_is_renewed(world):
    assert world.reserve().allowed
    assert world.reserve(now=NOW + timedelta(hours=1)).allowed
    assert len(world.rows(world.buckets)) == 2
    for _ in range(4):
        assert world.reserve(now=NOW + timedelta(hours=1)).allowed
    assert world.reserve(now=NOW + timedelta(hours=1)).retry_after_seconds == 12


def test_denied_admission_may_commit_bounded_idle_cleanup_without_audit(world):
    for i in range(20):
        assert world.reserve('password_login', f'user{i}').allowed
    active = world.state()
    idle = NOW - timedelta(minutes=11)
    world.seed(201, next_at=idle, updated_at=idle)
    assert not world.service.reserve('password_login', 'newuser').allowed
    after = world.state()
    assert len(after[0]) == 22
    assert [row for row in after[0] if row['updated_at'] == NOW] == active[0]
    assert after[1:] == active[1:]


def test_capacity_counts_both_missing_rows_and_existing_pair_can_continue(world):
    world.seed(9999)
    before = world.state()
    denied = world.reserve()
    assert not denied.allowed and denied.retry_after_seconds == 60
    assert world.state() == before
    with world.engine.begin() as c:
        c.execute(world.buckets.delete().where(world.buckets.c.id == f'{0:064x}'))
    assert world.reserve().allowed
    assert len(world.rows(world.buckets)) == 10000
    assert world.reserve().allowed
    assert not world.reserve('password_login', 'bob').allowed


def test_audit_failure_rolls_back_cleanup_debt_and_new_rows(world):
    idle = NOW - timedelta(minutes=11)
    world.seed(201, next_at=idle, updated_at=idle)
    before = world.state()
    with world.engine.begin() as c:
        if world.engine.dialect.name == 'sqlite':
            c.exec_driver_sql("CREATE TRIGGER fail_admission_audit BEFORE INSERT ON access_audit BEGIN SELECT RAISE(ABORT, 'private-storage-marker'); END")
        else:
            c.exec_driver_sql("CREATE FUNCTION fail_admission_audit_fn() RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'private-storage-marker'; END $$")
            c.exec_driver_sql('CREATE TRIGGER fail_admission_audit BEFORE INSERT ON access_audit FOR EACH ROW EXECUTE FUNCTION fail_admission_audit_fn()')
    with pytest.raises(AccessUnavailableError) as failure:
        world.service.reserve('password_login', 'alice')
    safe_error(failure, 'access_unavailable')
    assert world.state() == before


@pytest.mark.parametrize('committed', [False, True])
def test_lost_commit_ack_never_returns_allowed_or_retries(world, monkeypatch, committed):
    original = sa.engine.Connection.commit
    calls = []
    def lose_ack(c):
        if c.info.get('admission-commit-probe'):
            calls.append(True)
            if committed:
                original(c)
            raise RuntimeError('private-commit-marker')
        return original(c)
    real_transaction = world.store.transaction
    @contextmanager
    def marked_transaction():
        with real_transaction() as c:
            c.info['admission-commit-probe'] = True
            try:
                yield c
            finally:
                # The marker must survive through the outer commit but pooled
                # connections are cleared after the probe below.
                pass
    monkeypatch.setattr(world.store, 'transaction', marked_transaction)
    monkeypatch.setattr(sa.engine.Connection, 'commit', lose_ack)
    with pytest.raises(AccessUnavailableError) as failure:
        world.service.reserve('password_login', 'alice')
    safe_error(failure, 'access_unavailable')
    monkeypatch.setattr(sa.engine.Connection, 'commit', original)
    with world.engine.connect() as c:
        c.info.pop('admission-commit-probe', None)
    assert len(calls) == 1
    assert len(world.rows(world.buckets)) == (2 if committed else 0)
    assert len(world.rows(world.audit)) == world.baseline_audit + int(committed)


def test_on_seam_never_opens_connection_and_rollback_retains_no_admission(world, monkeypatch):
    before = world.state()
    with world.store.transaction() as c:
        with monkeypatch.context() as guard:
            def forbidden(*args, **kwargs):
                raise AssertionError('second connection attempted')
            guard.setattr(world.engine, 'connect', forbidden)
            assert world.service.reserve_on(c, 'password_login', 'alice', now=NOW).allowed
        c.rollback()
    assert world.state() == before


def test_on_requires_exact_store_lock_and_no_nested_transaction(world):
    with world.engine.begin() as c:
        with pytest.raises(InvalidTransactionError):
            world.service.reserve_on(c, 'password_login', 'alice', now=NOW)
    other = AccessStore(world.engine)
    with other.transaction() as c:
        with pytest.raises(InvalidTransactionError):
            world.service.reserve_on(c, 'password_login', 'alice', now=NOW)
    with world.store.transaction() as c:
        with c.begin_nested():
            with pytest.raises(InvalidTransactionError):
                world.service.reserve_on(c, 'password_login', 'alice', now=NOW)
