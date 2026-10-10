"""Channels at peak and the fax server renewal page through the real server and command line (N20, N24).

Synthetic call records and routing only. Not yet run against a real fax server's records.
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from api.tests.test_channel_peak import RIGHTFAX_3, ROUTES

BOOTSTRAP = 'synthetic-renewal-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'FAX_DEFAULT_COUNTRY': 'US', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def test_import_calls_record_a_renewal_and_read_its_page(client):
    assert client.get('/routing/channels', headers=ADMIN).json()['systems'] == []
    assert client.get('/routing/renewals', headers=ADMIN).json()['pages'] == []
    imported = client.post('/routing/channels/files', headers=ADMIN,
                           data={'system': 'RightFax at HQ', 'licensed': '8', 'time_zone': 'UTC'},
                           files={'file': ('audit.log', RIGHTFAX_3.encode(), 'text/plain')})
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert (body['imported'], body['format'], body['skipped_count']) == (3, 'rightfax', 1)
    system = body['systems'][0]
    assert system['report']['never_used'] == 5 and system['imports'][0]['calls'] == 3
    renews = (date.today() + timedelta(days=60)).isoformat()
    saved = client.put('/routing/renewals', headers=ADMIN, json={
        'system': 'RightFax at HQ', 'renews_on': renews, 'amount': '26756.71', 'product': 'RightFax 22.2',
        'licensed_channels': 8, 'parallel_numbers': ['303-555-0100'], 'parallel_since': date.today().isoformat()})
    assert saved.status_code == 200, saved.text
    page = saved.json()['pages'][0]
    assert page['renewal']['state'] == 'review' and page['channels']['unneeded_value'] == '$16,722.94'
    assert page['parallel']['numbers'][0]['number'] == '+13035550100'
    routed = client.post('/routing/renewals/routes', headers=ADMIN, data={'system': 'RightFax at HQ'},
                         files={'file': ('routing.csv', ROUTES.encode(), 'text/csv')})
    assert routed.status_code == 200 and routed.json()['imported'] == 3 and routed.json()['skipped_count'] == 1
    assert routed.json()['pages'][0]['left']['count'] == 3
    removed = client.delete(f"/routing/channels/imports/{system['imports'][0]['id']}", headers=ADMIN)
    assert removed.status_code == 200 and removed.json()['systems'] == []
    assert client.delete(f"/routing/channels/imports/{system['imports'][0]['id']}", headers=ADMIN).status_code == 404
    withdrawn = client.post('/routing/renewals/remove', headers=ADMIN, json={'system': 'RightFax at HQ'})
    assert withdrawn.status_code == 200
    assert client.post('/routing/renewals/remove', headers=ADMIN, json={'system': 'RightFax at HQ'}).status_code == 404


def test_what_faxbot_cannot_read_is_refused_in_one_sentence(client):
    unknown = client.post('/routing/channels/files', headers=ADMIN, data={'system': 'X'},
                          files={'file': ('calls.csv', b'hello,world\n', 'text/csv')})
    assert unknown.status_code == 400 and 'Choose its format' in unknown.json()['detail']
    bad = client.put('/routing/renewals', headers=ADMIN, json={'system': 'X', 'renews_on': '31/05/2027',
                                                              'amount': '1'})
    assert bad.status_code == 400 and '2027-05-31' in bad.json()['detail']
    money = client.put('/routing/renewals', headers=ADMIN, json={'system': 'X', 'renews_on': '2027-05-31',
                                                                'amount': 'a lot'})
    assert money.status_code == 400 and 'as a number' in money.json()['detail']
    assert client.get('/routing/renewals').status_code in (401, 403)
    assert client.post('/routing/channels/files', data={'system': 'X'},
                       files={'file': ('a.csv', RIGHTFAX_3.encode(), 'text/csv')}).status_code in (401, 403)


@pytest.fixture
def renewal_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for client in _serve(monkeypatch, tmp_path, FAX_DEFAULT_COUNTRY='US'):
        yield Cli(client)


def test_the_command_line_imports_records_and_shows_the_page(renewal_cli, tmp_path):
    log = tmp_path / 'audit.log'
    log.write_text(RIGHTFAX_3, encoding='utf-8')
    routing = tmp_path / 'routing.csv'
    routing.write_text(ROUTES, encoding='utf-8')
    imported = renewal_cli('costs', 'recommendations', 'import-calls', log, '--system', 'RightFax at HQ',
                           '--licensed', '8', '--time-zone', 'UTC')
    assert imported.exit_code == 0, imported.stdout + imported.stderr
    assert 'Imported 3 calls.' in imported.stdout and '1 line was not read:' in imported.stdout
    assert 'so 5 of your 8 licensed channels never carried a call.' in ' '.join(imported.stdout.split())
    renews = (date.today() + timedelta(days=60)).isoformat()
    saved = renewal_cli('costs', 'recommendations', 'renewal', '--system', 'RightFax at HQ', '--renews', renews,
                        '--amount', '26756.71', '--product', 'RightFax 22.2', '--channels', '8')
    assert saved.exit_code == 0, saved.stdout + saved.stderr
    said = ' '.join(saved.stdout.split())
    assert 'RightFax 22.2 renews on' in said and '$26,756.71' in said
    assert 'For comparison: Cuyahoga County' in said
    routed = renewal_cli('costs', 'recommendations', 'import-routing', routing, '--system', 'RightFax at HQ')
    assert routed.exit_code == 0 and 'Imported 3 numbers.' in routed.stdout and 'Grace' in routed.stdout
    shown = renewal_cli.json('costs', 'recommendations', 'channels')
    assert shown['systems'][0]['report']['peak'] == 3
    assert renewal_cli('costs', 'recommendations', 'renewal', '--system', 'X').exit_code != 0
    assert renewal_cli('costs', 'recommendations', 'import-calls', log, '--system', 'X',
                       '--format', 'fax').exit_code != 0
    assert renewal_cli('costs', 'recommendations', 'renewal', '--remove', '--system', 'RightFax at HQ').exit_code == 0
    code = shown['systems'][0]['imports'][0]['id']
    removed = renewal_cli('costs', 'recommendations', 'channels', '--remove', code)
    assert removed.exit_code == 0 and 'No call records yet.' in removed.stdout
