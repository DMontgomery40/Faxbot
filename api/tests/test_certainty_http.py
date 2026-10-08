"""Uncertain sent faxes over the real HTTPS application: checks, the receipt query and send again, sent once."""
from datetime import datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from app.work.certainty import CertaintyStore, CertaintyWorker, PartnerQuestion, query_id, resend_id


BOOTSTRAP = 'synthetic-certainty-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
NUMBER = '+12025550123'


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    # The test makes items itself; the lifespan's tasks wait.
    monkeypatch.setattr(CertaintyWorker, 'step', lambda self, now=None: False)

    async def idle(self):
        return False
    monkeypatch.setattr(PartnerQuestion, 'step', idle)
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def engine():
    return main.app.state.configuration_runtime.manager.store.engine


def uncertain(client):
    """A fax accepted through POST /fax whose only attempt's outcome became unknown.

    Test mode holds every fax; this one is made an ordinary fax that went to the provider and got no answer.
    """
    sent = client.post('/fax', headers=ADMIN, data={'to': NUMBER},
                       files={'file': ('note.txt', b'Synthetic referral body', 'text/plain')})
    assert sent.status_code == 202, sent.text
    job = sent.json()['id']
    metadata = sa.MetaData()
    attempts, deliveries = (sa.Table(name, metadata, autoload_with=engine())
                            for name in ('outbound_attempts', 'outbound_deliveries'))
    now = datetime.utcnow()
    with engine().begin() as connection:
        connection.execute(attempts.insert().values(id='attempt-' + job[:24], job_id=job, sequence=1,
                                                    phase='uncertain', error_category='transport_ambiguous',
                                                    created_at=now, submitted_at=now))
        connection.execute(deliveries.update().where(deliveries.c.id == job).values(
            state='reconciliation_required', dispatch_mode='normal', attempt_id='attempt-' + job[:24]))
    store = CertaintyStore(engine())
    assert store.feed(main.app.state.access_runtime.control) == 1
    return job


def fax_ids():
    with engine().connect() as connection:
        return set(connection.execute(sa.text('SELECT id FROM fax_jobs')).scalars())


def test_an_uncertain_fax_shows_its_checks_and_a_person_settles_it_sending_it_again_once(client):
    job = uncertain(client)
    found = client.get(f'/certainty/faxes/{job}', headers=ADMIN)
    assert found.status_code == 200, found.text
    [item] = found.json()['items']
    assert [check['kind'] for check in item['checks']] == ['partner', 'call_record', 'receipt_query', 'phone_call']
    assert item['state'] == 'open' and item['why'] == 'The fax service did not confirm it accepted the fax.'
    assert client.get('/certainty/counts', headers=ADMIN).json()['open'] == 1
    assert [entry['id'] for entry in client.get('/certainty/items', headers=ADMIN).json()['items']] == [item['id']]

    page = client.get(f"/certainty/items/{item['id']}/receipt-query", headers=ADMIN)
    assert page.status_code == 200 and page.headers['content-type'] == 'application/pdf'
    before = fax_ids()
    queried = client.post(f"/certainty/items/{item['id']}/receipt-query", headers=ADMIN,
                          json={'version': item['version']})
    assert queried.status_code == 200, queried.text
    assert fax_ids() - before == {query_id(item['id'])}
    again = client.post(f"/certainty/items/{item['id']}/receipt-query", headers=ADMIN,
                        json={'version': queried.json()['version']})
    assert again.status_code == 409 and fax_ids() - before == {query_id(item['id'])}

    stale = client.post(f"/certainty/items/{item['id']}/settle", headers=ADMIN,
                        json={'outcome': 'not_delivered', 'reason': 'No fax came', 'version': item['version']})
    assert stale.status_code == 409
    settled = client.post(f"/certainty/items/{item['id']}/settle", headers=ADMIN, json={
        'outcome': 'not_delivered', 'reason': 'Their front desk says no fax came', 'send_again': True,
        'version': queried.json()['version']})
    assert settled.status_code == 200, settled.text
    new_fax = resend_id(item['id'])
    assert settled.json()['resend_fax_id'] == new_fax and new_fax in fax_ids()
    shown = client.get(f'/admin/fax-jobs/{new_fax}', headers=ADMIN)
    assert shown.status_code == 200 and shown.json()['pages'] == 1
    assert client.get(f'/certainty/faxes/{new_fax}', headers=ADMIN).json()['about'] == {'fax_id': job,
                                                                                         'kind': 'resend'}
    events = client.get(f"/certainty/items/{item['id']}/history", headers=ADMIN).json()['events']
    assert [event['kind'] for event in events] == ['opened', 'drafted', 'query_sent', 'settled']


def test_people_without_access_see_nothing_and_settings_need_settings_permissions(client):
    job = uncertain(client)
    reader = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': ['inbound:read']})
    assert reader.status_code == 200, reader.text
    other = {'X-API-Key': reader.json()['token']}
    assert client.get(f'/certainty/faxes/{job}', headers=other).json()['items'] == []
    assert client.get('/certainty/counts', headers=other).json()['open'] == 0
    assert client.get('/certainty/settings', headers=other).status_code == 403
    settings = client.get('/certainty/settings', headers=ADMIN)
    assert settings.status_code == 200 and settings.json()['settle_hours'] == 24
    saved = client.put('/certainty/settings', headers=ADMIN, json={'settle_hours': 4, 'version': 0})
    assert saved.status_code == 200 and saved.json()['settle_hours'] == 4 and saved.json()['version'] == 1
    assert client.put('/certainty/settings', headers=ADMIN, json={'settle_hours': 4, 'version': 0}).status_code == 409
