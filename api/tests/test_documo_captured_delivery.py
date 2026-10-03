"""Internal captured-account/delivery invariants, never route acceptance."""
import httpx
import pytest
import sqlalchemy as sa

from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_polling import OutboundPoller
from api.app.outbound_transport import CapturedTransport
from api.app.outbound_worker import OutboundWorker
from api.tests.test_outbound_store import installation, accept
from api.tests.test_outbound_transport import Runtime
from api.tests.test_schema import database


SID = '052df0f3-f2d1-4ed6-86c1-02893dc48a94'


@pytest.mark.asyncio
@pytest.mark.parametrize('ambiguous', [False, True])
async def test_documo_original_account_survives_rotation_without_resubmission(
        installation, tmp_path, monkeypatch, ambiguous):
    configuration, store, snapshot = installation
    captured = ProviderConfiguration('documo', credentials={'api_key': 'original-synthetic-key'},
        settings={'base_url': 'https://original.invalid', 'sandbox': False})
    snapshot = configuration.apply(snapshot, snapshot.active.values.with_patch({'fax_data_dir': str(tmp_path)}),
        actor='test', restart_required=False, providers={'outbound': captured})
    job = accept((configuration, store, snapshot))
    document = b'%PDF-internal-synthetic-document'
    (tmp_path / (job + '.pdf')).write_bytes(document)
    configuration.apply(snapshot, snapshot.active.values, actor='test', restart_required=False,
        providers={'outbound': ProviderConfiguration('documo', credentials={'api_key': 'replacement-key'},
            settings={'base_url': 'https://replacement.invalid', 'sandbox': False})})
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.host == 'original.invalid'
        assert request.headers['Authorization'] == 'Basic original-synthetic-key'
        if request.method == 'POST':
            assert request.url.path == '/v1/faxes'
            assert store.get(job)['state'] == 'submitting'
            assert document in request.content and b'+12025550123' in request.content
            with configuration.engine.connect() as connection:
                record = connection.execute(sa.select(configuration.jobs).where(
                    configuration.jobs.c.id == job)).mappings().one()
                assert not record['pdf_url'] and not record['pdf_token']
            if ambiguous:
                raise httpx.ReadTimeout('private-response-failure', request=request)
            return httpx.Response(200, json={'messageId': SID})
        assert request.method == 'GET' and request.url.path == f'/v1/fax/{SID}/info'
        return httpx.Response(200, json={'messageId': SID, 'status': 'success'})
    original_client = httpx.AsyncClient
    def client(**kwargs):
        kwargs['transport'] = httpx.MockTransport(handle)
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)
    worker = OutboundWorker(store, CapturedTransport(store, Runtime()))
    await worker.step()
    assert len(requests) == 1
    if ambiguous:
        assert store.get(job)['state'] == 'reconciliation_required'
        view = store.operator_view(job)
        assert view['can_bind_provider_identity']
        with pytest.raises(ValueError, match='Invalid provider identity'):
            store.bind_provider_identity(job, expected_version=view['version'],
                provider_sid='not-a-documo-uuid', actor='admin')
        assert store.operator_view(job)['version'] == view['version']
        store.bind_provider_identity(job, expected_version=view['version'], provider_sid=SID.upper(), actor='admin')
    else:
        assert store.get(job)['state'] == 'in_progress'
    await OutboundPoller(store).refresh(job)
    assert store.get(job)['state'] == 'success'
    await worker.step()
    assert [request.method for request in requests] == ['POST', 'GET']
    assert configuration.outbound_profile(job).configuration.credentials['api_key'] == 'original-synthetic-key'


@pytest.mark.parametrize('historical_case', [False, True])
def test_documo_uuid_casing_cannot_attach_another_jobs_identity(installation, historical_case):
    from api.app.outbound_store import DeliveryConflict
    from api.tests.test_outbound_reconciliation import uncertain
    configuration, store, _ = installation
    provider = ProviderConfiguration('documo', credentials={'api_key': 'synthetic-key'},
        settings={'base_url': 'https://original.invalid', 'sandbox': False})
    first, _, snapshot = uncertain(installation, profile=provider)
    store.bind_provider_identity(first, expected_version=store.get(first)['version'],
        provider_sid=SID, actor='admin')
    if historical_case:
        with configuration.engine.begin() as connection:
            connection.execute(store.attempts.update().where(store.attempts.c.job_id == first).values(provider_sid=SID.upper()))
            connection.execute(configuration.jobs.update().where(configuration.jobs.c.id == first).values(provider_sid=SID.upper()))
    second, _, _ = uncertain((configuration, store, snapshot))
    with pytest.raises(DeliveryConflict, match='already belongs'):
        store.bind_provider_identity(second, expected_version=store.get(second)['version'],
            provider_sid=SID.upper() if not historical_case else SID, actor='admin')
    assert store.operator_view(second)['can_bind_provider_identity']
