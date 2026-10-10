"""Route problems, the 2-by-2 test and shared upstreams through the real server (brief 92, RF).

The installation is in test mode (FAX_DISABLED): a test fax a person sends is accepted and held, never sent.
"""
from datetime import datetime, timedelta
import json

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from app.routing import route_families
from app.routing.background import installation_engine

BOOTSTRAP = 'synthetic-route-families-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
FIRST, SECOND = '+13035550111', '+13035550112'


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_ROUTES': 'humblefax, signalwire',
                        'HUMBLEFAX_ACCESS_KEY': 'synthetic-access', 'HUMBLEFAX_SECRET_KEY': 'synthetic-secret',
                        'HUMBLEFAX_FROM_NUMBER': SECOND,
                        'SIGNALWIRE_SPACE_URL': 'example.signalwire.com', 'SIGNALWIRE_PROJECT_ID': 'project-1',
                        'SIGNALWIRE_API_TOKEN': 'synthetic-token', 'SIGNALWIRE_FAX_FROM_E164': FIRST,
                        'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def engine():
    return installation_engine(main.app)[0]


def open_incident(**changes):
    now = datetime.utcnow().replace(microsecond=0)
    row = {'id': 'i' * 32, 'account_key': 'signalwire', 'provider_id': 'signalwire', 'transport': 'service',
           'phase': 'service', 'generation': 'profile-1', 'change_at': now - timedelta(hours=3),
           'change_cause': 'outside', 'first_failure_at': now - timedelta(hours=3),
           'last_failure_at': now - timedelta(hours=1), 'destinations': 4, 'failures': 5, 'calls': 6,
           'corroborated': 3, 'opened_at': now - timedelta(minutes=50), **changes}
    table = sa.Table('route_family_incidents', sa.MetaData(), autoload_with=engine())
    with engine().begin() as connection:
        connection.execute(table.insert().values(**row))
    return row


def decisions(job):
    table = sa.Table('fax_job_rule_decisions', sa.MetaData(), autoload_with=engine())
    with engine().connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table).where(table.c.job_id == job)
                                                        .order_by(table.c.sequence)).mappings()]


def test_a_route_problem_is_listed_with_its_sentence_and_a_person_can_close_it(client):
    incident = open_incident()
    listed = client.get('/routing/families', headers=ADMIN)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    [item] = body['incidents']
    assert item['open'] and item['account_label'] == 'SignalWire' and item['destinations'] == 4
    assert 'faxes by SignalWire has failed at the fax service for 4 different numbers' in item['sentence']
    assert item['sentence'].endswith('Faxbot prefers your other routes meanwhile and does not count these failures '
                                     'against the numbers.')
    assert item['advice'].startswith('To tell whether SignalWire or the numbers are at fault, run a 2-by-2 test')
    assert {account['key'] for account in body['accounts']} >= {'phaxio', 'humblefax', 'signalwire'}
    assert {FIRST, SECOND} <= set(body['numbers'])
    closed = client.post(f'/routing/families/{incident["id"]}/close', headers=ADMIN)
    assert closed.status_code == 200, closed.text
    assert not closed.json()['open'] and closed.json()['closed_reason'] == 'person'
    assert 'you closed it' in closed.json()['sentence']
    again = client.post(f'/routing/families/{incident["id"]}/close', headers=ADMIN)
    assert again.status_code == 409
    assert route_families.family_incident(engine(), 'signalwire') is None


def test_a_two_by_two_sends_each_test_fax_only_when_a_person_sends_it(client):
    created = client.post('/routing/families/tests', headers=ADMIN, json={
        'route_a': 'phaxio', 'route_b': 'signalwire', 'number_a': FIRST, 'number_b': SECOND})
    assert created.status_code == 200, created.text
    test = created.json()
    assert [cell['state'] for cell in test['cells']] == ['not_sent'] * 4
    assert test['verdict'] == 'unsent'
    jobs = sa.table('fax_jobs', sa.column('id'))
    with engine().connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(jobs)).scalar() == 0
    sent = client.post(f'/routing/families/tests/{test["id"]}/send/a2', headers=ADMIN)
    assert sent.status_code == 200, sent.text
    job = sent.json()['fax_id']
    assert sent.json()['sentence'] == f'The test fax to {SECOND} by Phaxio is on its way. Its result shows here when ' \
                                      'it ends.'
    # The test fax keeps to its one account: a later decision names only it, with the rules' own first.
    rows = decisions(job)
    assert [row['sequence'] for row in rows] == [1, 2]
    pinned = json.loads(rows[-1]['decision'])
    assert pinned['envelope']['mode'] == 'one' and pinned['envelope']['accounts'] == ['phaxio']
    with engine().connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(jobs)).scalar() == 1
    again = client.post(f'/routing/families/tests/{test["id"]}/send/a2', headers=ADMIN)
    assert again.status_code == 409 and 'already sent' in again.json()['detail']
    shown = client.get(f'/routing/families/tests/{test["id"]}', headers=ADMIN).json()
    assert [cell['state'] for cell in shown['cells']] == ['not_sent', 'pending', 'not_sent', 'not_sent']
    assert shown['cells'][1]['fax_id'] == job


