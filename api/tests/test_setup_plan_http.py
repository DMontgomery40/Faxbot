"""Guided setup over the real HTTPS stack: preview, apply, staleness and two mailboxes' jurisdictions.

Every collaborator here is the real one: the configuration's authorized write,
the sending rules' draft, check and publish, the route planner and the shared
predictor (no stand-in prices). History is synthetic rows in the delivery tables.
"""
from datetime import datetime, timedelta
import json
import uuid

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from app.setup_plan.packs import target_document
from app.setup_plan.store import PlanStore
from api.tests.test_schema import snapshot


BOOTSTRAP = 'synthetic-setup-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
CLINIC = '+12025550123'
NOW = datetime.utcnow().replace(microsecond=0)


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_ROUTES': 'sip, signalwire',
                        'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def _engine():
    return main.app.state.configuration_runtime.manager.store.engine


def _values():
    return main.app.state.configuration_runtime.manager.store.read().desired.values


def _sent(number, route, outcome, *, pages=2, ago=timedelta(days=2)):
    """One finished attempt to ``number`` on ``route``: an accepted fax, its attempt and its cost row."""
    store = main.app.state.configuration_runtime.manager.store
    job, attempt, at = uuid.uuid4().hex, uuid.uuid4().hex, NOW - ago
    store.accept_outbound(store.read().active, {'id': job, 'to_number': number, 'file_name': 'synthetic.txt',
                                                'tiff_path': '', 'status': 'queued', 'pages': pages,
                                                'created_at': at, 'updated_at': at})
    metadata = sa.MetaData()
    attempts = sa.Table('outbound_attempts', metadata, autoload_with=_engine())
    costs = sa.Table('delivery_attempt_costs', metadata, autoload_with=_engine())
    with _engine().begin() as connection:
        connection.execute(attempts.insert().values(id=attempt, job_id=job, sequence=1, phase=outcome, created_at=at,
                                                    submitted_at=at, completed_at=at + timedelta(seconds=60)))
        connection.execute(costs.insert().values(
            id=attempt, job_id=job, destination=number, route=route, route_reason='cheapest', provider_id=route,
            started_at=at, ended_at=at + timedelta(seconds=60), estimated_cost_micros=10_000, currency='USD',
            billing_checks=0, outcome=outcome, created_at=at, updated_at=at))


def _failing_first_route(client):
    """Phaxio, which you chose for the clinic, failed four times; SignalWire delivered three."""
    for _ in range(4):
        _sent(CLINIC, 'phaxio', 'failed')
    for _ in range(3):
        _sent(CLINIC, 'signalwire', 'success')
    chosen = client.patch(f'/routing/destinations/{CLINIC}', headers=ADMIN, json={'preferred_route': 'phaxio'})
    assert chosen.status_code == 200, chosen.text


def _preview(client, **context):
    response = client.post('/setup/plans', headers=ADMIN, json=context)
    assert response.status_code == 200, response.text
    return response.json()


def _items(plan):
    return {item['key']: item for pack in plan['packs'] for item in pack['items']}


def _mailbox(client, label):
    version = client.get('/auth/me', headers=ADMIN).json()['policy_version']
    created = client.post('/access/mailboxes', headers=ADMIN,
                          json={'label': label, 'enabled': True, 'expected_policy_version': version})
    assert created.status_code == 200, created.text
    return created.json()['mailbox']['id']


