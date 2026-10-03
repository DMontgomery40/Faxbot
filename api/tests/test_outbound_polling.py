"""Original-account status reads and atomic scheduling, no HTTP route tests."""
from datetime import datetime, timedelta
import asyncio
from uuid import uuid4

import httpx
import pytest

from api.tests.test_outbound_store import installation, accept
from api.tests.test_schema import database
from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_polling import OutboundPoller
from api.app.outbound_store import DeliveryConflict, OutboundStore


def issued(installation, *, created_at=None):
    configuration, store, snapshot = installation
    if created_at is None:
        job = accept(installation)
    else:
        job = uuid4().hex
        configuration.accept_outbound(snapshot.active, {'id': job, 'to_number': '+12025550123',
            'file_name': 'synthetic.txt', 'tiff_path': '', 'status': 'queued', 'pages': 3,
            'created_at': created_at, 'updated_at': created_at})
    claim = store.claim('worker', now=created_at)
    store.begin_submission(claim, now=created_at)
    store.record_receipt(claim, provider_sid='remote-one', status='in_progress', now=created_at)
    return job, claim


def with_profile(installation, profile):
    configuration, store, snapshot = installation
    snapshot = configuration.apply(snapshot, snapshot.active.values, actor='test',
        restart_required=False, providers={'outbound': profile})
    return configuration, store, snapshot


def signalwire(seconds, *, host='captured.signalwire.invalid', manifest=None):
    return ProviderConfiguration('signalwire', credentials={'api_token': 'synthetic-token'},
        settings={'space_url': host, 'project_id': 'synthetic-project', 'status_poll_seconds': seconds},
        manifest=manifest)


def status_wire(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'sid': 'remote-one', 'id': 'remote-one', 'status': 'sending'})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs))
    return requests


def test_poll_reservation_is_single_until_due_and_requires_remote_identity(installation):
    _, store, _ = installation
    accept(installation)
    assert store.reserve_poll() is None
    claim = store.claim('worker')
    store.begin_submission(claim)
    store.record_receipt(claim, provider_sid='remote-one', status='in_progress')
    now = datetime.utcnow()
    assert store.reserve_poll(now=now) == claim.job_id
    assert store.reserve_poll(now=now + timedelta(seconds=1)) is None
    assert store.reserve_poll(now=now + timedelta(seconds=31)) == claim.job_id


def test_poll_reservation_preserves_issuer_and_terminal_jobs_are_not_scheduled(installation):
    configuration, store, _ = installation
    job, claim = issued(installation)
    before = store.get(job)
    assert OutboundStore(configuration).reserve_poll() == job
    assert store.reserve_poll() is None
    after = store.get(job)
    for key in ('version', 'attempt_id', 'claim_token', 'claim_owner', 'updated_at'):
        assert before[key] == after[key]
    assert store.record_receipt(claim, provider_sid='remote-one', status='success')
    assert store.reserve_poll(now=datetime.utcnow() + timedelta(days=1)) is None
    assert store.poll_target(job) is None


@pytest.mark.asyncio
async def test_poll_uses_original_profile_and_terminal_callback_wins(installation, monkeypatch):
    configuration, store, snapshot = installation
    job, claim = issued(installation)
    configuration.apply(snapshot, snapshot.active.values, actor='test', restart_required=False,
        providers={'outbound': ProviderConfiguration('phaxio', credentials={'api_key': 'rotated'})})
    seen = []
    class Service:
        async def get_fax_status(self, sid):
            assert sid == 'remote-one'
            store.observe(job, attempt_id=claim.attempt_id, profile_id=claim.profile_id,
                provider_sid=sid, status='success', event_key='early-callback')
            return {'provider_sid': sid, 'status': 'in_progress'}
    def factory(profile):
        seen.append(profile.configuration.credentials['api_key'])
        return Service()
    monkeypatch.setattr('api.app.outbound_polling.service_from_profile', factory)
    assert await OutboundPoller(store).refresh(job) is False
    assert seen == ['synthetic-key']
    assert store.get(job)['state'] == 'success'


@pytest.mark.asyncio
async def test_poll_cannot_attach_a_different_provider_fax(installation, monkeypatch):
    _, store, _ = installation
    job, _ = issued(installation)
    class Service:
        async def get_fax_status(self, sid): return {'provider_sid': 'different', 'status': 'success'}
    monkeypatch.setattr('api.app.outbound_polling.service_from_profile', lambda profile: Service())
    with pytest.raises(DeliveryConflict):
        await OutboundPoller(store).refresh(job)
    assert store.get(job)['state'] == 'in_progress'


@pytest.mark.asyncio
async def test_status_timeout_preserves_in_progress_and_does_not_resubmit(installation, monkeypatch):
    _, store, _ = installation
    job, _ = issued(installation)
    class Service:
        async def get_fax_status(self, sid): raise TimeoutError('synthetic payload')
    monkeypatch.setattr('api.app.outbound_polling.service_from_profile', lambda profile: Service())
    with pytest.raises(TimeoutError):
        await OutboundPoller(store).refresh(job)
    assert store.get(job)['state'] == 'in_progress'
    assert store.claim('different-worker') is None


@pytest.mark.asyncio
async def test_status_request_is_bounded_and_cancelled_without_changing_delivery(installation, monkeypatch):
    _, store, _ = installation
    job, _ = issued(installation)
    cancelled = asyncio.Event()
    class Service:
        async def get_fax_status(self, sid):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    monkeypatch.setattr('api.app.outbound_polling.service_from_profile', lambda profile: Service())
    with pytest.raises(TimeoutError):
        await OutboundPoller(store, timeout=0.01).refresh(job)
    assert cancelled.is_set()
    assert store.get(job)['state'] == 'in_progress'
    assert store.claim('different-worker') is None


