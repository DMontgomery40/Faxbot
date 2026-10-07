"""Sending rules over the real HTTPS stack: drafts, check and replay, publish, history, scopes and the dry run."""
from datetime import datetime
import inspect

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from app.rules import model, store as store_module
from app.rules.store import RuleStore
from app.rules.evaluate import decide


BOOTSTRAP = 'synthetic-rules-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
UK = '+442071234567'
US = '+13035550100'


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


def rule(rule_id, then, when=None, **extra):
    return {'id': rule_id, 'name': f'Rule {rule_id}', 'on': True, 'when': when or {}, 'then': then, **extra}


UK_RULE = rule('r-uk', {'try_in_order': ['signalwire', 'sip']}, {'destination': {'countries': ['GB']}})
DOCUMENT = {'format': 1, 'labels': ['legal'],
            'workflows': [{'key': 'referrals', 'name': 'Referrals', 'labels': ['legal']}],
            'limits': [rule('l-no-phaxio-uk', {'never': ['phaxio']}, {'destination': {'countries': ['GB']}})],
            'routes': [UK_RULE]}


def _publish(client, document, *, scope='organization', expected_active=None, note='First rules'):
    current = client.get('/routing/rules', params={'scope': scope}, headers=ADMIN).json()
    version = current['draft']['version'] if current['draft'] else 0
    saved = client.put('/routing/rules/draft', params={'scope': scope}, headers=ADMIN,
                       json={'document': document, 'expected_version': version})
    assert saved.status_code == 200, saved.text
    published = client.post('/routing/rules/publish', params={'scope': scope}, headers=ADMIN,
                            json={'expected_active_revision': expected_active,
                                  'expected_draft_version': saved.json()['version'], 'note': note})
    assert published.status_code == 200, published.text
    return published.json()