def test_a_preview_compiles_packs_from_history_sends_nothing_and_changes_nothing(client, monkeypatch):
    _failing_first_route(client)
    before = snapshot(_engine())

    def refuse(*args, **kwargs):
        raise AssertionError('A preview must not reach the network.')
    monkeypatch.setattr(httpx.Client, 'send', refuse)
    monkeypatch.setattr(httpx.AsyncClient, 'send', refuse)
    plan = _preview(client, organization_name='Synthetic Clinic', country='US')
    after = snapshot(_engine())
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    assert changed == ['setup_plans'], changed
    assert len(after['setup_plans']) == 1
    assert plan['number'] == 1 and len(plan['revision']) == 64 and plan['applications'] == []
    items = _items(plan)
    fallback = items[f'reliability.fallback.{CLINIC}']
    assert fallback['kind'] == 'rule' and fallback['selected'] is True and fallback['blocked'] is None
    assert fallback['rule']['then'] == {'try_in_order': ['signalwire', 'phaxio']}
    assert 'failed 4 of its 4 faxes' in fallback['sentence'] and 'because you chose it' in fallback['sentence']
    header = items['compliance.header']
    assert header['kind'] == 'setting' and header['changes'] == {'fax_header': 'Synthetic Clinic'}
    assert {entry['key'] for entry in plan['missing']} >= {'retention', 'templates'}
    assert 'targets' not in plan and plan['checks']['organization']['warnings'] == []
    assert client.get('/setup/plans/1', headers=ADMIN).json()['packs'] == plan['packs']
    latest = client.get('/setup/plans/latest', headers=ADMIN).json()
    assert latest['plan']['number'] == 1 and latest['mailboxes'] == []


def test_apply_publishes_exactly_the_previewed_rules_and_saves_the_settings(client):
    _failing_first_route(client)
    plan = _preview(client, organization_name='Synthetic Clinic', country='US')
    stored = PlanStore(_engine()).get(1)['plan']
    applied = client.post('/setup/plans/1/apply', headers=ADMIN, json={'expected_revision': plan['revision']})
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result['outcome'] == 'applied' and result['restart_required'] is False
    assert [step['part'] for step in result['steps']] == ['settings', 'organization']
    chosen = [item for item in _items(plan).values() if item['selected']]
    assert sorted(result['items']) == sorted(item['key'] for item in chosen)
    rules = client.get('/routing/rules', headers=ADMIN).json()
    expected = target_document(stored['targets']['organization']['base'],
                               [item for item in chosen if item['kind'] == 'rule'])
    assert rules['active']['document'] == expected and rules['draft'] is None
    assert rules['active']['note'] == 'From setup plan 1'
    assert _values().fax_header == 'Synthetic Clinic'
    assert result['plan']['applications'][0]['outcome'] == 'applied'
    # The same plan is never applied twice; a new preview finds the rule in effect.
    again = client.post('/setup/plans/1/apply', headers=ADMIN, json={'expected_revision': plan['revision']})
    assert again.status_code == 400 and 'not applied yet' in again.json()['detail']
    later = _items(_preview(client, organization_name='Synthetic Clinic', country='US'))
    assert later[f'reliability.fallback.{CLINIC}']['kind'] == 'in_effect'
    assert later['compliance.header']['kind'] == 'in_effect'


def test_apply_can_take_some_suggestions_and_leaves_the_rest(client):
    _failing_first_route(client)
    plan = _preview(client, organization_name='Synthetic Clinic', country='US')
    applied = client.post('/setup/plans/1/apply', headers=ADMIN,
                          json={'expected_revision': plan['revision'], 'items': ['compliance.header']})
    assert applied.status_code == 200, applied.text
    assert applied.json()['items'] == ['compliance.header']
    assert client.get('/routing/rules', headers=ADMIN).json()['active'] is None
    # The settings part is done; the rules part was never touched, so the rest of the plan still applies.
    rest = client.post('/setup/plans/1/apply', headers=ADMIN, json={'expected_revision': plan['revision']})
    assert rest.status_code == 200, rest.text
    assert rest.json()['items'] == [f'reliability.fallback.{CLINIC}']
    steps = client.post('/setup/plans/1/apply', headers=ADMIN,
                        json={'expected_revision': plan['revision'], 'items': ['cost.fax-friendly']})
    assert steps.status_code == 400 and 'Faxbot can’t apply it' in steps.json()['detail']


