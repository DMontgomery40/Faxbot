"""Provider inbound ingest through HTTP: placement, visibility and safe retry."""
from datetime import datetime

import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.access.types import AccessUnavailableError
from app.main import app

BOOTSTRAP = 'synthetic-inbound-bootstrap-key'


def test_sinch_ingest_is_visible_and_retryable_after_a_storage_failure(isolated_installation, monkeypatch):
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    monkeypatch.setenv('REQUIRE_API_KEY', 'true')
    monkeypatch.setenv('API_KEY', BOOTSTRAP)
    payload = {'id': 'sinch-fax-1', 'from': '+15559990000', 'to': '+15550100001', 'num_pages': 1}
    headers = {'X-API-Key': BOOTSTRAP}
    with TestClient(app, base_url='http://testserver') as client:
        inbound = app.state.access_runtime.inbound
        original = inbound.accept

        def refuse(values, **kwargs):
            raise AccessUnavailableError()

        monkeypatch.setattr(inbound, 'accept', refuse)
        assert client.post('/sinch-inbound', json=payload).status_code == 503
        assert client.get('/inbound', headers=headers).json() == []
        # The provider's retry is not swallowed by the dedupe record of the failed attempt.
        monkeypatch.setattr(inbound, 'accept', original)
        assert client.post('/sinch-inbound', json=payload).json() == {'status': 'ok'}
        assert client.post('/sinch-inbound', json=payload).json() == {'status': 'ok'}
        items = client.get('/inbound', headers=headers).json()
        assert [(item['to'], item['backend'], item['mailbox']) for item in items] == [('+15550100001', 'sinch', None)]
        detail = client.get('/inbound/' + items[0]['id'], headers=headers)
        assert detail.status_code == 200 and detail.json()['fr'] == '+15559990000'
        pdf = client.get('/inbound/' + items[0]['id'] + '/pdf', headers=headers)
        assert pdf.status_code == 200 and pdf.headers['content-type'].startswith('application/pdf')


def test_startup_places_inbound_rows_stored_without_a_resource(isolated_installation, monkeypatch):
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    monkeypatch.setenv('REQUIRE_API_KEY', 'true')
    monkeypatch.setenv('API_KEY', BOOTSTRAP)
    with TestClient(app) as client:
        assert client.get('/inbound', headers={'X-API-Key': BOOTSTRAP}).json() == []
    engine = sa.create_engine(isolated_installation['DATABASE_URL'])
    try:
        now = datetime.utcnow()
        with engine.begin() as c:
            c.execute(sa.text("INSERT INTO inbound_faxes (id, status, backend, created_at, received_at, updated_at) "
                              "VALUES ('pre-existing', 'received', 'phaxio', :now, :now, :now)"), {'now': now})
    finally:
        engine.dispose()
    with TestClient(app) as client:
        items = client.get('/inbound', headers={'X-API-Key': BOOTSTRAP}).json()
        assert [item['id'] for item in items] == ['pre-existing']
