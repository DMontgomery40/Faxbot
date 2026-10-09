"""Sending together over the real HTTPS stack and access policy."""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.batching import acceptance
from app.batching.store import HoldPlan


BOOTSTRAP = 'synthetic-batching-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
NUMBER = '+12025550123'


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def scoped_key(client, scopes):
    response = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': scopes})
    assert response.status_code == 200, response.text
    return {'X-API-Key': response.json()['token']}


def test_a_number_is_turned_on_only_with_the_recipients_agreement_and_its_history_is_kept(client):
    off = client.get(f'/batching/numbers/{NUMBER}', headers=ADMIN)
    assert off.status_code == 200, off.text
    view = off.json()
    assert view['enabled'] is False and view['version'] == 0 and view['max_wait_minutes'] == 10
    assert view['max_pages'] == 30 and view['mixed_senders'] is False
    assert view['state_sentence'] == 'Off: faxes to this number go straight away.'
    assert 'Phaxio' in view['route_sentence'] and view['saves_money'] is False
    refused = client.put(f'/batching/numbers/{NUMBER}', headers=ADMIN, json={'enabled': True})
    assert refused.status_code == 400 and refused.json()['detail'] == (
        'Record that the recipient agreed before turning this on.')
    on = client.put('/batching/numbers/(202) 555-0123', headers=ADMIN,
                    json={'enabled': True, 'recipient_agreed': True, 'max_wait_minutes': 5, 'version': 0})
    assert on.status_code == 200, on.text
    assert on.json()['enabled'] is True and on.json()['max_wait_minutes'] == 5
    assert on.json()['agreement']['recipient_agreed'] is True
    # On, but this installation sends through Phaxio, so nothing waits.
    assert on.json()['state_sentence'].startswith('On, but faxes go straight away')
    stale = client.put(f'/batching/numbers/{NUMBER}', headers=ADMIN, json={'enabled': True, 'version': 0})
    assert stale.status_code == 409
    gone = client.delete(f'/batching/numbers/{NUMBER}', headers=ADMIN)
    assert gone.status_code == 200 and gone.json()['enabled'] is False
    assert [change['action'] for change in gone.json()['history']] == ['off', 'on']
    assert client.get('/batching/numbers/555', headers=ADMIN).status_code == 400


def test_settings_need_settings_permissions(client):
    sender = scoped_key(client, ['fax:send', 'fax:read'])
    assert client.get(f'/batching/numbers/{NUMBER}', headers=sender).status_code == 403
    assert client.put(f'/batching/numbers/{NUMBER}', headers=sender,
                      json={'enabled': True, 'recipient_agreed': True}).status_code == 403
    check = client.get('/batching/check', params={'to': NUMBER}, headers=sender)
    assert check.status_code == 200, check.text
    assert check.json()['sends_together'] is False and check.json()['number'] == NUMBER


def test_a_held_fax_shows_in_jobs_as_waiting_and_send_now_releases_it(client, monkeypatch):
    monkeypatch.setattr(acceptance, 'hold_plan', lambda *args, **kwargs: HoldPlan(
        NUMBER, 'key:env', None, 1, kwargs['send_now'], 600))
    sent = client.post('/fax', headers=ADMIN, data={'to': NUMBER},
                       files={'file': ('note.txt', b'Synthetic fax body', 'text/plain')})
    assert sent.status_code == 202, sent.text
    job = sent.json()['id']
    listing = client.get('/admin/fax-jobs', headers=ADMIN).json()['jobs']
    together = next(item for item in listing if item['id'] == job)['together']
    assert together['state'] == 'waiting' and together['reference'] == 'Faxbot ' + job[:8]
    assert together['waiting_until'].endswith('Z') or '+00:00' in together['waiting_until']
    detail = client.get(f'/batching/faxes/{job}', headers=ADMIN).json()
    assert detail['sentence'] == 'Waiting to go with other faxes to this number.' and detail['send_now'] is False
    released = client.post(f'/batching/faxes/{job}/send-now', headers=ADMIN)
    assert released.status_code == 200, released.text
    assert released.json()['send_now'] is True
    plain = client.post('/fax', headers=ADMIN, data={'to': '+12025550199'},
                        files={'file': ('note.txt', b'Another synthetic fax', 'text/plain')})
    monkeypatch.setattr(acceptance, 'hold_plan', lambda *args, **kwargs: None)
    other = client.post('/fax', headers=ADMIN, data={'to': '+12025550188'},
                        files={'file': ('note.txt', b'A third synthetic fax', 'text/plain')}).json()['id']
    assert client.get(f'/batching/faxes/{other}', headers=ADMIN).json() == {'state': None, 'sentence': None}
    refused = client.post(f'/batching/faxes/{other}/send-now', headers=ADMIN)
    assert refused.status_code == 409 and refused.json()['detail'] == 'This fax is not waiting; it is already on its way.'
    assert plain.status_code == 202


