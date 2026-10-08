"""Sending rules in delivery over the real HTTPS stack: send fields, held faxes, why a route, quotes.

Sending is turned off in these installations (FAX_DISABLED), so every accepted
fax stays held and nothing is ever submitted to a provider. Numbers are synthetic.
"""
import pytest
from fastapi.testclient import TestClient

from app import main


BOOTSTRAP = 'synthetic-delivery-rules-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
US = '+13035550100'
HUMBLEFAX = {'provider_id': 'humblefax', 'label': 'HumbleFax', 'monthly_fee': '10', 'captured_on': '2026-10-05'}
SIGNALWIRE = {'provider_id': 'signalwire', 'label': 'SignalWire', 'per_minute': '0.0095', 'captured_on': '2026-10-05'}
PHAXIO = {'provider_id': 'phaxio', 'label': 'Phaxio', 'per_page': '0.07', 'captured_on': '2026-10-05'}


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_ROUTES': 'humblefax, signalwire',
                        'HUMBLEFAX_ACCESS_KEY': 'synthetic-access', 'HUMBLEFAX_SECRET_KEY': 'synthetic-secret',
                        'SIGNALWIRE_SPACE_URL': 'example.signalwire.com', 'SIGNALWIRE_PROJECT_ID': 'project-1',
                        'SIGNALWIRE_API_TOKEN': 'synthetic-token', 'SIGNALWIRE_FAX_FROM_E164': '+13035550111',
                        'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        saved = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [HUMBLEFAX, SIGNALWIRE, PHAXIO]})
        assert saved.status_code == 200, saved.text
        yield client


def rule(rule_id, then, when=None):
    return {'id': rule_id, 'name': f'Rule {rule_id}', 'on': True, 'when': when or {}, 'then': then}


def publish(client, document):
    current = client.get('/routing/rules', headers=ADMIN).json()
    saved = client.put('/routing/rules/draft', headers=ADMIN, json={
        'document': document, 'expected_version': current['draft']['version'] if current['draft'] else 0})
    assert saved.status_code == 200, saved.text
    active = current['active']['number'] if current['active'] else None
    published = client.post('/routing/rules/publish', headers=ADMIN, json={
        'expected_active_revision': active, 'expected_draft_version': saved.json()['version'], 'note': 'Synthetic'})
    assert published.status_code == 200, published.text


def send(client, to=US, headers=ADMIN, **fields):
    data = {'to': to, **{key: value for key, value in fields.items() if value is not None}}
    return client.post('/fax', headers=headers, data=data, files={'file': ('note.txt', b'Synthetic page\n',
                                                                          'text/plain')})


def scoped_key(client, scopes):
    response = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': scopes})
    assert response.status_code == 200, response.text
    return {'X-API-Key': response.json()['token']}


def test_a_held_fax_is_listed_approved_and_explained(client):
    publish(client, {'format': 1, 'limits': [rule('l-all', {'hold_for_approval': {}})],
                     'routes': [rule('r-sw', {'use': 'signalwire'})]})
    sent = send(client)
    assert sent.status_code == 202, sent.text
    job = sent.json()['id']
    listed = client.get('/routing/holds', headers=ADMIN).json()
    assert listed['can_approve'] is True
    hold = listed['holds'][0]
    assert (hold['job_id'], hold['kind'], hold['to_number'], hold['can_decide']) == (job, 'approval', US, True)
    assert hold['reason'] == 'Waits for approval: the rule ‘Rule l-all’ matched.'
    route = client.get(f'/routing/faxes/{job}/route', headers=ADMIN).json()
    assert route['hold']['id'] == hold['id']
    assert route['sentence'] == 'Waits for approval: the rule ‘Rule l-all’ matched. Organization rules version 1.'
    assert [step['rule_id'] for step in route['trace']] == ['l-all', 'r-sw']
    stale = client.post(f"/routing/holds/{hold['id']}/approve", headers=ADMIN, json={'version': hold['version'] + 1})
    assert stale.status_code == 409
    approved = client.post(f"/routing/holds/{hold['id']}/approve", headers=ADMIN, json={'version': hold['version']})
    assert approved.status_code == 200, approved.text
    assert approved.json()['sentence'] == 'Approved. The fax is no longer held.'
    assert client.get('/routing/holds', headers=ADMIN).json()['holds'] == []