def test_a_scope_with_no_rules_offers_the_names_its_editor_needs(client):
    response = client.get('/routing/rules', headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['scope'] == {'kind': 'organization', 'id': None, 'name': 'Organization'}
    assert body['active'] is None and body['draft'] is None and body['organization'] is None
    assert body['can_write'] is True and body['matches_30_days'] == {} and body['time_zone']
    assert [account['key'] for account in body['choices']['accounts']] == ['phaxio', 'sip', 'signalwire']
    assert set(body['choices']) == {'accounts', 'people', 'keys', 'groups', 'mailboxes', 'recipients'}
    assert body['choices']['recipients'] == []


def test_saved_recipients_are_offered_by_name_or_number_and_a_rule_can_use_them(client):
    for number, changes in ((US, {'display_name': 'Example Clinic'}), ('+12025550123', {'notes': 'No name yet'}),
                            ('+13125550188', {'display_name': 'acme Labs'})):
        saved = client.patch(f'/routing/destinations/{number}', headers=ADMIN, json=changes)
        assert saved.status_code == 200, saved.text
    recipients = client.get('/routing/rules', headers=ADMIN).json()['choices']['recipients']
    assert [item['name'] for item in recipients] == ['+12025550123', 'acme Labs', 'Example Clinic']
    clinic = next(item['id'] for item in recipients if item['name'] == 'Example Clinic')
    _publish(client, {'format': 1, 'limits': [], 'routes': [
        rule('r-clinic', {'use': 'signalwire'}, {'destination': {'recipients': [clinic]}})]})
    body = client.post('/routing/explain', headers=ADMIN, json={'to': US}).json()
    assert [route['account'] for route in body['routes'] if route['usable']] == ['signalwire']
    other = client.post('/routing/explain', headers=ADMIN, json={'to': '+12025550123'}).json()
    assert other['trace'][0]['failed'] == 'the saved recipient'


def test_drafts_save_with_their_check_and_refuse_a_stale_version(client):
    saved = client.put('/routing/rules/draft', headers=ADMIN, json={'document': DOCUMENT, 'expected_version': 0})
    assert saved.status_code == 200, saved.text
    draft = saved.json()
    assert draft['version'] == 1 and draft['base_revision'] is None and draft['check']['errors'] == []
    stale = client.put('/routing/rules/draft', headers=ADMIN, json={'document': DOCUMENT, 'expected_version': 0})
    assert stale.status_code == 409 and 'Reload' in stale.json()['detail']
    broken = {**DOCUMENT, 'routes': [rule('r-x', {'use': 'documo'})]}
    saved = client.put('/routing/rules/draft', headers=ADMIN, json={'document': broken, 'expected_version': 1})
    assert saved.status_code == 200 and saved.json()['version'] == 2
    assert [issue['rule_id'] for issue in saved.json()['check']['errors']] == ['r-x']
    assert 'doesn’t exist' in saved.json()['check']['errors'][0]['message']
    refused = client.post('/routing/rules/publish', headers=ADMIN,
                          json={'expected_active_revision': None, 'expected_draft_version': 2})
    assert refused.status_code == 400 and refused.json()['detail']['errors'][0]['rule_id'] == 'r-x'
    assert client.delete('/routing/rules/draft', headers=ADMIN).json() == {'discarded': True}
    assert client.get('/routing/rules', headers=ADMIN).json()['draft'] is None


def test_publishing_numbers_revisions_and_a_stale_publish_returns_409(client):
    first = _publish(client, DOCUMENT)
    assert first['number'] == 1 and first['note'] == 'First rules'
    state = client.get('/routing/rules', headers=ADMIN).json()
    assert state['active']['number'] == 1 and state['active']['document'] == DOCUMENT and state['draft'] is None
    # A second administrator publishing from the old head is refused, with a sentence.
    saved = client.put('/routing/rules/draft', headers=ADMIN,
                       json={'document': {**DOCUMENT, 'routes': []}, 'expected_version': 0}).json()
    stale = client.post('/routing/rules/publish', headers=ADMIN,
                        json={'expected_active_revision': None, 'expected_draft_version': saved['version']})
    assert stale.status_code == 409 and 'published these rules after you opened them' in stale.json()['detail']
    second = client.post('/routing/rules/publish', headers=ADMIN,
                         json={'expected_active_revision': 1, 'expected_draft_version': saved['version'],
                               'note': 'No routes'})
    assert second.status_code == 200 and second.json()['number'] == 2
    with _engine().connect() as connection:
        audit = connection.execute(sa.text("SELECT operation, details FROM access_audit "
                                           "WHERE operation = 'routing.rules_published' ORDER BY created_at")).all()
    assert len(audit) == 2 and '"revision":2' in audit[1][1] and '"previous":1' in audit[1][1]


def test_revisions_are_immutable_history_with_differences_and_restore(client):
    _publish(client, DOCUMENT)
    with _engine().connect() as connection:
        first = dict(connection.execute(sa.text('SELECT * FROM routing_rule_revisions')).mappings().one())
    moved = {**DOCUMENT, 'labels': ['legal', 'billing'],
             'routes': [rule('r-us', {'use': 'sip'}, {'destination': {'countries': ['US']}}),
                        {**UK_RULE, 'name': 'UK numbers go by SignalWire'}]}
    _publish(client, moved, expected_active=1, note='US rule')
    with _engine().connect() as connection:
        rows = connection.execute(sa.text('SELECT * FROM routing_rule_revisions ORDER BY number')).mappings().all()
    assert dict(rows[0]) == first and rows[1]['parent_id'] == first['id']
    listed = client.get('/routing/rules/revisions', headers=ADMIN).json()['revisions']
    assert [(item['number'], item['note']) for item in listed] == [(2, 'US rule'), (1, 'First rules')]
    assert 'document' not in listed[0]
    assert client.get('/routing/rules/revisions/1', headers=ADMIN).json()['document'] == DOCUMENT
    assert client.get('/routing/rules/revisions/9', headers=ADMIN).status_code == 404
    changes = client.get('/routing/rules/revisions/1/diff/2', headers=ADMIN).json()['changes']
    assert {(change['change'], change['section'], change['id']) for change in changes} == {
        ('added', 'labels', 'billing'), ('added', 'routes', 'r-us'), ('changed', 'routes', 'r-uk')}
    restored = client.post('/routing/rules/revisions/1/restore', headers=ADMIN)
    assert restored.status_code == 200 and restored.json()['document'] == DOCUMENT
    assert restored.json()['base_revision'] == 2
    # The store has no way to change or remove a revision.
    source = inspect.getsource(store_module)
    assert 'revisions.update(' not in source and 'revisions.delete(' not in source


def test_the_dry_run_explains_the_route_against_active_draft_and_earlier_rules(client):
    explained = client.post('/routing/explain', headers=ADMIN, json={'to': UK, 'pages': 2})
    assert explained.status_code == 200, explained.text
    body = explained.json()
    assert body['outcome'] == 'route' and 'No sending rule matched' in body['sentence']
    assert [route['account'] for route in body['routes']] == ['phaxio', 'sip', 'signalwire']
    _publish(client, DOCUMENT)
    body = client.post('/routing/explain', headers=ADMIN, json={'to': UK, 'pages': 2}).json()
    assert body['outcome'] == 'route' and '‘Rule r-uk’' in body['sentence']
    usable = [route['account'] for route in body['routes'] if route['usable']]
    assert usable == ['signalwire', 'sip']
    skipped = next(route for route in body['routes'] if route['account'] == 'phaxio')
    assert not skipped['usable'] and 'Rule l-no-phaxio-uk' in skipped['sentence'] and skipped['soft'] is False
    assert [(step['rule_id'], step['matched']) for step in body['trace']] == [('l-no-phaxio-uk', True),
                                                                           ('r-uk', True)]
    us = client.post('/routing/explain', headers=ADMIN, json={'to': US}).json()
    assert us['trace'][0]['failed'] == 'the destination’s country'
    # A draft is tried without publishing it.
    client.put('/routing/rules/draft', headers=ADMIN, json={'document': {**DOCUMENT, 'limits': [
        rule('l-approve', {'hold_for_approval': {'separate_approver': True}})]}, 'expected_version': 0})
    held = client.post('/routing/explain', headers=ADMIN, json={'to': UK, 'source': 'draft'}).json()
    assert held['outcome'] == 'held' and held['holds'][0].startswith('Waits for approval')
    assert client.post('/routing/explain', headers=ADMIN, json={'to': UK, 'source': {'revision': 1}}).json()[
        'outcome'] == 'route'
    assert client.post('/routing/explain', headers=ADMIN, json={'to': 'not a number'}).status_code == 400


def _fax(connection, job_id, to_number, created_at):
    connection.execute(sa.text(
        "INSERT INTO fax_jobs (id, to_number, file_name, tiff_path, status, pages, backend, created_at, updated_at) "
        "VALUES (:id, :to, 'note.pdf', '', 'queued', 1, 'phaxio', :at, :at)"), {'id': job_id, 'to': to_number,
                                                                                'at': created_at})


def test_check_replays_recent_faxes_and_labels_older_ones_approximate(client):
    engine = _engine()
    store = RuleStore(engine)
    accounts = (model.Account('phaxio', 'phaxio', default=True, automatic=True),
                model.Account('sip', 'sip', automatic=True), model.Account('signalwire', 'signalwire', automatic=True))
    facts = model.Facts(UK, '2026-10-07T15:00:00', country='GB')
    with engine.begin() as connection:
        _fax(connection, 'job-1', UK, datetime(2026, 10, 7, 15))
        store.record_decision_on(connection, job_id='job-1', facts=facts, decision=decide({}, facts, accounts),
                                 now=datetime(2026, 10, 7, 15))
        _fax(connection, 'job-2', US, datetime(2026, 10, 1, 12))  # accepted before rules existed
    client.put('/routing/rules/draft', headers=ADMIN, json={'document': DOCUMENT, 'expected_version': 0})
    checked = client.post('/routing/rules/draft/check', headers=ADMIN, json={'replay': 200})
    assert checked.status_code == 200, checked.text
    replay = checked.json()['replay']
    assert (replay['checked'], replay['changed'], replay['approximate']) == (2, 1, 1)
    [item] = replay['items']
    assert item['job_id'] == 'job-1' and not item['approximate']
    assert 'No sending rule matched' in item['before'] and 'Rule r-uk' in item['after']
    assert replay['sentence'] == '1 of your last 2 faxes would go differently.'
    # The check is kept with the draft version it covered.
    assert client.get('/routing/rules', headers=ADMIN).json()['draft']['check']['replay']['changed'] == 1


def test_lower_scopes_need_their_mailbox_or_workflow_and_read_the_organizations_rules(client):
    assert client.get('/routing/rules', params={'scope': 'mailbox:nope'}, headers=ADMIN).status_code == 404
    assert client.get('/routing/rules', params={'scope': 'workflow:referrals'}, headers=ADMIN).status_code == 404
    assert client.get('/routing/rules', params={'scope': 'team:x'}, headers=ADMIN).status_code == 400
    _publish(client, DOCUMENT)
    workflow = client.get('/routing/rules', params={'scope': 'workflow:referrals'}, headers=ADMIN)
    assert workflow.status_code == 200
    assert workflow.json()['scope']['name'] == 'Referrals'
    assert workflow.json()['organization']['number'] == 1
    published = _publish(client, {'format': 1, 'limits': [], 'routes': [rule('w-sip', {'use': 'sip'})]},
                         scope='workflow:referrals', note='Referrals by the trunk')
    assert published['number'] == 1
    body = client.post('/routing/explain', headers=ADMIN, json={'to': UK, 'labels': ['legal']}).json()
    assert [route['account'] for route in body['routes'] if route['usable']] == ['sip']
    assert ('w-sip', True) in [(step['rule_id'], step['matched']) for step in body['trace']]
    assert {'scope': 'workflow:referrals', 'number': 1} in body['revisions']


def test_rules_need_identity_and_the_scopes_permission(client):
    response = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'sender', 'scopes': ['fax:send']})
    sender = {'X-API-Key': response.json()['token']}
    for method, path, body in [('GET', '/routing/rules', None),
                               ('PUT', '/routing/rules/draft', {'document': DOCUMENT, 'expected_version': 0}),
                               ('POST', '/routing/rules/publish', {'expected_draft_version': 1}),
                               ('GET', '/routing/rules/revisions', None),
                               ('POST', '/routing/explain', {'to': UK})]:
        assert client.request(method, path, json=body, headers=sender).status_code == 403, (method, path)
        assert client.request(method, path, json=body).status_code == 401, (method, path)


