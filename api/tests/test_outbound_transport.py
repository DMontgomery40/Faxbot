"""Captured transport preparation and adapter boundary, using no HTTP routes."""
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from api.tests.test_outbound_store import installation, accept
from api.tests.test_schema import database
from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_transport import CapturedTransport
from api.app.outbound_worker import OutboundWorker


class Runtime:
    def __init__(self):
        self.revisions = []
    @contextmanager
    def frame(self, revision):
        self.revisions.append(revision.id)
        yield


def prepared_job(installation, tmp_path):
    configuration, store, snapshot = installation
    profile = ProviderConfiguration('phaxio', credentials={'api_key': 'old-key', 'api_secret': 'old-secret'})
    snapshot = configuration.apply(snapshot, snapshot.active.values.with_patch({'fax_data_dir': str(tmp_path)}),
        actor='test', restart_required=False, providers={'outbound': profile})
    job = accept((configuration, store, snapshot))
    (tmp_path / (job + '.pdf')).write_bytes(b'%PDF-synthetic-internal-seam')
    return job, snapshot


@pytest.mark.asyncio
async def test_transport_uses_accepted_profile_and_persists_media_grant_before_submission(installation, tmp_path, monkeypatch):
    import sqlalchemy as sa
    configuration, store, _ = installation
    job, snapshot = prepared_job(installation, tmp_path)
    configuration.apply(snapshot, snapshot.active.values, actor='test', restart_required=False,
        providers={'outbound': ProviderConfiguration('phaxio', credentials={'api_key': 'new-key', 'api_secret': 'new-secret'})})
    calls = []
    class Service:
        def is_configured(self): return True
        async def send_fax(self, to, url, job_id, *, attempt_id):
            assert store.get(job_id)['state'] == 'submitting'
            with configuration.engine.connect() as connection:
                record = connection.execute(sa.select(configuration.jobs).where(configuration.jobs.c.id == job_id)).mappings().one()
                assert record['pdf_url'] == url and record['pdf_token'] in url
                assert record['pdf_token_expires_at'] > datetime.utcnow()
            calls.append((to, url, job_id, attempt_id))
            return {'provider_sid': 'remote-one', 'status': 'queued'}
    profiles = []
    def factory(profile):
        profiles.append(profile)
        return Service()
    monkeypatch.setattr('api.app.outbound_transport.service_from_profile', factory)
    runtime = Runtime()
    await OutboundWorker(store, CapturedTransport(store, runtime)).step()
    assert len(calls) == 1 and profiles[0].configuration.credentials['api_key'] == 'old-key'
    assert runtime.revisions == [snapshot.active.id]
    assert store.get(job)['state'] == 'in_progress'


@pytest.mark.asyncio
async def test_missing_artifact_never_reaches_provider(installation, tmp_path, monkeypatch):
    _, store, _ = installation
    job, _ = prepared_job(installation, tmp_path)
    (tmp_path / (job + '.pdf')).unlink()
    calls = []
    monkeypatch.setattr('api.app.outbound_transport.service_from_profile', lambda profile: calls.append(profile))
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    assert calls == [] and store.get(job)['state'] == 'failed'


def test_media_grant_refuses_stale_preparation_token(installation, tmp_path):
    _, store, _ = installation
    job, _ = prepared_job(installation, tmp_path)
    claim = store.claim('worker-one')
    store.recover_expired(now=claim.expires_at + timedelta(seconds=1))
    from api.app.outbound_store import DeliveryConflict
    with pytest.raises(DeliveryConflict):
        store.grant_pdf(claim, url='http://localhost/fax.pdf', token='synthetic',
            expires_at=datetime.utcnow() + timedelta(minutes=1))


@pytest.mark.asyncio
async def test_provider_missing_remote_identity_is_uncertain(installation, tmp_path, monkeypatch):
    _, store, _ = installation
    job, _ = prepared_job(installation, tmp_path)
    class Service:
        def is_configured(self): return True
        async def send_fax(self, *args, **kwargs): return {'provider_sid': '', 'status': 'queued'}
    monkeypatch.setattr('api.app.outbound_transport.service_from_profile', lambda profile: Service())
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    assert store.get(job)['state'] == 'reconciliation_required'


@pytest.mark.asyncio
async def test_symlinked_document_never_reaches_provider(installation, tmp_path, monkeypatch):
    _, store, _ = installation
    job, _ = prepared_job(installation, tmp_path)
    document = tmp_path / (job + '.pdf')
    outside = tmp_path / 'unrelated.pdf'
    document.replace(outside)
    document.symlink_to(outside)
    calls = []
    monkeypatch.setattr('api.app.outbound_transport.service_from_profile', lambda profile: calls.append(profile))
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    assert calls == [] and store.get(job)['state'] == 'failed'


@pytest.mark.asyncio
async def test_terminal_provider_reply_is_not_downgraded(installation, tmp_path, monkeypatch):
    _, store, _ = installation
    job, _ = prepared_job(installation, tmp_path)
    class Service:
        def is_configured(self): return True
        async def send_fax(self, *args, **kwargs): return {'provider_sid': 123, 'status': 'SUCCESS'}
    monkeypatch.setattr('api.app.outbound_transport.service_from_profile', lambda profile: Service())
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    assert store.get(job)['state'] == 'success'


def test_dispatch_inputs_and_grant_reject_forged_profile(installation, tmp_path):
    from dataclasses import replace
    from api.app.outbound_store import DeliveryConflict
    _, store, _ = installation
    prepared_job(installation, tmp_path)
    claim = replace(store.claim('worker-one'), profile_id='unrelated-profile')
    with pytest.raises(DeliveryConflict):
        store.load_dispatch(claim)
    with pytest.raises(DeliveryConflict):
        store.grant_pdf(claim, url='http://localhost/fax.pdf', token='synthetic',
            expires_at=datetime.utcnow() + timedelta(minutes=1))