def test_a_fax_the_caller_cannot_read_is_hidden(client):
    sender = scoped_key(client, ['inbound:read'])
    job = client.post('/fax', headers=ADMIN, data={'to': NUMBER},
                      files={'file': ('note.txt', b'Synthetic fax body', 'text/plain')}).json()['id']
    assert client.get(f'/batching/faxes/{job}', headers=sender).status_code in {403, 404}
    assert client.post(f'/batching/faxes/{job}/send-now', headers=sender).status_code in {403, 404}


def test_an_index_page_or_page_marks_need_their_own_agreement_and_show_who_recorded_it(client):
    client.put(f'/batching/numbers/{NUMBER}', headers=ADMIN, json={'enabled': True, 'recipient_agreed': True})
    view = client.get(f'/batching/numbers/{NUMBER}', headers=ADMIN).json()
    assert view['boundaries'] == 'separators' and view['boundaries_agreement'] is None
    assert view['boundaries_sentence'] == 'Each document sent together to this number follows its own separator page.'
    assert [(choice['value'], choice['agreement_text'] is None) for choice in view['boundaries_choices']] == [
        ('separators', True), ('index_page', False), ('page_headers', False)]
    assert view['boundaries_choices'][1]['agreement_text'] == (
        "This recipient has agreed to one index page listing each document's pages, "
        'instead of a separator page before each document.')
    assert view['boundaries_keeps'] == ("Only Faxbot's separator pages are left out; cover sheets and barcode "
                                        'pages inside your documents are always sent.')
    assert view['savings']['separator_pages']['pages_saved'] == 0
    refused = client.put(f'/batching/numbers/{NUMBER}', headers=ADMIN, json={'enabled': True, 'boundaries': 'index_page'})
    assert refused.status_code == 400 and refused.json()['detail'] == (
        'Record that the recipient agreed to one index page before using it.')
    assert client.put(f'/batching/numbers/{NUMBER}', headers=ADMIN,
                      json={'enabled': True, 'boundaries': 'stapled'}).status_code == 422
    on = client.put(f'/batching/numbers/{NUMBER}', headers=ADMIN,
                    json={'enabled': True, 'boundaries': 'index_page', 'boundaries_agreed': True})
    assert on.status_code == 200, on.text
    assert on.json()['boundaries'] == 'index_page' and on.json()['boundaries_agreement']['boundaries_agreed'] is True
    assert on.json()['boundaries_agreement']['by'] == 'Installation bootstrap'
    assert on.json()['boundaries_sentence'].startswith('Faxes sent together to this number start with one index page')
    assert [change['boundaries'] for change in on.json()['history']] == ['index_page', 'separators']
    marks = client.put(f'/batching/numbers/{NUMBER}', headers=ADMIN,
                       json={'enabled': True, 'boundaries': 'page_headers', 'boundaries_agreed': True}).json()
    # This installation's header prints no sending number, so its calls use separators and the card says so.
    assert marks['boundaries'] == 'page_headers' and marks['boundaries_sentence'] == (
        'Faxes to this number use separator pages for now, because marks at the top of every page need your '
        'header text and sending number set in Numbers > Sender identity.')
    off = client.delete(f'/batching/numbers/{NUMBER}', headers=ADMIN).json()
    assert off['boundaries'] == 'separators' and off['boundaries_sentence'] is None
    assert off['boundaries_agreement'] is None
    savings = client.get('/routing/savings', headers=ADMIN).json()
    assert savings['separator_pages']['estimate'] is True and savings['separator_pages']['pages_saved'] == 0
    assert savings['separator_pages']['sentence'] == 'No shared call in the last 30 days left out its separator pages.'