def _mailbox(client, label):
    version = client.get('/auth/me', headers=ADMIN).json()['policy_version']
    created = client.post('/access/mailboxes', headers=ADMIN,
                          json={'label': label, 'enabled': True, 'expected_policy_version': version})
    assert created.status_code == 200, created.text
    return created.json()['mailbox']['id']


def test_a_mailboxs_rules_follow_mailbox_management_and_narrow_its_faxes(client):
    mailbox = _mailbox(client, 'Front desk')
    scope = {'scope': f'mailbox:{mailbox}'}
    state = client.get('/routing/rules', params=scope, headers=ADMIN)
    assert state.status_code == 200, state.text
    assert state.json()['scope'] == {'kind': 'mailbox', 'id': mailbox, 'name': 'Front desk'}
    assert state.json()['can_write'] is True
    published = _publish(client, {'format': 1, 'limits': [rule('m-no-sip', {'never': ['sip']})], 'routes': []},
                         scope=f'mailbox:{mailbox}', note='No trunk from the front desk')
    assert published['number'] == 1
    body = client.post('/routing/explain', headers=ADMIN, json={'to': US, 'mailbox': mailbox}).json()
    assert [route['account'] for route in body['routes'] if route['usable']] == ['phaxio', 'signalwire']
    assert ('m-no-sip', True) in [(step['rule_id'], step['matched']) for step in body['trace']]
    other = client.post('/routing/explain', headers=ADMIN, json={'to': US}).json()
    assert [route['account'] for route in other['routes'] if route['usable']] == ['phaxio', 'sip', 'signalwire']
    response = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'sender', 'scopes': ['fax:send']})
    sender = {'X-API-Key': response.json()['token']}
    assert client.get('/routing/rules', params=scope, headers=sender).status_code == 403
    assert client.put('/routing/rules/draft', params=scope, headers=sender,
                      json={'document': {'format': 1}, 'expected_version': 0}).status_code == 403


