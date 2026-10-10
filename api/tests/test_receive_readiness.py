"""Receiving readiness per number and receive owners (brief 92, RF item 2).

Receiving is judged from receiving evidence only: an installation that sends fine but cannot be reached reads
"not receiving". Synthetic numbers and the real server; the address check goes through the real reach route.
"""
from datetime import datetime, timedelta
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main, receive_readiness as readiness
from app.config_values import ConfigurationValues
from app.receive_readiness import ATTENTION, NOT_RECEIVING, READY, Endpoint, NumberView, judge
from app.routing.background import installation_engine
from app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 - fixture

BOOTSTRAP = 'synthetic-receiving-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
NUMBER, OTHER = '+13035550121', '+13035550122'
SIP = Endpoint('sip', 'Telnyx trunk', 'sip')
SINCH = Endpoint('sinch', 'Sinch', 'sinch')


def _client(monkeypatch, public):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': public,
                        'FAX_BACKEND': 'phaxio', 'INBOUND_ENABLED': 'true', 'FAX_INBOUND_BACKEND': 'sinch',
                        'SINCH_PROJECT_ID': 'project-1', 'SINCH_API_KEY': 'synthetic-key',
                        'SINCH_API_SECRET': 'synthetic-secret', 'SINCH_INBOUND_BASIC_USER': 'faxbot',
                        'SINCH_INBOUND_BASIC_PASS': 'Synthetic-Pass-1', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    # The Sinch account's numbers (an account-list overlay in a real installation).
    monkeypatch.setattr(readiness, 'receiving_endpoints', lambda values: {NUMBER: [SINCH]})
    return TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'})


def test_sending_works_but_an_unreachable_receiving_address_reads_not_receiving(isolated_installation, monkeypatch):
    # Phaxio sends (test mode, ready); the public address is a closed port, as on a backup line with no way in.
    with _client(monkeypatch, 'http://127.0.0.1:9') as client:
        assert client.get('/health/ready').status_code in (200, 503)
        body = client.get('/receiving/readiness', headers=ADMIN)
        assert body.status_code == 200, body.text
        [number] = body.json()['numbers']
        assert number['number'] == NUMBER and number['status'] == NOT_RECEIVING
        assert number['sentence'].startswith('Not receiving: Faxbot could not reach its own receiving address for '
                                             'Sinch through your public address')


def test_the_receiving_address_is_proven_through_the_public_address_with_a_one_time_code(isolated_installation,
                                                                                         monkeypatch):
    with _client(monkeypatch, 'https://testserver') as client:
        original = readiness.probe
        monkeypatch.setattr(readiness, 'probe', lambda address: original(address, client=client))
        [number] = client.get('/receiving/readiness', headers=ADMIN).json()['numbers']
        assert number['status'] == READY and number['sentence'] == 'Receiving: every check for this number passed.'
        assert {'title': 'Sinch: receiving address', 'status': READY,
                'sentence': 'Sinch can reach Faxbot at its receiving address.'} in number['checks']
        # Only a code this server issued in the last minute is answered, and only once.
        assert client.get('/sinch-inbound').status_code == 404
        assert client.get('/sinch-inbound?faxbot_reach=made-up').status_code == 404
        code = readiness.issue_code()
        assert client.get(f'/sinch-inbound/second?faxbot_reach={code}').text == code
        assert client.get(f'/sinch-inbound/second?faxbot_reach={code}').status_code == 404


def test_two_receivers_for_one_number_are_refused_unless_moved_on_purpose(isolated_installation, monkeypatch):
    with _client(monkeypatch, 'https://testserver') as client:
        first = client.post('/receiving/owners', headers=ADMIN, json={'number': NUMBER, 'owner': 'sinch'})
        assert first.status_code == 200, first.text
        assert first.json()['sentence'] == f'Sinch now receives the faxes for {NUMBER}.'
        other = client.post('/receiving/owners', headers=ADMIN, json={
            'number': NUMBER, 'owner': 'elsewhere', 'label': 'the fax machine at reception'})
        assert other.status_code == 409
        assert other.json()['detail'].startswith(f'Sinch already receives the faxes for {NUMBER}.')
        moved = client.post('/receiving/owners', headers=ADMIN, json={
            'number': NUMBER, 'owner': 'elsewhere', 'label': 'the fax machine at reception', 'move': True})
        assert moved.status_code == 200
        assert client.post('/receiving/owners', headers=ADMIN, json={
            'number': NUMBER, 'owner': 'nobody'}).status_code == 400
        released = client.post('/receiving/owners', headers=ADMIN, json={'number': NUMBER, 'owner': None})
        assert released.json()['sentence'] == f'No receiver is named for {NUMBER} any more.'
        engine = installation_engine(main.app)[0]
        rows = readiness.owners(engine)
        assert rows[NUMBER]['generation'] == 3 and rows[NUMBER]['owner'] is None


