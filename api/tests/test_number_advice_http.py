"""Number advice over the real HTTPS stack and access policy: the check before a first fax never stops a fax.

The NPPES registry is never called: its network read is replaced by fixtures shaped like the registry API
(``fixtures/nppes``), or by one that fails the test.
"""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.routing import nppes


BOOTSTRAP = 'synthetic-number-advice-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
FIXTURES = Path(__file__).parent / 'fixtures' / 'nppes'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def registry(monkeypatch, *answers):
    asked = []

    def fetch(params, *, timeout=10.0):
        asked.append(dict(params))
        answer = answers[min(len(asked), len(answers)) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer
    monkeypatch.setattr(nppes, '_fetch', fetch)
    return asked


def scoped_key(client, scopes):
    response = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': scopes})
    assert response.status_code == 200, response.text
    return {'X-API-Key': response.json()['token']}


@pytest.mark.parametrize('answer', [fixture('search_organization.json'), fixture('errors.json'),
                                    httpx.ReadTimeout('synthetic')])
def test_the_check_before_a_first_fax_warns_a_sender_and_the_fax_is_still_accepted(client, monkeypatch, answer):
    asked = registry(monkeypatch, answer)
    sender = scoped_key(client, ['fax:send', 'fax:read'])
    check = client.get('/routing/recipient-check', headers=sender,
                       params={'to': '(303) 555-0199', 'name': 'Synthetic Health Clinic'})
    assert check.status_code == 200, check.text
    body = check.json()
    assert body['number'] == '+13035550199' and body['first_send'] is True
    if isinstance(answer, dict) and 'results' in answer:
        assert body['warning'] and body['sentence'] == (
            "NPPES lists +1 303-555-0111 as SYNTHETIC HEALTH CLINIC's fax number, not this one.")
    else:
        assert body['state'] == 'not_checked' and not body['warning']
    assert len(asked) == 1
    # The warning never stops the fax: the real acceptance path takes it.
    sent = client.post('/fax', headers=sender, data={'to': '+13035550199'},
                       files={'file': ('note.txt', b'Synthetic fax body', 'text/plain')})
    assert sent.status_code == 202, sent.text
    again = client.get('/routing/recipient-check', headers=sender, params={'to': '+13035550199', 'name': 'X'})
    assert again.json()['first_send'] is False and again.json()['state'] == 'sent_before' and len(asked) == 1


def test_people_who_may_not_send_get_no_check(client, monkeypatch):
    registry(monkeypatch, AssertionError('no registry call for a refused person'))
    reader = scoped_key(client, ['fax:read'])
    assert client.get('/routing/recipient-check', headers=reader, params={'to': '+13035550199'}).status_code == 403


def test_your_npi_is_saved_read_once_and_removed_with_settings_permissions(client, monkeypatch):
    asked = registry(monkeypatch, fixture('own_record.json'))
    sender = scoped_key(client, ['fax:send', 'fax:read'])
    assert client.post('/routing/npi', headers=sender, json={'npi': '1234567893'}).status_code == 403
    assert client.get('/routing/npi', headers=sender).status_code == 403
    refused = client.post('/routing/npi', headers=ADMIN, json={'npi': '1234567890'})
    assert refused.status_code == 400 and refused.json()['detail'] == (
        'Enter the ten-digit NPI exactly as it is on the NPI record.')
    added = client.post('/routing/npi', headers=ADMIN, json={'npi': '1234567893', 'label': 'Denver office'})
    assert added.status_code == 200, added.text
    item = added.json()['npis'][0]
    assert item['label'] == 'Denver office' and item['name'] == 'OUR SYNTHETIC PRACTICE'
    assert '+1 720-555-0199' in {number['display'] for number in item['numbers']}
    assert asked == [{'number': '1234567893'}]
    registry(monkeypatch, httpx.ConnectError('synthetic'))
    failed = client.post('/routing/npi/check', headers=ADMIN)
    assert failed.status_code == 200 and failed.json()['problem'].startswith('Faxbot could not reach NPPES')
    assert failed.json()['npis'][0]['numbers']   # the last read stays
    gone = client.delete('/routing/npi/1234567893', headers=ADMIN)
    assert gone.status_code == 200 and gone.json()['npis'] == []
    assert client.delete('/routing/npi/1234567893', headers=ADMIN).status_code == 400


def test_number_placement_and_site_advice_are_settings_reads(client):
    sender = scoped_key(client, ['fax:send', 'fax:read'])
    for path in ('/routing/recommendations/numbers', '/routing/recommendations/sites'):
        assert client.get(path, headers=sender).status_code == 403
    placed = client.get('/routing/recommendations/numbers', headers=ADMIN)
    assert placed.status_code == 200, placed.text
    assert placed.json()['state'] == 'no_numbers' and placed.json()['estimate'] is True
    sites = client.get('/routing/recommendations/sites', headers=ADMIN)
    assert sites.status_code == 200 and sites.json()['caller_id'].startswith('Faxbot never changes caller ID')


def test_a_carriers_rate_file_is_imported_and_a_bad_one_is_refused(client):
    deck = b'destination,prefix,rate_inter,rate_intra,billing\nUSA,1303555,0.002,0.01,1-1\n'
    sender = scoped_key(client, ['fax:send', 'fax:read'])
    assert client.post('/routing/jurisdiction-rates', headers=sender, data={'carrier': 'anveo'},
                       files={'file': ('deck.csv', deck, 'text/csv')}).status_code == 403
    imported = client.post('/routing/jurisdiction-rates', headers=ADMIN,
                           data={'carrier': 'anveo', 'source_url': 'https://www.anveo.com/anveodirect.standard.csv',
                                 'read_on': '2026-10-08'},
                           files={'file': ('deck.csv', deck, 'text/csv')})
    assert imported.status_code == 200, imported.text
    assert imported.json()['prices'][0] | {'imported_at': None} == {
        'route': 'sip-anveo', 'carrier': 'AnveoDirect', 'rows': 1, 'differ': 1,
        'source_url': 'https://www.anveo.com/anveodirect.standard.csv', 'read_on': '2026-10-08', 'imported_at': None}
    bad = client.post('/routing/jurisdiction-rates', headers=ADMIN, data={'carrier': 'anveo'},
                      files={'file': ('deck.csv', b'prefix,price\n1303,0.01\n', 'text/csv')})
    assert bad.status_code == 400 and 'within one state' in bad.json()['detail']



def test_a_first_fax_keeps_the_stored_nppes_warning_with_the_fax_without_asking_the_registry(client, monkeypatch):
    from app.routing.background import installation_engine
    asked = registry(monkeypatch, AssertionError('the acceptance check never asks the registry'))
    engine, _ = installation_engine(main.app)
    nppes.NppesStore(engine).record(nppes.records_from(fixture('search_organization.json')), 'lookup')
    named = client.patch('/routing/destinations/+13035550121', headers=ADMIN,
                         json={'display_name': 'Synthetic Health Clinic'})
    assert named.status_code == 200, named.text
    sent = client.post('/fax', headers=ADMIN, data={'to': '+13035550121'},
                       files={'file': ('note.txt', b'Synthetic fax body', 'text/plain')})
    assert sent.status_code == 202, sent.text
    cost = client.get(f"/routing/faxes/{sent.json()['id']}/cost", headers=ADMIN)
    assert cost.status_code == 200, cost.text
    assert cost.json()['recipient_warning']['sentence'] == (
        'This number is listed for SYNTHETIC HEALTH IMAGING LLC in NPPES, not Synthetic Health Clinic.')
    listed = client.get('/routing/fax-costs', headers=ADMIN, params={'ids': sent.json()['id']}).json()['costs']
    assert listed[sent.json()['id']]['recipient_warning']['state'] == 'listed_for_other'
    other = client.post('/fax', headers=ADMIN, data={'to': '+13035550199'},
                        files={'file': ('note.txt', b'Synthetic fax body', 'text/plain')})
    assert other.status_code == 202
    assert client.get(f"/routing/faxes/{other.json()['id']}/cost", headers=ADMIN).json()['recipient_warning'] is None
    assert asked == []
