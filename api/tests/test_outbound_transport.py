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


class DestinationService:
    """A provider double that records the destination each adapter call receives."""
    def __init__(self, calls):
        self.calls = calls
    def is_configured(self):
        return True
    async def send_fax(self, to, url, job_id, *, attempt_id):
        self.calls.append(to)
        return {'provider_sid': 'remote-one', 'status': 'queued'}
    async def send_fax_file(self, to, path, *, uuid=None):
        self.calls.append(to)
        return {'provider_sid': 'remote-one', 'id': 'remote-one', 'status': 'queued'}


def accepted_with(installation, tmp_path, provider, to_number, country):
    from datetime import datetime
    from uuid import uuid4
    configuration, store, snapshot = installation
    snapshot = configuration.apply(snapshot, snapshot.active.values.with_patch(
        {'fax_data_dir': str(tmp_path), 'fax_default_country': country}), actor='test', restart_required=False,
        providers={'outbound': ProviderConfiguration(provider, credentials={'api_key': 'synthetic'})})
    job, now = uuid4().hex, datetime.utcnow()
    configuration.accept_outbound(snapshot.active, {'id': job, 'to_number': to_number, 'file_name': 'a.pdf',
        'tiff_path': '', 'status': 'queued', 'pages': 1, 'created_at': now, 'updated_at': now})
    (tmp_path / (job + '.pdf')).write_bytes(b'%PDF-synthetic-internal-seam')
    # A later country change must not redirect the accepted fax.
    later = configuration.apply(snapshot, snapshot.active.values.with_patch(
        {'fax_default_country': 'US' if country != 'US' else 'GB'}), actor='test', restart_required=False)
    return job, later


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['phaxio', 'signalwire', 'sinch', 'documo', 'humblefax'])
@pytest.mark.parametrize('stored, country, expected', [
    ('+441782684953', 'GB', '+441782684953'),   # accepted after this change: already canonical
    ('01782684953', 'GB', '+441782684953'),     # accepted before it: the entered text, read once for GB
    ('3035550123', 'US', '+13035550123'),
])
async def test_every_adapter_receives_the_one_accepted_destination(
        installation, tmp_path, monkeypatch, provider, stored, country, expected):
    _, store, _ = installation
    job, _ = accepted_with(installation, tmp_path, provider, stored, country)
    calls = []
    monkeypatch.setattr('api.app.outbound_transport.service_from_profile', lambda profile: DestinationService(calls))
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    if provider == 'humblefax' and expected.startswith('+44'):
        # HumbleFax sends only to US and Canadian numbers: refused before submission.
        assert calls == [] and store.get(job)['state'] == 'failed'
        return
    assert calls == [expected]
    assert store.get(job)['state'] == 'in_progress'


@pytest.mark.asyncio
async def test_unreadable_pre_change_destination_fails_before_any_provider_call(installation, tmp_path, monkeypatch):
    _, store, _ = installation
    job, _ = accepted_with(installation, tmp_path, 'phaxio', '123456', 'US')
    calls = []
    monkeypatch.setattr('api.app.outbound_transport.service_from_profile', lambda profile: DestinationService(calls))
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    assert calls == [] and store.get(job)['state'] == 'failed'


# -- the SSL Fax engine (HylaFAX+) on the SIP trunk --------------------------------------------------

ENGINE_SECRET = 'synthetic-inbound-secret-0123456789'


class EngineAmi:
    """Faxbot's manager connection as the engine path uses it; records what was asked."""
    def __init__(self, lines=2):
        import asyncio
        self._connected = asyncio.Event()
        self._connected.set()
        self.lines, self.originated, self.plans, self.removed = lines, [], {}, []

    async def originate_sendfax(self, job_id, dest, tiff_path, *, attempt_id=None, call=None):
        self.originated.append((job_id, dest, attempt_id))
        self.calls = getattr(self, 'calls', []) + [call]

    async def iax_lines_ready(self, prefix):
        return self.lines

    async def db_put(self, family, key, value):
        self.plans[key] = value

    async def db_del(self, family, key):
        self.removed.append(key)