def test_a_claim_made_at_the_same_moment_as_another_loses(database, monkeypatch):  # noqa: F811
    upgrade_schema(database)
    readiness.claim(database, NUMBER, 'sip')
    table = sa.Table('receive_owner_claims', sa.MetaData(), autoload_with=database)
    stale = readiness._current(database, table, NUMBER)
    # Another claim commits between this claim's read and its write.
    with database.begin() as connection:
        connection.execute(table.insert().values(id=uuid.uuid4().hex, number=NUMBER, generation=2, owner='sinch',
                                                 claimed_at=datetime.utcnow()))
    monkeypatch.setattr(readiness, '_current', lambda db, table, number: stale)
    with pytest.raises(readiness.OwnerConflict, match='just now'):
        readiness.claim(database, NUMBER, 'sip', move=True)
    assert readiness.owners(database)[NUMBER]['owner'] == 'sinch'


def test_a_trunk_reads_its_sign_in_and_the_last_call_that_reached_it():
    now = datetime(2026, 10, 10, 12)
    view = judge(NumberView(NUMBER, [SIP]), registration='rejected', now=now)
    assert view.status == NOT_RECEIVING and 'not signed in to your carrier' in view.sentence
    view = judge(NumberView(NUMBER, [SIP]), registration='registered', now=now)
    assert view.status == ATTENTION and view.sentence.startswith('No call to this number has reached Faxbot yet.')
    view = judge(NumberView(NUMBER, [SIP]), registration='registered', invite=now - timedelta(hours=3), now=now)
    assert view.status == READY
    view = judge(NumberView(NUMBER, [SIP]), auth_mode='ip', invite=now - timedelta(hours=3), now=now)
    assert view.status == READY and any('no sign-in is needed' in check['sentence'] for check in view.checks)
    view = judge(NumberView(NUMBER, [SIP]), registration='unknown', invite=now - timedelta(hours=3), now=now)
    assert view.status == ATTENTION


def test_two_accounts_on_one_number_need_a_named_receiver_and_strays_are_named():
    view = judge(NumberView(NUMBER, [SIP, SINCH]), registration='registered', reached={'sinch': (True, None)},
                 invite=datetime.utcnow())
    assert view.status == ATTENTION
    assert view.sentence == ('Faxes to this number can arrive on both Telnyx trunk and Sinch, and none is named as '
                             'its receiver. Choose which one receives it.')
    view = judge(NumberView(NUMBER, [SIP, SINCH], owner='sip', owner_label='Telnyx trunk'), registration='registered',
                 reached={'sinch': (True, None)}, invite=datetime.utcnow(), arrivals={'sinch'})
    assert view.status == ATTENTION and 'also arrived on sinch this week' in view.sentence
    view = judge(NumberView(OTHER, [SINCH], last_received=datetime.utcnow()), reached={'sinch': (True, None)})
    assert view.status == READY and any(check['title'] == 'Last fax received' and check['status'] == READY
                                        for check in view.checks)


def test_trunk_numbers_come_from_the_real_accounts():
    values = ConfigurationValues.from_environment({
        'INBOUND_ENABLED': 'true', 'FAX_INBOUND_BACKEND': 'sip', 'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx',
        'SIP_TRUNK_USERNAME': 'faxbotuser', 'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1',
        'SIP_TRUNK_DIDS': f'{NUMBER},{OTHER}'})
    found = readiness.receiving_endpoints(values)
    assert set(found) == {NUMBER, OTHER} and [item.provider for item in found[NUMBER]] == ['sip']
    assert readiness.receiving_endpoints(ConfigurationValues.from_environment({'INBOUND_ENABLED': 'false'})) == {}


def test_the_command_line_checks_receiving_and_names_owners(isolated_installation, monkeypatch):
    from typer.testing import CliRunner
    from app.cli.main import app as cli_app
    with _client(monkeypatch, 'http://127.0.0.1:9') as client:
        def run(*args):
            return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args],
                                      obj={'client_factory': lambda address, timeout: (client, False)},
                                      env={'COLUMNS': '220', 'TZ': 'UTC'})
        checked = run('system', 'diagnostics', 'receiving', 'check')
        assert checked.exit_code == 0, checked.stdout
        flat = ' '.join(checked.stdout.split())
        assert f'{NUMBER}: Not receiving. Not receiving: Faxbot could not reach its own receiving address' in flat
        owned = run('system', 'diagnostics', 'receiving', 'owner', NUMBER, '--account', 'sinch')
        assert owned.exit_code == 0 and f'Sinch now receives the faxes for {NUMBER}.' in owned.stdout
        refused = run('system', 'diagnostics', 'receiving', 'owner', NUMBER, '--elsewhere', 'the old fax machine')
        assert refused.exit_code != 0
        assert run('system', 'diagnostics', 'receiving', 'owner', NUMBER).exit_code != 0
