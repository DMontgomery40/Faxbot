"""Synthetic operational analysis: no remote calls or real fax data."""
import asyncio
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import sqlalchemy as sa
from pydantic import ValidationError

from app.config_values import ConfigurationValues


def configured(**changes):
    return ConfigurationValues(**{key.upper(): value for key, value in dict(analysis_enabled=True,
        analysis_model='synthetic-model', analysis_api_key='synthetic-secret', **changes).items()})


def test_analysis_configuration_is_opt_in_secret_and_owner_only():
    from app.access.configuration import owner_only_fields
    values = ConfigurationValues()
    assert getattr(values, 'analysis_enabled', None) is False
    assert values.analysis_interval_hours == 24
    assert 'analysis_api_key' in owner_only_fields()
    assert ConfigurationValues.model_fields['analysis_api_key'].json_schema_extra['secret']
    assert 'synthetic-secret' not in repr(configured())
    for bad in ['http://example.test/v1', 'https://user:pass@example.test', 'https://example.test/?key=secret']:
        with pytest.raises(ValidationError):
            configured(analysis_base_url=bad)
    for bad in [-1, 169]:
        with pytest.raises(ValidationError):
            configured(analysis_interval_hours=bad)


@pytest.fixture
def store(tmp_path):
    from app.schema import upgrade_schema, create_database_engine
    from app.analysis.store import AnalysisStore
    engine = create_database_engine('sqlite:///' + str(tmp_path / 'analysis.db'))
    upgrade_schema(engine)
    result = AnalysisStore(engine)
    yield result
    engine.dispose()


def test_queue_survives_restart_and_single_worker_claims(store):
    from app.analysis.store import AnalysisStore
    now = datetime(2026, 10, 8)
    values = configured(analysis_interval_hours=0)
    store.queue(values, now=now)
    restarted = AnalysisStore(store.engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: restarted.claim(values, now=now), range(2)))
    assert sum(run is not None for run in claimed) == 1
    assert restarted.status(values, now=now)['state'] == 'running'


def test_disable_cancels_queued_and_scheduling_respects_manual(store):
    now = datetime(2026, 10, 8)
    values = configured(analysis_interval_hours=0)
    assert store.claim(values, now=now) is None
    store.queue(values, now=now)
    disabled = values.model_copy(update={'analysis_enabled': False})
    assert store.claim(disabled, now=now) is None
    assert store.status(disabled, now=now)['state'] == 'disabled'
    assert store.claim(values, now=now) is None


def test_failure_preserves_success_and_expired_lease_is_not_replayed(store):
    now = datetime(2026, 10, 8)
    values = configured()
    first = store.claim(values, now=now)
    store.finish(first, summary='Evidence-backed synthetic report', evidence=[{'tool': 'spending', 'data': {}}],
                 usage={'total_tokens': 20}, now=now)
    store.queue(values, now=now + timedelta(hours=1))
    second = store.claim(values, now=now + timedelta(hours=1))
    store.finish(second, error='Analysis provider is unavailable.', now=now + timedelta(hours=1))
    status = store.status(values, now=now + timedelta(hours=1))
    assert status['state'] == 'failed'
    assert status['last_run']['id'] == first['id']
    assert status['last_run']['summary'] == 'Evidence-backed synthetic report'
    assert status['message'] == 'Analysis provider is unavailable.'
    store.queue(values, now=now + timedelta(hours=2))
    abandoned = store.claim(values, now=now + timedelta(hours=2))
    assert store.claim(values, now=now + timedelta(hours=2, minutes=6)) is None
    assert store.status(values, now=now + timedelta(hours=2, minutes=6))['state'] == 'failed'
    store.finish(abandoned, summary='late completion', now=now + timedelta(hours=2, minutes=7))
    assert store.status(values, now=now + timedelta(hours=2, minutes=7))['last_run']['id'] == first['id']


