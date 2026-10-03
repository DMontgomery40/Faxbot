"""Internal worker crash windows against real durable stores, never API acceptance."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest

from api.tests.test_outbound_store import installation, accept
from api.tests.test_schema import database
from api.app.outbound_worker import OutboundWorker, SubmissionReceipt, PreparationFailure


class Transport:
    def __init__(self, submit=None):
        self.submissions = 0
        self.closed = 0
        self.preparing = None
        self.operation = submit

    @asynccontextmanager
    async def prepare(self, claim):
        if self.preparing:
            await self.preparing(claim)
        try:
            yield self
        finally:
            self.closed += 1

    async def submit(self):
        self.submissions += 1
        if self.operation:
            return await self.operation()
        return SubmissionReceipt('remote-one', 'in_progress')


@pytest.mark.asyncio
async def test_worker_recovers_ready_work_after_owner_restart(installation):
    _, store, _ = installation
    job = accept(installation)
    transport = Transport()
    worker = OutboundWorker(store, transport)
    assert await worker.step() is True
    assert store.get(job)['state'] == 'in_progress'
    assert await OutboundWorker(store, transport).step() is False
    assert transport.submissions == 1 and transport.closed == 1


@pytest.mark.asyncio
async def test_ambiguous_transport_error_is_never_automatically_retried(installation):
    _, store, _ = installation
    job = accept(installation)
    async def lost_response():
        raise TimeoutError('synthetic secret must not reach history')
    transport = Transport(lost_response)
    await OutboundWorker(store, transport).step()
    assert store.get(job)['state'] == 'reconciliation_required'
    assert await OutboundWorker(store, transport).step() is False
    assert transport.submissions == 1
    assert 'synthetic secret' not in str(store.history(job))


@pytest.mark.asyncio
async def test_shutdown_during_submission_preserves_uncertainty(installation):
    _, store, _ = installation
    job = accept(installation)
    issued = asyncio.Event()
    async def pending():
        issued.set()
        await asyncio.Event().wait()
    transport = Transport(pending)
    task = asyncio.create_task(OutboundWorker(store, transport).step())
    await asyncio.wait_for(issued.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get(job)['state'] == 'reconciliation_required'
    assert transport.closed == 1
    assert await OutboundWorker(store, transport).step() is False


@pytest.mark.asyncio
async def test_lost_marker_commit_ack_never_invokes_transport(installation, monkeypatch):
    _, store, _ = installation
    job = accept(installation)
    original = store.begin_submission
    def lost_ack(claim):
        assert original(claim)
        raise RuntimeError('lost commit response')
    monkeypatch.setattr(store, 'begin_submission', lost_ack)
    transport = Transport()
    with pytest.raises(RuntimeError):
        await OutboundWorker(store, transport).step()
    assert transport.submissions == 0
    store.recover_expired(now=datetime.utcnow() + timedelta(minutes=10))
    assert store.get(job)['state'] == 'reconciliation_required'


@pytest.mark.asyncio
async def test_expired_preparation_cannot_authorize_a_stale_worker(installation):
    _, store, _ = installation
    job = accept(installation)
    transport = Transport()
    async def expire(claim):
        store.recover_expired(now=claim.expires_at + timedelta(seconds=1))
    transport.preparing = expire
    await OutboundWorker(store, transport).step()
    assert transport.submissions == 0
    assert store.get(job)['state'] == 'ready'


@pytest.mark.asyncio
async def test_lost_receipt_commit_ack_preserves_committed_delivery(installation, monkeypatch):
    _, store, _ = installation
    job = accept(installation)
    original = store.record_receipt
    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('lost acknowledgement')
    monkeypatch.setattr(store, 'record_receipt', lost_ack)
    transport = Transport()
    with pytest.raises(RuntimeError):
        await OutboundWorker(store, transport).step()
    assert store.get(job)['state'] == 'in_progress'
    assert await OutboundWorker(store, transport).step() is False
    assert transport.submissions == 1


@pytest.mark.asyncio
async def test_preparation_failure_is_definitive_and_never_submits(installation):
    _, store, _ = installation
    job = accept(installation)
    transport = Transport()
    async def missing(claim):
        raise PreparationFailure('artifact_unavailable')
    transport.preparing = missing
    await OutboundWorker(store, transport).step()
    assert store.get(job)['state'] == 'failed'
    assert transport.submissions == 0


@pytest.mark.asyncio
async def test_timeout_or_unusable_receipt_requires_reconciliation(installation):
    _, store, _ = installation
    async def timeout():
        await asyncio.Event().wait()
    async def malformed():
        return SubmissionReceipt('', 'success')
    for operation in (timeout, malformed):
        job = accept(installation)
        transport = Transport(operation)
        await OutboundWorker(store, transport, submission_timeout=0.01).step()
        assert store.get(job)['state'] == 'reconciliation_required'
        assert transport.submissions == 1 and transport.closed == 1


@pytest.mark.asyncio
async def test_shutdown_joins_marker_transaction_before_releasing_ownership(installation, monkeypatch):
    import threading
    _, store, _ = installation
    job = accept(installation)
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    original = store.begin_submission
    def slow_marker(claim):
        entered.set()
        assert release.wait(5)
        result = original(claim)
        completed.set()
        return result
    monkeypatch.setattr(store, 'begin_submission', slow_marker)
    transport = Transport()
    task = asyncio.create_task(OutboundWorker(store, transport).step())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set() and transport.closed == 1
    assert transport.submissions == 0
    store.recover_expired(now=datetime.utcnow() + timedelta(minutes=10))
    assert store.get(job)['state'] == 'reconciliation_required'
