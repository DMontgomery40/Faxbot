"""Sending rules in delivery over the real HTTPS stack: send fields, held faxes, why a route, quotes.

Sending is turned off in these installations (FAX_DISABLED), so every accepted
fax stays held and nothing is ever submitted to a provider. Numbers are synthetic.
"""
import pytest
import sqlalchemy as sa
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
    # The installation key decided: what happened first, and the key never by its internal name (D26).
    assert refused.json()['sentence'] == 'Not sent: wrong recipient. Decided through an API key.'
    fax = client.get(f'/fax/{job}', headers=ADMIN).json()
    assert fax['status'] == 'failed' and fax['error'] == 'Not sent: wrong recipient. Decided through an API key.'


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


# The real faxbot commands against this server ---------------------------------------------------------------------

@pytest.fixture
def ruled_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for served in _serve(monkeypatch, tmp_path, FAX_OUTBOUND_ROUTES='humblefax',
                         HUMBLEFAX_ACCESS_KEY='synthetic-access', HUMBLEFAX_SECRET_KEY='synthetic-secret'):
        yield Cli(served), tmp_path


def _cli_ok(result):
    assert result.exit_code == 0, (result.stdout, result.stderr)
    return ' '.join(result.stdout.split())


def test_the_sent_and_costs_commands_against_a_real_server(ruled_cli):
    from api.tests.test_cli import BOOTSTRAP
    cli, tmp_path = ruled_cli
    admin = {'X-API-Key': BOOTSTRAP}
    cards = cli.client.put('/routing/rate-cards', headers=admin, json={'cards': [HUMBLEFAX, PHAXIO]})
    assert cards.status_code == 200, cards.text
    publish_as(cli.client, admin, {'format': 1, 'limits': [rule('l-all', {'hold_for_approval': {}})]})
    # A flat plan's fax is "In your plan", never $0.00; Phaxio by its page price.
    quoted = _cli_ok(cli('costs', 'fax', '--to', US, '--pages', '2'))
    assert 'In your plan' in quoted and 'About $0.14' in quoted and '$0.00' not in quoted
    document = tmp_path / 'note.txt'
    document.write_text('Synthetic page\n')
    first = cli.json('send', US, str(document))['id']
    second = cli.json('send', US, str(document))['id']
    held = _cli_ok(cli('sent', 'list', '--held'))
    assert first in held and second in held and 'Waits for approval: the rule ‘Rule l-all’ matched.' in held
    route = _cli_ok(cli('sent', 'route', first))
    assert route.startswith('Waits for approval: the rule ‘Rule l-all’ matched. Organization rules version 1.')
    assert _cli_ok(cli('sent', 'approve', first)) == 'Approved. The fax is no longer held.'
    refused = _cli_ok(cli('sent', 'refuse', second, '--reason', 'wrong recipient'))
    assert refused == 'Not sent: wrong recipient. Decided through an API key.'
    # A cap no account meets: the fax waits with no route, and anyone may check again.
    publish_as(cli.client, admin, {'format': 1, 'limits': [rule('l-cap', {'cap_cost': {'currency': 'USD',
                                                                                        'amount': '0.01'}},
                                                                {'destination': {'numbers': ['+13035550199']}})],
                                   'routes': [rule('r-phaxio', {'use': 'phaxio'})]})
    capped = cli.json('send', '+13035550199', str(document))['id']
    assert 'No account is estimated to cost less than the $0.01 cap' in _cli_ok(cli('sent', 'list', '--held'))
    assert _cli_ok(cli('sent', 'check-again', capped)).startswith('Faxbot is trying the accounts your rules allow')
    # Now the routing rule allows only Phaxio: its quote is an amount.
    quoted = _cli_ok(cli('costs', 'fax', '--to', US, '--pages', '2'))
    assert 'Phaxio' in quoted and 'About $0.14' in quoted and 'HumbleFax' not in quoted


def publish_as(client, headers, document):
    current = client.get('/routing/rules', headers=headers).json()
    saved = client.put('/routing/rules/draft', headers=headers, json={
        'document': document, 'expected_version': current['draft']['version'] if current['draft'] else 0})
    assert saved.status_code == 200, saved.text
    active = current['active']['number'] if current['active'] else None
    published = client.post('/routing/rules/publish', headers=headers, json={
        'expected_active_revision': active, 'expected_draft_version': saved.json()['version'], 'note': 'Synthetic'})
    assert published.status_code == 200, published.text


