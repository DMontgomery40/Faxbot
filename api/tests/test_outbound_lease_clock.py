"""Lease decisions use time after real SQLite/PostgreSQL transaction contention."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
import threading
import time

import pytest
import sqlalchemy as sa

from api.tests.test_outbound_store import installation, accept
from api.tests.test_schema import database
from api.app.outbound_store import DeliveryConflict


def after_lock_wait(configuration, monkeypatch, operation, *, deadline=None):
    """Block the real installation lock until a waiting call's lease has elapsed."""
    entered = threading.Event()
    original = configuration._locked

    @contextmanager
    def signal_wait():
        entered.set()
        with original() as connection:
            yield connection

    monkeypatch.setattr(configuration, '_locked', signal_wait)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with original():
            future = executor.submit(operation)
            assert entered.wait(2)
            if deadline is not None:
                assert datetime.utcnow() < deadline
                time.sleep(max(0, (deadline - datetime.utcnow()).total_seconds()) + 0.2)
                assert datetime.utcnow() > deadline
            else:
                time.sleep(1.2)
        return future.result(timeout=5)


def test_expired_preparation_cannot_receive_grant_after_lock_wait(installation, monkeypatch):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker', lease_seconds=1)
    with pytest.raises(DeliveryConflict, match='lease is no longer current'):
        after_lock_wait(configuration, monkeypatch,
            lambda: delivery.grant_pdf(claim, url='http://localhost/synthetic.pdf', token='synthetic',
                expires_at=datetime.utcnow() + timedelta(minutes=1)), deadline=claim.expires_at)
    with configuration.engine.connect() as connection:
        assert connection.execute(sa.select(configuration.jobs.c.pdf_token).where(
            configuration.jobs.c.id == identity)).scalar_one() is None
    assert delivery.get(identity)['state'] == 'preparing'


def test_expired_preparation_cannot_authorize_submission_after_lock_wait(installation, monkeypatch):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker', lease_seconds=1)
    assert after_lock_wait(configuration, monkeypatch,
        lambda: delivery.begin_submission(claim), deadline=claim.expires_at) is False
    assert delivery.get(identity)['state'] == 'preparing'
    assert not any(event['kind'] == 'submission_authorized' for event in delivery.history(identity))


def test_new_claim_has_live_lease_after_lock_wait(installation, monkeypatch):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = after_lock_wait(configuration, monkeypatch, lambda: delivery.claim('worker', lease_seconds=1))
    assert claim.job_id == identity
    assert claim.expires_at > datetime.utcnow()
    assert delivery.begin_submission(claim) is True


@pytest.mark.parametrize('state', ['preparing', 'submitting'])
def test_recovery_considers_leases_expiring_during_lock_wait(installation, monkeypatch, state):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker', lease_seconds=1)
    if state == 'submitting':
        assert delivery.begin_submission(claim)
    assert after_lock_wait(configuration, monkeypatch, delivery.recover_expired, deadline=claim.expires_at) == 1
    assert delivery.get(identity)['state'] == ('ready' if state == 'preparing' else 'reconciliation_required')


def test_media_capability_expiring_during_lock_wait_is_refused(installation, monkeypatch):
    configuration, delivery, _ = installation
    identity = accept(installation)
    claim = delivery.claim('worker', lease_seconds=30)
    expires = datetime.utcnow() + timedelta(seconds=1)
    with pytest.raises(ValueError, match='Invalid delivery media grant'):
        after_lock_wait(configuration, monkeypatch,
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