class EngineJob:
    def __init__(self, tag, events):
        self.tag, self.events, self.submitted, self.engine_job = tag, events, False, '7'

    def submit(self):
        self.submitted = True
        self.events.append('submit')
        return self.engine_job

    def discard(self):
        self.events.append('discard')

    def close(self):
        self.events.append('close')


def sip_job(installation, tmp_path, *, engine_running):
    from api.app import sip_trunk
    configuration, store, snapshot = installation
    values = snapshot.active.values.with_patch({
        'fax_data_dir': str(tmp_path), 'sip_trunk_preset': 'telnyx', 'sip_trunk_username': 'faxbotuser',
        'sip_trunk_password': 'Synthetic-Password-1', 'sip_trunk_caller_id': '+15555550100',
        'asterisk_inbound_secret': ENGINE_SECRET})
    snapshot = configuration.apply(snapshot, values, actor='test', restart_required=False,
        providers={'outbound': ProviderConfiguration('sip', traits={'requires_tiff': True})})
    job = accept((configuration, store, snapshot))
    (tmp_path / (job + '.pdf')).write_bytes(b'%PDF-synthetic')
    (tmp_path / (job + '.tiff')).write_bytes(b'II*\x00synthetic')
    if engine_running:
        sip_trunk.write_asterisk_configuration(snapshot.active.values)
        (tmp_path / 'hylafax-out').mkdir(exist_ok=True)
        (tmp_path / 'hylafax-out' / 'engine.status').write_text('{"state": "running", "lines": 2}')
        (tmp_path / 'asterisk' / 'iax.conf.started').write_bytes((tmp_path / 'asterisk' / 'iax.conf').read_bytes())
    return job


@pytest.mark.asyncio
async def test_a_trunk_fax_goes_to_the_ssl_fax_engine_and_is_submitted_after_the_durable_marker(
        installation, tmp_path, monkeypatch):
    from api.app import hylafax_engine
    _, store, _ = installation
    job = sip_job(installation, tmp_path, engine_running=True)
    ami, events = EngineAmi(), []

    def create_job(values, *, tag, job_id, attempt_id, tiff_path, header='', settings=None, station=None):
        # Before the durable marker: the plan is stored and nothing is dialed.
        assert store.get(job_id)['state'] != 'submitting' and tag in ami.plans
        events.append('create')
        return EngineJob(tag, events)
    monkeypatch.setattr(hylafax_engine, 'create_job', create_job)
    await OutboundWorker(store, CapturedTransport(store, Runtime(), ami=ami)).step()
    assert events == ['create', 'submit', 'close'] and ami.originated == []
    (tag, plan), = ami.plans.items()
    assert plan.split('/')[2] == job and ami.removed == []
    assert store.get(job)['state'] == 'in_progress'


@pytest.mark.asyncio
async def test_the_built_in_engine_places_the_call_when_the_ssl_fax_engine_is_not_running(installation, tmp_path):
    _, store, _ = installation
    job = sip_job(installation, tmp_path, engine_running=False)
    ami = EngineAmi()
    await OutboundWorker(store, CapturedTransport(store, Runtime(), ami=ami)).step()
    assert [call[0] for call in ami.originated] == [job] and ami.plans == {}


@pytest.mark.asyncio
async def test_an_engine_that_refuses_the_job_leaves_no_plan_and_the_built_in_engine_sends(
        installation, tmp_path, monkeypatch):
    from api.app import hylafax_engine
    _, store, _ = installation
    job = sip_job(installation, tmp_path, engine_running=True)
    ami = EngineAmi()

    def refuse(*args, **kwargs):
        raise hylafax_engine.EngineError('Faxbot could not reach the SSL Fax engine.')
    monkeypatch.setattr(hylafax_engine, 'create_job', refuse)
    await OutboundWorker(store, CapturedTransport(store, Runtime(), ami=ami)).step()
    assert [call[0] for call in ami.originated] == [job]
    assert list(ami.plans) == ami.removed and len(ami.removed) == 1
    assert store.get(job)['state'] == 'in_progress'