def test_send_names_the_route_a_label_rule_chose(ruled_cli):
    """#71: on acceptance `faxbot send --label partner-trunk-test` printed "Planned route HumbleFax" while the
    label's rule sent the fax by Telnyx. The send output names what the fax's own decision chose; without the
    label, the number's recommended route as before."""
    from api.tests.test_cli import BOOTSTRAP
    cli, tmp_path = ruled_cli
    admin = {'X-API-Key': BOOTSTRAP}
    assert cli.client.put('/routing/rate-cards', headers=admin, json={'cards': [HUMBLEFAX, PHAXIO]}).status_code == 200
    first = cli.client.get('/routing/destinations/' + US, headers=admin).json()['recommended_routes'][0]
    chosen, label = (PHAXIO, 'Phaxio') if first['route'] != 'phaxio' else (HUMBLEFAX, 'HumbleFax')
    publish_as(cli.client, admin, {'format': 1, 'labels': ['partner-trunk-test'], 'routes': [
        rule('r-trunk', {'use': chosen['provider_id']}, {'labels': ['partner-trunk-test']})]})
    document = tmp_path / 'note.txt'
    document.write_text('Synthetic page\n')
    labelled = _cli_ok(cli('send', US, str(document), '--label', 'partner-trunk-test'))
    assert f'Planned route {label}' in labelled and f"Planned route {first['label']}" not in labelled
    plain = _cli_ok(cli('send', US, str(document)))
    assert f"Planned route {first['label']}" in plain


# D26: a refused fax in Sent and on the command line ------------------------------------------------------------------

def _signed_in(client, login):
    """Headers for a person's own signed-in session: the cookie, the origin and the CSRF token."""
    from api.tests.test_access_management_http import COOKIE, PASSWORD, _session_token
    from api.tests.test_cli import ORIGIN
    client.cookies.clear()
    signed_in = client.post('/auth/login', json={'login': login, 'password': PASSWORD}, headers={'Origin': ORIGIN})
    assert signed_in.status_code == 200, signed_in.text
    client.cookies.clear()
    cookie = {'Cookie': f'{COOKIE}={_session_token(signed_in)}'}
    return {**cookie, 'Origin': ORIGIN, 'X-CSRF-Token': client.get('/auth/me', headers=cookie).json()['csrf_token']}


def _integration_key(client, admin, name):
    """An integration (an API key's owner) whose key may approve faxes."""
    def policy():
        return client.get('/auth/me', headers=admin).json()['policy_version']
    integration = client.post('/access/integrations', headers=admin, json={
        'display_name': name, 'enabled': True, 'expected_policy_version': policy()}).json()['integration']
    roles = {role['id']: role for role in client.get('/access/roles', headers=admin).json()['items']}

    def version():
        return client.get(f"/access/users/{integration['id']}", headers=admin).json()['version']
    granted = client.post('/access/assignments', headers=admin, json={
        'subject': {'kind': 'principal', 'id': integration['id'], 'version': version()},
        'role': {'id': 'role_administrator', 'version': roles['role_administrator']['version']},
        'resource_id': 'installation', 'expected_policy_version': policy()})
    assert granted.status_code == 200, granted.text
    issued = client.post('/access/keys', headers=admin, json={
        'principal': {'id': integration['id'], 'version': version()}, 'name': 'sync key', 'note': None,
        'expires_at': None, 'ceiling': [{'permission': permission, 'resource_id': 'installation'}
                                        for permission in ('fax:approve', 'fax:read')],
        'expected_policy_version': policy()})
    assert issued.status_code == 200, issued.text
    return {'X-API-Key': issued.json()['token']}