def test_a_test_refuses_numbers_that_are_not_yours_and_accounts_that_do_not_send(client):
    refused = client.post('/routing/families/tests', headers=ADMIN, json={
        'route_a': 'phaxio', 'route_b': 'signalwire', 'number_a': FIRST, 'number_b': '+13035550199'})
    assert refused.status_code == 400 and 'not one of your own numbers' in refused.json()['detail']
    refused = client.post('/routing/families/tests', headers=ADMIN, json={
        'route_a': 'phaxio', 'route_b': 'nobody', 'number_a': FIRST, 'number_b': SECOND})
    assert refused.status_code == 400 and 'not one of your sending accounts' in refused.json()['detail']


def test_an_upstream_is_recorded_with_its_source_and_unknown_stays_unknown(client):
    saved = client.put('/routing/upstreams/phaxio', headers=ADMIN, json={
        'upstream': 'Example Carrier', 'source_url': 'https://example.com/upstream', 'source_date': '2026-10-10'})
    assert saved.status_code == 200, saved.text
    assert saved.json()['source_day'] == '10 October 2026'
    assert client.get('/routing/families', headers=ADMIN).json()['upstreams'][0]['upstream'] == 'Example Carrier'
    missing = client.put('/routing/upstreams/sinch', headers=ADMIN, json={'upstream': 'Example Carrier'})
    assert missing.status_code == 400 and 'web address' in missing.json()['detail']
    cleared = client.put('/routing/upstreams/phaxio', headers=ADMIN, json={'upstream': None})
    assert cleared.status_code == 200
    assert client.get('/routing/families', headers=ADMIN).json()['upstreams'] == []


def test_reading_needs_settings_and_sending_a_test_needs_permission_to_send(client):
    assert client.get('/routing/families').status_code in (401, 403)
    assert client.post('/routing/families/tests/x/send/a1').status_code in (401, 403)


def test_the_command_line_lists_plans_sends_and_closes_through_the_real_server(client):
    from typer.testing import CliRunner
    from app.cli.main import app as cli_app

    def run(*args):
        return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args],
                                  obj={'client_factory': lambda address, timeout: (client, False)},
                                  env={'COLUMNS': '220', 'TZ': 'UTC'})
    empty = run('system', 'diagnostics', 'routes', 'list')
    assert empty.exit_code == 0, empty.stdout
    assert 'Faxbot has found no problem shared by a whole sending route.' in empty.stdout
    incident = open_incident()
    listed = run('system', 'diagnostics', 'routes', 'list')
    flat = ' '.join(listed.stdout.split())
    assert f'Open ({incident["id"][:8]}): Since ' in flat and 'faxes by SignalWire has failed' in flat
    assert 'Start one with: faxbot system diagnostics routes test' in flat
    planned = run('system', 'diagnostics', 'routes', 'test', '--route-a', 'phaxio', '--route-b', 'signalwire',
                  '--number-a', FIRST, '--number-b', SECOND)
    assert planned.exit_code == 0, planned.stdout
    flat = ' '.join(planned.stdout.split())
    assert 'Faxbot never sends a test fax by itself: send the 4 test faxes not sent yet when you are ready.' in flat
    test_id = flat.split('Test ', 1)[1].split(':', 1)[0]
    sent = run('system', 'diagnostics', 'routes', 'send', test_id, 'b1')
    assert sent.exit_code == 0, sent.stdout
    assert f'The test fax to {FIRST} by SignalWire is on its way.' in ' '.join(sent.stdout.split())
    shown = ' '.join(run('system', 'diagnostics', 'routes', 'show-test', test_id).stdout.split())
    assert 'b1 SignalWire +13035550111 Sending' in shown
    closed = run('system', 'diagnostics', 'routes', 'close', incident['id'][:8])
    assert closed.exit_code == 0 and 'you closed it' in closed.stdout
    upstream = run('system', 'diagnostics', 'routes', 'upstream', 'sinch', '--upstream', 'Example Carrier')
    assert upstream.exit_code != 0
    saved = run('system', 'diagnostics', 'routes', 'upstream', 'sinch', '--upstream', 'Example Carrier', '--source',
                'https://example.com/upstream', '--read-on', '2026-10-10')
    assert saved.exit_code == 0 and 'sinch uses Example Carrier upstream, read on 10 October 2026.' in saved.stdout