def test_refusing_needs_approve_faxes_and_fails_the_fax_with_the_reason(client):
    publish(client, {'format': 1, 'limits': [rule('l-all', {'hold_for_approval': {}})]})
    job = send(client).json()['id']
    hold = client.get('/routing/holds', headers=ADMIN).json()['holds'][0]
    sender = scoped_key(client, ['fax:send', 'fax:read'])
    assert client.post(f"/routing/holds/{hold['id']}/refuse", headers=sender,
                       json={'version': hold['version'], 'reason': 'x'}).status_code == 403
    # The sender sees no one else's holds and cannot decide.
    assert client.get('/routing/holds', headers=sender).json() == {'holds': [], 'can_approve': False}
    refused = client.post(f"/routing/holds/{hold['id']}/refuse", headers=ADMIN,
                          json={'version': hold['version'], 'reason': 'wrong recipient'})
    assert refused.status_code == 200, refused.text
    fax = client.get(f'/fax/{job}', headers=ADMIN).json()
    assert fax['status'] == 'failed' and fax['error'].endswith('wrong recipient')


def test_a_fax_no_rule_allows_waits_with_its_sentence_and_options(client):
    publish(client, {'format': 1, 'limits': [rule('l-cap', {'cap_cost': {'currency': 'USD', 'amount': '0.01'}}),
                                             rule('l-no-hf', {'never': ['humblefax']})],
                     'routes': [rule('r-two', {'try_in_order': ['phaxio', 'humblefax']})]})
    sent = send(client)
    assert sent.status_code == 202, sent.text
    hold = client.get('/routing/holds', headers=ADMIN).json()['holds'][0]
    assert hold['kind'] == 'no_route'
    assert hold['reason'].startswith('No account is estimated to cost less than the $0.01 cap')
    assert [item['account'] for item in hold['options']] == ['phaxio']
    assert hold['options'][0]['reason'].startswith('Phaxio is skipped: its estimate, $0.07, is over the $0.01 cap')
    assert hold['not_offered'] == ['HumbleFax is skipped: the rule ‘Rule l-no-hf’ applies.']
    anyway = client.post(f"/routing/holds/{hold['id']}/approve", headers=ADMIN,
                         json={'version': hold['version'], 'account': 'humblefax'})
    assert anyway.status_code == 400  # a rule forbids it outright: never offered
    anyway = client.post(f"/routing/holds/{hold['id']}/approve", headers=ADMIN,
                         json={'version': hold['version'], 'account': 'phaxio'})
    assert anyway.status_code == 200 and anyway.json()['sentence'] == 'Approved. The fax goes by Phaxio.'


def test_the_send_form_fields_are_checked_and_matched_by_rules(client):
    publish(client, {'format': 1, 'labels': ['legal'], 'workflows': [{'key': 'referrals', 'name': 'Referrals'}],
                     'routes': [rule('r-legal', {'use': 'signalwire'}, {'labels': ['legal']})]})
    assert send(client, workflow='nope').status_code == 400
    unknown = send(client, labels=['secret'])
    assert unknown.status_code == 400 and 'secret is not a label' in unknown.json()['detail']
    assert send(client, mailbox='no-such-mailbox').status_code == 400
    sent = send(client, labels=['legal'], workflow='referrals')
    assert sent.status_code == 202, sent.text
    route = client.get(f"/routing/faxes/{sent.json()['id']}/route", headers=ADMIN).json()
    assert route['sentence'].startswith('Goes by SignalWire because the rule ‘Rule r-legal’ matched')
    context = client.get('/auth/context', headers=ADMIN)
    assert context.status_code == 200, context.text
    assert context.json()['send']['labels'] == ['legal']
    assert context.json()['send']['workflows'] == [{'key': 'referrals', 'name': 'Referrals'}]


def test_a_flat_plan_quotes_in_your_plan_never_zero(client):
    quoted = client.get('/routing/quote', headers=ADMIN, params={'to': US, 'pages': 2})
    assert quoted.status_code == 200, quoted.text
    by_account = {item['account']: item for item in quoted.json()['quotes']}
    assert by_account['humblefax']['estimate'] is None
    assert by_account['humblefax']['estimate_text'] == 'In your plan'
    assert by_account['humblefax']['in_plan'] is True
    assert by_account['phaxio']['estimate'] == {'currency': 'USD', 'amount': '0.14'}
    assert by_account['phaxio']['estimate_text'] is None
    explained = client.post('/routing/explain', headers=ADMIN, json={'to': US, 'pages': 2, 'source': 'active'}).json()
    routes = {item['account']: item for item in explained['routes']}
    assert routes['humblefax']['quote'] is None and routes['humblefax']['quote_text'] == 'In your plan'


def test_new_routes_need_their_permissions(client):
    assert client.get('/routing/holds').status_code == 401
    sender = scoped_key(client, ['fax:send'])
    assert client.get('/routing/quote', headers=sender, params={'to': US}).status_code == 403
    assert client.post('/routing/rules/apply-to-waiting', headers=sender).status_code == 403
    applied = client.post('/routing/rules/apply-to-waiting', headers=ADMIN)
    assert applied.status_code == 200 and applied.json()['sentence'] == (
        'No fax is waiting to be sent, so nothing changed.')