def test_real_tool_loop_records_evidence_and_limits_unknown_tools(store):
    from app.analysis.agent import analyze, AnalysisError
    from app.analysis.evidence import OperationalEvidence
    requests = []
    def response(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'call1', 'type': 'function', 'function': {'name': 'delivery_outcomes', 'arguments': '{}'}}]}}],
                'usage': {'total_tokens': 5}})
        assert json.loads(payload['messages'][-1]['content'])['attempts'] == 0
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'There are no recorded attempts in this period.'}}],
                                         'usage': {'total_tokens': 7}})
    result = asyncio.run(analyze(configured(), OperationalEvidence(store.engine), transport=httpx.MockTransport(response)))
    assert result['summary'].startswith('There are no')
    assert result['usage']['total_tokens'] == 12
    assert result['evidence'][0]['tool'] == 'delivery_outcomes'
    assert 'synthetic-secret' not in json.dumps(requests)
    def unknown(request):
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'tool_calls': [
            {'id': 'bad', 'type': 'function', 'function': {'name': 'send_fax', 'arguments': '{}'}}]}}]})
    with pytest.raises(AnalysisError, match='unsupported tool'):
        asyncio.run(analyze(configured(), OperationalEvidence(store.engine), transport=httpx.MockTransport(unknown)))


def test_provider_errors_and_oversized_responses_are_sanitized(store):
    from app.analysis.agent import analyze, AnalysisError
    from app.analysis.evidence import OperationalEvidence
    for response in [httpx.Response(401, text='synthetic-secret'), httpx.Response(200, text='x' * 270000)]:
        with pytest.raises(AnalysisError) as error:
            asyncio.run(analyze(configured(), OperationalEvidence(store.engine),
                                transport=httpx.MockTransport(lambda request: response)))
        assert 'synthetic-secret' not in str(error.value)


def test_worker_rechecks_disabled_configuration_before_provider_call(store):
    from app.analysis.worker import AnalysisWorker
    enabled = configured(analysis_interval_hours=0)
    current = [enabled]
    store.queue(enabled)
    async def runner(values, source, *, allowed):
        current[0] = enabled.model_copy(update={'analysis_enabled': False})
        assert await allowed() is False
        from app.analysis.agent import AnalysisError
        raise AnalysisError('Analysis was disabled or its configuration changed. Refresh with the current settings.')
    worker = AnalysisWorker(store, lambda: current[0], runner=runner)
    asyncio.run(worker.step())
    assert store.status(current[0])['state'] == 'disabled'