def test_sent_says_what_happened_to_a_refused_fax_and_who_decided(ruled_cli):
    """D26: on acceptance Sent showed "Refused by Installation bootstrap: Telnyx account disabled for a negative
    balanc". Sent's list and details, GET /fax/{id} and faxbot faxes sent list and show now lead with "Not sent:" and
    the reason, whole; a person who decided is named, an API key only in the fax's details, and the installation
    key never by its internal name. The stored sentence is unchanged."""
    from api.tests.test_cli import BOOTSTRAP
    from api.tests.test_cli_work import ready_user
    cli, tmp_path = ruled_cli
    client, admin = cli.client, {'X-API-Key': BOOTSTRAP}
    publish_as(client, admin, {'format': 1, 'limits': [rule('l-all', {'hold_for_approval': {}})]})
    document = tmp_path / 'note.txt'
    document.write_text('Synthetic page\n')

    def refuse(headers, reason):
        job = cli.json('send', US, str(document))['id']
        hold = next(item for item in client.get('/routing/holds', headers=admin).json()['holds']
                    if item['job_id'] == job)
        refused = client.post(f"/routing/holds/{hold['id']}/refuse", headers=headers,
                              json={'version': hold['version'], 'reason': reason})
        assert refused.status_code == 200, refused.text
        return job, refused.json()['sentence']
    balance = 'Telnyx account disabled for a negative balance; add funds in the Telnyx portal under Billing'
    installation, said = refuse(admin, balance)
    assert said == f'Not sent: {balance}. Decided through an API key.'
    integration, _ = refuse(_integration_key(client, admin, 'Billing sync'), 'duplicate of an earlier fax')
    ready_user(client, 'dana', 'Dana Ruiz', role='role_administrator')
    person, said = refuse(_signed_in(client, 'dana'), 'wrong recipient')
    assert said == 'Not sent: wrong recipient. Decided by Dana Ruiz.'
    # A long reason: the stored sentence is cut at 500 characters, mid-word; Sent ends it at its last whole word.
    words = ' '.join(f'word{index:03d}' for index in range(80))
    long_job, _ = refuse(admin, words[:490])
    # One word of 500 characters, the most a reason may be: what was stored, then "…", never a full stop.
    token_job, _ = refuse(admin, 'x' * 500)
    client.cookies.clear()

    listed = {job['id']: job for job in client.get('/admin/fax-jobs', headers=admin).json()['jobs']}
    shown = {job: (listed[job]['error'], client.get(f'/admin/fax-jobs/{job}', headers=admin).json()['refusal'])
             for job in (installation, integration, person)}
    assert shown == {
        installation: (f'Not sent: {balance}. Decided through an API key.',
                       {'sentence': f'Not sent: {balance}.', 'decided_by': 'Decided through an API key.',
                        'decided_by_detail': 'Decided through the installation key.'}),
        integration: ('Not sent: duplicate of an earlier fax. Decided through an API key.',
                      {'sentence': 'Not sent: duplicate of an earlier fax.', 'decided_by': 'Decided through an API key.',
                       'decided_by_detail': 'Decided through the API key ‘Billing sync’.'}),
        person: ('Not sent: wrong recipient. Decided by Dana Ruiz.',
                 {'sentence': 'Not sent: wrong recipient.', 'decided_by': 'Decided by Dana Ruiz.',
                  'decided_by_detail': 'Decided by Dana Ruiz.'})}
    for job in (installation, integration, person):
        assert client.get(f'/fax/{job}', headers=admin).json()['error'] == listed[job]['error']
    for job in listed.values():
        assert 'Installation bootstrap' not in str(job), job
    assert all('Billing sync' not in str(job['error']) for job in listed.values())
    long_error = listed[long_job]['error']
    assert long_error.endswith('… Decided through an API key.') and long_error.startswith('Not sent: word000 ')
    kept = long_error[len('Not sent: '):-len('… Decided through an API key.')].split(' ')
    assert kept == words.split(' ')[:len(kept)] and len(kept) > 50   # whole words only, never one cut short
    kept = 500 - len('Refused by Installation bootstrap: ')
    assert listed[token_job]['error'] == f"Not sent: {'x' * kept}… Decided through an API key."
    # Refused holds read the same way; the stored sentence stays as it was written.
    refused_holds = {item['job_id']: item['reason'] for item in
                     client.get('/routing/holds', headers=admin, params={'state': 'refused'}).json()['holds']}
    assert refused_holds[installation] == f'Not sent: {balance}. Decided through an API key.'
    from app import main
    with main.app.state.configuration_runtime.manager.store.engine.connect() as connection:
        stored = connection.execute(sa.text('SELECT error FROM fax_jobs WHERE id = :id'),
                                    {'id': installation}).scalar_one()
    assert stored == f'Refused by Installation bootstrap: {balance}'
    # The command line: the list as Sent's Status cell, the details naming the key.
    listed_text = _cli_ok(cli('sent', 'list'))
    assert 'Failed. Not sent: duplicate of an earlier fax. Decided through an API key.' in listed_text
    assert 'Failed. Not sent: wrong recipient. Decided by Dana Ruiz.' in listed_text
    assert 'Installation bootstrap' not in listed_text and 'Billing sync' not in listed_text
    assert f'Problem Not sent: {balance}. Decided through the installation key.' in _cli_ok(
        cli('sent', 'show', installation))
    assert 'Problem Not sent: duplicate of an earlier fax. Decided through the API key ‘Billing sync’.' in _cli_ok(
        cli('sent', 'show', integration))
    assert 'Problem Not sent: wrong recipient. Decided by Dana Ruiz.' in _cli_ok(cli('status', person))