def test_a_stale_apply_is_refused_before_anything_is_written(client):
    _failing_first_route(client)
    plan = _preview(client, organization_name='Synthetic Clinic', country='US')
    wrong = client.post('/setup/plans/1/apply', headers=ADMIN, json={'expected_revision': 'f' * 64})
    assert wrong.status_code == 409
    # Someone saves a setting after the preview: the plan's settings are stale.
    current = client.get('/admin/settings', headers=ADMIN).json()['_meta']['desired_revision_id']
    saved = client.put('/admin/settings', headers=ADMIN,
                       json={'route_min_success_percent': 70, 'expected_revision_id': current})
    assert saved.status_code == 200, saved.text
    stale = client.post('/setup/plans/1/apply', headers=ADMIN, json={'expected_revision': plan['revision']})
    assert stale.status_code == 409, stale.text
    assert client.get('/routing/rules', headers=ADMIN).json()['active'] is None
    assert _values().fax_header == 'Faxbot'
    assert client.get('/setup/plans/1', headers=ADMIN).json()['applications'] == []
    # Someone publishes rules after a new preview: its rules are stale, even applied alone.
    fresh = _preview(client, organization_name='Synthetic Clinic', country='US')
    draft = client.put('/routing/rules/draft', headers=ADMIN, json={'document': {'format': 1, 'limits': [], 'routes': [
        {'id': 'other', 'name': 'Other', 'on': True, 'when': {}, 'then': {'automatic': True}}]}, 'expected_version': 0})
    published = client.post('/routing/rules/publish', headers=ADMIN,
                            json={'expected_active_revision': None, 'expected_draft_version': draft.json()['version']})
    assert published.status_code == 200, published.text
    refused = client.post(f"/setup/plans/{fresh['number']}/apply", headers=ADMIN,
                          json={'expected_revision': fresh['revision'], 'items': [f'reliability.fallback.{CLINIC}']})
    assert refused.status_code == 409 and 'Preview again' in refused.json()['detail']
    assert client.get('/routing/rules', headers=ADMIN).json()['active']['number'] == 1


def test_an_unpublished_draft_blocks_the_rules_and_says_so(client):
    _failing_first_route(client)
    client.put('/routing/rules/draft', headers=ADMIN,
               json={'document': {'format': 1, 'limits': [], 'routes': []}, 'expected_version': 0})
    plan = _preview(client)
    fallback = _items(plan)[f'reliability.fallback.{CLINIC}']
    assert fallback['blocked'] and fallback['selected'] is False
    blocking = [entry for entry in plan['missing'] if entry['status'] == 'blocking']
    assert [entry['key'] for entry in blocking] == ['draft.organization']
    refused = client.post('/setup/plans/1/apply', headers=ADMIN,
                          json={'expected_revision': plan['revision'], 'items': [fallback['key']]})
    assert refused.status_code == 400
    # The draft is still the administrator's own, untouched.
    assert client.get('/routing/rules', headers=ADMIN).json()['draft']['document']['routes'] == []


def test_two_mailboxes_keep_their_own_jurisdictions(client):
    denver, leeds = _mailbox(client, 'Denver'), _mailbox(client, 'Leeds')
    latest = client.get('/setup/plans/latest', headers=ADMIN).json()
    assert latest['plan'] is None and {box['name'] for box in latest['mailboxes']} == {'Denver', 'Leeds'}
    plan = _preview(client, organization_name='Example Health',
                    mailboxes={denver: {'country': 'US'}, leeds: {'country': 'GB'}})
    views = {view['name']: view for view in plan['mailboxes']}
    assert views['Denver']['items'] == ['compliance.header'] and views['Leeds']['items'] == []
    assert views['Leeds']['missing'] == ['reviewed-rules.GB']
    assert _items(plan)['compliance.header']['applies_to'] == [denver]
    assert plan['context']['mailboxes'] == {denver: {'country': 'US'}, leeds: {'country': 'GB'}}
    unknown = client.post('/setup/plans', headers=ADMIN, json={'mailboxes': {'gone': {'country': 'US'}}})
    assert unknown.status_code == 400 and 'no longer exists' in unknown.json()['detail']
    bad = client.post('/setup/plans', headers=ADMIN, json={'country': 'XX'})
    assert bad.status_code == 400


def test_reading_needs_settings_read_and_previewing_needs_settings_write(client):
    assert client.get('/setup/plans/latest').status_code == 401
    assert client.post('/setup/plans', json={}).status_code == 401
    assert client.get('/setup/plans/7', headers=ADMIN).status_code == 404
    listed = client.get('/setup/plans', headers=ADMIN).json()
    assert listed == {'plans': []}
    plan = _preview(client)
    assert json.loads(json.dumps(plan))['number'] == 1
    assert client.get('/setup/plans', headers=ADMIN).json()['plans'][0]['number'] == 1
