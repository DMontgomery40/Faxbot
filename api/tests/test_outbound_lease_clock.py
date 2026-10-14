"""Lease decisions use time after real SQLite/PostgreSQL transaction contention."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
import time

import pytest
import sqlalchemy as sa

from api.tests.test_outbound_store import installation, accept
from api.tests.test_schema import database
from api.app import outbound_store
from api.app.outbound_store import DeliveryConflict

# Real seconds the helper waits for a worker before calling it stuck. It bounds a hang, not a lease: every lease here
# runs on the test's clock. A first claim on a loaded CI PostgreSQL once took 11.4 s after the lock was let go.
STUCK_SECONDS = 60


@pytest.fixture
def lease_clock(monkeypatch):
    class Clock(datetime):
        current = None

        @classmethod
        def utcnow(cls):
            return cls.current

        @classmethod
        def advance_to(cls, moment):
            cls.current = cls.fromisoformat(moment.isoformat())

    Clock.advance_to(datetime.utcnow())
    monkeypatch.setattr(outbound_store, 'datetime', Clock)
    return Clock


def after_lock_wait(configuration, clock, operation, *, deadline=None, released=None):
    """Advance the lease clock only after a worker requests the real database lock.

    Thread startup and the idle precheck may take any part of the watchdog budget;
    they do not consume the lease. The watchdog (``STUCK_SECONDS``) detects a stuck
    worker, not expiry.
    """
    requested = threading.Event()
    progress = threading.Event()
    worker = []

    def lock_statement(connection, cursor, statement, parameters, context, executemany):
        if worker and threading.get_ident() == worker[0] and (
                statement == 'BEGIN IMMEDIATE' or 'pg_advisory_xact_lock' in statement):
            requested.set()
            progress.set()

    def run():
        worker.append(threading.get_ident())
        return operation()

    sa.event.listen(configuration.engine, 'before_cursor_execute', lock_statement)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with configuration._locked():
                future = executor.submit(run)
                future.add_done_callback(lambda completed: progress.set())
                assert progress.wait(STUCK_SECONDS), 'Worker neither requested the database lock nor finished.'
                if future.done():
                    # Report a failed precheck's real exception instead of hiding it behind a timeout.
                    future.result()
                    pytest.fail('Worker finished without waiting for the database lock.')
                assert requested.is_set(), 'Worker did not request the database lock.'
                if deadline is not None:
                    assert clock.utcnow() < deadline
                    clock.advance_to(deadline + timedelta(milliseconds=200))
                    assert clock.utcnow() > deadline
                else:
                    clock.advance_to(clock.utcnow() + timedelta(seconds=1.2))
                if released is not None:
                    released.append(clock.utcnow())
            return future.result(timeout=STUCK_SECONDS)
    finally:
        sa.event.remove(configuration.engine, 'before_cursor_execute', lock_statement)


def test_expired_preparation_cannot_receive_grant_after_lock_wait(installation, lease_clock):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker', lease_seconds=3)
    with pytest.raises(DeliveryConflict, match='lease is no longer current'):
        after_lock_wait(configuration, lease_clock,
            lambda: delivery.grant_pdf(claim, url='http://localhost/synthetic.pdf', token='synthetic',
                expires_at=lease_clock.utcnow() + timedelta(minutes=1)), deadline=claim.expires_at)
    with configuration.engine.connect() as connection:
        assert connection.execute(sa.select(configuration.jobs.c.pdf_token).where(
            configuration.jobs.c.id == identity)).scalar_one() is None
    assert delivery.get(identity)['state'] == 'preparing'


def test_expired_preparation_cannot_authorize_submission_after_lock_wait(installation, lease_clock):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker', lease_seconds=3)
    assert after_lock_wait(configuration, lease_clock,
        lambda: delivery.begin_submission(claim), deadline=claim.expires_at) is False
    assert delivery.get(identity)['state'] == 'preparing'
    assert not any(event['kind'] == 'submission_authorized' for event in delivery.history(identity))


def test_new_claim_has_live_lease_after_lock_wait(installation, lease_clock):
    configuration, delivery, _ = installation
    identity = accept(installation)
    released = []
    claim = after_lock_wait(configuration, lease_clock, lambda: delivery.claim('worker', lease_seconds=1),
                            released=released)
    assert claim.job_id == identity
    # The 1 s lease starts after the clock advances 1.2 s during the lock wait.
    # Capturing time before taking the lock would produce an already-expired claim.
    assert claim.expires_at > released[0]


def test_new_claims_lease_starts_when_the_claim_is_written(installation, lease_clock, monkeypatch):
    """The claim's own reads inside the lock (once 4.8 s on SQLite and 11.4 s on PostgreSQL in CI) take longer than
    its 1 s lease here. The lease still starts as the claim is written, so its worker gets a live claim."""
    configuration, delivery, _ = installation
    identity = accept(installation)
    context = configuration._outbound_context

    def slow_reads(connection, job_id):
        lease_clock.advance_to(lease_clock.utcnow() + timedelta(seconds=1.5))
        return context(connection, job_id)

    monkeypatch.setattr(configuration, '_outbound_context', slow_reads)
    started = lease_clock.utcnow()
    claim = delivery.claim('worker', lease_seconds=1)
    assert claim.job_id == identity
    assert lease_clock.utcnow() > started + timedelta(seconds=1)  # the reads outlasted a lease begun at the lock
    assert claim.expires_at > lease_clock.utcnow()
    assert delivery.renew_lease(claim, lease_seconds=1) is True  # still its worker's, not given up
    assert delivery.recover_expired() == 0


def test_a_first_claim_reflects_no_tables_inside_its_write_lock(installation, monkeypatch):
    """Every other writer waits while a claim holds the lock: a first claim reflected 300 catalog statements' worth
    of capacity and schedule tables there. They are reflected before the lock, with plain reads."""
    from api.app import capacity
    from api.app.routing import schedule
    configuration, delivery, _ = installation
    monkeypatch.setattr(capacity, '_CACHE', {})
    monkeypatch.setattr(schedule, '_CACHE', {})
    accept(installation)
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(configuration.engine, 'before_cursor_execute', record)
    try:
        claim = delivery.claim('worker')
    finally:
        sa.event.remove(configuration.engine, 'before_cursor_execute', record)
    assert claim is not None
    [locked] = [index for index, statement in enumerate(statements)
                if statement == 'BEGIN IMMEDIATE' or 'pg_advisory_xact_lock' in statement]
    catalog = ('sqlite_master', 'sqlite_temp_master', 'pg_catalog', 'information_schema')
    reflected = [statement for statement in statements
                 if statement.upper().startswith('PRAGMA') or any(name in statement for name in catalog)]
    assert reflected  # this claim was the first: it did reflect the tables, before the lock
    inside = [statement for statement in statements[locked:] if statement in reflected]
    assert inside == []


@pytest.mark.parametrize('state', ['preparing', 'submitting'])
def test_recovery_considers_leases_expiring_during_lock_wait(installation, lease_clock, state):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker', lease_seconds=3)
    if state == 'submitting':
        assert delivery.begin_submission(claim)
    assert after_lock_wait(configuration, lease_clock, delivery.recover_expired, deadline=claim.expires_at) == 1
    assert delivery.get(identity)['state'] == ('ready' if state == 'preparing' else 'reconciliation_required')


def test_recovery_precheck_delay_does_not_consume_the_lease(installation, monkeypatch, lease_clock):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker', lease_seconds=3)
    original = delivery._any

    def delayed(query):
        # Reproduces the former helper's two-second entry timeout without changing the lease decision.
        time.sleep(2.2)
        return original(query)

    monkeypatch.setattr(delivery, '_any', delayed)
    assert after_lock_wait(configuration, lease_clock, delivery.recover_expired, deadline=claim.expires_at) == 1
    assert delivery.get(identity)['state'] == 'ready'


def test_media_capability_expiring_during_lock_wait_is_refused(installation, lease_clock):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker', lease_seconds=30)
    expires = lease_clock.utcnow() + timedelta(seconds=1)
    with pytest.raises(ValueError, match='Invalid delivery media grant'):
        after_lock_wait(configuration, lease_clock,
            lambda: delivery.grant_pdf(claim, url='http://localhost/synthetic.pdf', token='synthetic',
                expires_at=expires), deadline=expires)
    with configuration.engine.connect() as connection:
        assert connection.execute(sa.select(configuration.jobs.c.pdf_token).where(
            configuration.jobs.c.id == identity)).scalar_one() is None


def test_explicit_deterministic_now_is_preserved(installation):
    _, delivery, _ = installation
    identity = accept(installation)
    supplied = datetime(2025, 1, 2, 3, 4, 5)
    claim = delivery.claim('worker', now=supplied, lease_seconds=30)
    assert claim.expires_at == supplied + timedelta(seconds=30)
    assert delivery.begin_submission(claim, now=supplied + timedelta(seconds=1)) is True
    assert delivery.recover_expired(now=supplied + timedelta(seconds=31)) == 1
    assert delivery.get(identity)['state'] == 'reconciliation_required'