def test_a_rule_that_matched_nothing_counts_zero_and_the_check_says_so(client):
    document = {**DOCUMENT, 'routes': [UK_RULE, rule('r-idle', {'use': 'sip'}, {'destination': {'countries': ['FR']}})]}
    _publish(client, document)
    engine = _engine()
    store = RuleStore(engine)
    accounts = (model.Account('phaxio', 'phaxio', default=True, automatic=True),
                model.Account('sip', 'sip', automatic=True), model.Account('signalwire', 'signalwire', automatic=True))
    now = datetime.utcnow().replace(microsecond=0)
    facts = model.Facts(UK, now.isoformat(), country='GB')
    with engine.begin() as connection:
        _fax(connection, 'job-1', UK, now)
        store.record_decision_on(connection, job_id='job-1', facts=facts,
                                 decision=decide(store.compiled_active(), facts, accounts), now=now)
    matches = client.get('/routing/rules', headers=ADMIN).json()['matches_30_days']
    assert matches == {'l-no-phaxio-uk': 1, 'r-uk': 1, 'r-idle': 0}
    client.put('/routing/rules/draft', headers=ADMIN, json={'document': document, 'expected_version': 0})
    checked = client.post('/routing/rules/draft/check', headers=ADMIN, json={'replay': 0}).json()
    assert [warning['rule_id'] for warning in checked['warnings']] == ['r-idle']
    assert 'matched no fax in the last 30 days' in checked['warnings'][0]['message']