def test_disabled_analysis_sends_nothing_but_explicit_test_is_synthetic(store):
    from app.analysis.agent import analyze, test_connection, AnalysisError
    from app.analysis.evidence import OperationalEvidence
    values = configured().model_copy(update={'analysis_enabled': False})
    def reject(request):
        pytest.fail('Disabled analysis sent an HTTP request')
    with pytest.raises(AnalysisError, match='disabled'):
        asyncio.run(analyze(values, OperationalEvidence(store.engine), transport=httpx.MockTransport(reject)))
    captured = []
    def synthetic(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
    assert asyncio.run(test_connection(values, transport=httpx.MockTransport(synthetic)))['ok']
    assert captured[0]['messages'] == [{'role': 'user', 'content': 'Synthetic connection test. Reply with OK.'}]


def test_key_is_encrypted_in_configuration_and_masked_in_settings(store, tmp_path):
    from app.config_store import ConfigurationStore
    from app.config_views import project_admin_settings
    settings = ConfigurationStore(store.engine, tmp_path / 'installation.key')
    first = settings.initialize(configured(), actor='synthetic-test')
    saved = settings.read()
    assert saved.active.values.analysis_api_key == 'synthetic-secret'
    with store.engine.connect() as connection:
        assert 'synthetic-secret' not in connection.execute(sa.text('SELECT envelope FROM configuration_revisions')).scalar_one()
    assert project_admin_settings(first)['analysis']['api_key'] == '***'
    assert project_admin_settings(first)['analysis']['configured'] is True


def test_http_owner_guard_queue_and_audit(store, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.tests.test_access_configuration_writes import ConfigurationWorld
    from api.tests.test_access_policy import NOW
    from api.app.analysis.http import router
    from api.app.access.http import RequestIdentity, require_identity, access_error_response
    from api.app.access.types import AccessError
    from api.app.access import route_policy
    from api.app import config_store
    monkeypatch.setattr(route_policy, 'utcnow', lambda: NOW)
    monkeypatch.setattr(config_store, '_utc_now', lambda: NOW)
    world = ConfigurationWorld(store.engine, tmp_path)
    world.grant(['settings:read', 'settings:write'])
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(AccessError, access_error_response)
    app.state.access_runtime = SimpleNamespace(store=world.store, control=world.control)
    app.state.configuration_runtime = SimpleNamespace(manager=SimpleNamespace(store=world.configuration))
    app.dependency_overrides[require_identity] = lambda: RequestIdentity(world.actor, 'test')
    with TestClient(app) as client:
        assert client.get('/analysis').status_code == 200
        assert client.post('/analysis/run').status_code == 403
        assert client.post('/analysis/test').status_code == 403
        assert store.status(configured())['state'] == 'idle'
        from api.app.access.mutation_types import MutationDeniedError
        with pytest.raises(MutationDeniedError):
            world.manager.patch_authorized(world.initial, {'analysis_api_key': 'synthetic-denied'},
                                            principal=world.actor, control=world.control)
        assert world.configuration.read().active.values.analysis_api_key == ''
        world.assignment('editor', 'role_owner')
        assert client.post('/analysis/test').json()['ok'] is False
        candidate = world.initial.desired.values.with_patch({'analysis_enabled': True, 'analysis_model': 'synthetic-model',
                                                            'analysis_api_key': 'synthetic-secret', 'analysis_interval_hours': 0})
        world.configuration.apply(world.initial, candidate, restart_required=False, actor='synthetic-test')
        response = client.post('/analysis/run')
        assert response.status_code == 202
        assert response.json()['state'] == 'queued'
        assert response.headers['cache-control'] == 'no-store'
        assert 'synthetic-secret' not in response.text
        assert client.get('/analysis').json()['state'] == 'queued'
    with store.engine.connect() as connection:
        details = connection.execute(sa.select(world.tables['access_audit'].c.details)).scalars().all()
    assert any('analysis.refresh' in row for row in details)
    assert any('analysis.test' in row for row in details)


def test_saved_analysis_freshness_tracks_age_and_provider_settings(store):
    values = configured(analysis_interval_hours=0)
    now = datetime(2026, 10, 8)
    store.queue(values, now=now)
    run = store.claim(values, now=now)
    store.finish(run, summary='Synthetic report', now=now)
    assert not store.status(values, now=now)['stale']
    assert store.status(values, now=now + timedelta(hours=25))['stale']
    changed = values.model_copy(update={'analysis_base_url': 'https://compatible.invalid/v1'})
    assert store.status(changed, now=now)['stale']
    assert 'config_signature' not in store.status(values, now=now)['last_run']


def test_updated_interval_takes_effect_without_waiting_for_old_schedule(store):
    now = datetime(2026, 10, 8)
    values = configured()
    run = store.claim(values, now=now)
    store.finish(run, summary='Synthetic report', now=now)
    changed = values.model_copy(update={'analysis_interval_hours': 1})
    assert store.claim(changed, now=now + timedelta(hours=2)) is not None


def test_openai_uses_current_token_parameter_for_synthetic_test():
    from app.analysis.agent import test_connection
    requests = []
    def response(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]})
    assert asyncio.run(test_connection(configured(), transport=httpx.MockTransport(response)))['ok']
    assert 'max_completion_tokens' in requests[0] and 'max_tokens' not in requests[0]