def test_never_polled_job_is_not_starved_by_due_repeat_reads(installation):
    _, store, _ = installation
    now = datetime.utcnow()
    earlier = [issued(installation, created_at=now)[0],
               issued(installation, created_at=now + timedelta(seconds=1))[0]]
    assert store.reserve_poll(now=now) == earlier[0]
    assert store.reserve_poll(now=now + timedelta(seconds=20)) == earlier[1]
    new_job, _ = issued(installation, created_at=now + timedelta(seconds=40))
    scheduled = []
    # Two 19-second reads are individually bounded by the poller's 20-second
    # timeout, but their cycle exceeds the 30-second repeat interval.
    for elapsed in range(40, 154, 19):
        scheduled.append(store.reserve_poll(now=now + timedelta(seconds=elapsed)))
    assert new_job in scheduled


def test_older_due_read_precedes_a_newer_unpolled_job(installation):
    _, store, _ = installation
    now = datetime.utcnow()
    old_job, _ = issued(installation, created_at=now)
    assert store.reserve_poll(now=now) == old_job
    new_job, _ = issued(installation, created_at=now + timedelta(seconds=60))
    assert store.reserve_poll(now=now + timedelta(seconds=60)) == old_job
    assert store.reserve_poll(now=now + timedelta(seconds=60)) == new_job


@pytest.mark.asyncio
async def test_zero_signalwire_interval_defers_automatic_reads_but_allows_manual_refresh(installation, monkeypatch):
    context = with_profile(installation, signalwire(0))
    _, store, _ = context
    job, _ = issued(context)
    requests = status_wire(monkeypatch)
    before = datetime.utcnow()
    assert await OutboundPoller(store).step()
    assert requests == []
    assert store.get(job)['next_poll_at'] >= before + timedelta(hours=1)
    assert not await OutboundPoller(store).step()
    assert await OutboundPoller(store).refresh(job)
    assert len(requests) == 1 and requests[0].method == 'GET'
    assert requests[0].url.host == 'captured.signalwire.invalid'


@pytest.mark.asyncio
async def test_signalwire_reservation_and_read_use_captured_positive_interval_after_rotation(installation, monkeypatch):
    context = with_profile(installation, signalwire(120))
    _, store, _ = context
    job, _ = issued(context)
    with_profile(context, signalwire(0, host='rotated.signalwire.invalid'))
    requests = status_wire(monkeypatch)
    earlier = datetime.utcnow() - timedelta(seconds=121)
    assert store.reserve_poll(now=earlier) == job
    assert store.get(job)['next_poll_at'] == earlier + timedelta(seconds=120)
    before = datetime.utcnow()
    assert await OutboundPoller(store).step()
    assert store.get(job)['next_poll_at'] >= before + timedelta(seconds=120)
    assert len(requests) == 1 and requests[0].method == 'GET'
    assert requests[0].url.host == 'captured.signalwire.invalid'


@pytest.mark.asyncio
async def test_signalwire_manifest_status_action_overrides_native_zero_policy(installation, monkeypatch):
    manifest = {'id': 'signalwire', 'allowed_domains': ['manifest.invalid'], 'auth': {'scheme': 'none'},
        'actions': {'send_fax': {'method': 'POST', 'url': 'https://manifest.invalid/create'},
            'get_status': {'method': 'GET', 'url': 'https://manifest.invalid/status/{{provider_sid}}',
                'response': {'job_id': 'id', 'status': 'status'}}}}
    context = with_profile(installation, signalwire(0, manifest=manifest))
    _, store, _ = context
    job, _ = issued(context)
    requests = status_wire(monkeypatch)
    assert await OutboundPoller(store).step()
    assert len(requests) == 1 and requests[0].method == 'GET'
    assert str(requests[0].url) == 'https://manifest.invalid/status/remote-one'
    assert store.get(job)['next_poll_at'] < datetime.utcnow() + timedelta(seconds=31)


@pytest.mark.asyncio
async def test_unsupported_native_poll_is_deferred_without_blocking_eligible_http_work(installation, monkeypatch):
    native = with_profile(installation, ProviderConfiguration('sip'))
    _, store, _ = native
    native_job, _ = issued(native)
    http_context = with_profile(native, signalwire(15))
    http_job, _ = issued(http_context)
    requests = status_wire(monkeypatch)
    before = datetime.utcnow()
    assert await OutboundPoller(store).step()
    assert requests == []
    assert store.get(native_job)['next_poll_at'] >= before + timedelta(hours=1)
    assert await OutboundPoller(store).step()
    assert len(requests) == 1 and requests[0].method == 'GET'
    assert store.get(http_job)['state'] == 'in_progress'


@pytest.mark.asyncio
async def test_excessive_positive_interval_does_not_block_the_next_eligible_fax(installation, monkeypatch):
    excessive = with_profile(installation, signalwire(10 ** 30))
    _, store, _ = excessive
    excessive_job, _ = issued(excessive)
    eligible = with_profile(excessive, signalwire(15, host='eligible.signalwire.invalid'))
    eligible_job, _ = issued(eligible)
    requests = status_wire(monkeypatch)
    assert await OutboundPoller(store).step()
    assert store.get(excessive_job)['next_poll_at'] == datetime.max
    assert await OutboundPoller(store).step()
    assert [request.url.host for request in requests] == [
        'captured.signalwire.invalid', 'eligible.signalwire.invalid']
    assert all(request.method == 'GET' for request in requests)
    assert store.get(eligible_job)['state'] == 'in_progress'
