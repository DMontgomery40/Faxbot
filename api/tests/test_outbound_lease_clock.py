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
    they do not consume the lease. The watchdog detects a stuck worker, not expiry.
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
                assert progress.wait(10), 'Worker neither requested the database lock nor finished.'
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
            return future.result(timeout=10)
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
