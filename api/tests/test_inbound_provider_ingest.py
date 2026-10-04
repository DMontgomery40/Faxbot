"""Provider inbound ingest through HTTP: placement, visibility and safe retry."""
from datetime import datetime

import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.access.types import AccessUnavailableError
from app.inbound.acquisition import ImportStore
from app.main import app
from api.tests.test_inbound_acquisition import pdf_bytes, providers, step  # noqa: F401 (fixture)

BOOTSTRAP = 'synthetic-inbound-bootstrap-key'


def test_sinch_ingest_is_visible_and_retryable_after_a_storage_failure(isolated_installation, monkeypatch, providers):
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    monkeypatch.setenv('REQUIRE_API_KEY', 'true')
    monkeypatch.setenv('API_KEY', BOOTSTRAP)
    monkeypatch.setenv('SINCH_PROJECT_ID', 'synthetic-project')
    monkeypatch.setenv('SINCH_API_KEY', 'synthetic-sinch-key')
    monkeypatch.setenv('SINCH_API_SECRET', 'synthetic-sinch-secret')
    providers.sinch('sinch-fax-1', to='+15550100001', **{'from': '+15559990000'})
    document = pdf_bytes()
    providers.files[('sinch', 'sinch-fax-1')] = (200, document)
    payload = {'id': 'sinch-fax-1', 'from': '+15559990000', 'to': '+15550100001', 'num_pages': 1}
    headers = {'X-API-Key': BOOTSTRAP}
    with TestClient(app, base_url='http://testserver') as client:
        original = ImportStore.begin

        def refuse(self, **kwargs):
            raise AccessUnavailableError()

        monkeypatch.setattr(ImportStore, 'begin', refuse)
        assert client.post('/sinch-inbound', json=payload).status_code == 503
        assert client.get('/inbound', headers=headers).json() == []
        # Nothing was recorded, so the provider's retry is a first arrival, not a swallowed duplicate.
        monkeypatch.setattr(ImportStore, 'begin', original)
        assert client.post('/sinch-inbound', json=payload).json() == {'status': 'ok'}
        assert client.post('/sinch-inbound', json=payload).json() == {'status': 'ok'}
        items = client.get('/inbound', headers=headers).json()
        assert [(item['to'], item['backend'], item['mailbox'], item['status']) for item in items] == [
            ('+15550100001', 'sinch', None, 'waiting')]
        assert client.get('/inbound/' + items[0]['id'] + '/pdf', headers=headers).status_code == 404
        assert step() is True
        detail = client.get('/inbound/' + items[0]['id'], headers=headers)
        assert detail.status_code == 200 and detail.json()['fr'] == '+15559990000'
        assert detail.json()['status'] == 'received' and detail.json()['provider_fax_id'] == 'sinch-fax-1'
        pdf = client.get('/inbound/' + items[0]['id'] + '/pdf', headers=headers)
        assert pdf.status_code == 200 and pdf.content == document


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
        # A fax recorded before acquisition records keeps its stored status and reads as received.
        assert items[0]['status_text'] == 'Received.' and items[0]['is_test'] is False
        assert items[0]['can_fetch_again'] is False


def test_older_stand_in_documents_read_as_not_received_without_changing_them(isolated_installation, monkeypatch,
                                                                            tmp_path):
    """Earlier versions stored a fixed stand-in PDF when a fetch failed; it is never shown or emailed as the fax."""
    import hashlib
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    monkeypatch.setenv('REQUIRE_API_KEY', 'true')
    monkeypatch.setenv('API_KEY', BOOTSTRAP)
    stand_in = b'%PDF-1.4\n% placeholder inbound\n%%EOF'
    document = tmp_path / 'old.pdf'
    document.write_bytes(stand_in)
    with TestClient(app) as client:
        assert client.get('/inbound', headers={'X-API-Key': BOOTSTRAP}).json() == []
    engine = sa.create_engine(isolated_installation['DATABASE_URL'])
    try:
        now = datetime.utcnow()
        with engine.begin() as c:
            c.execute(sa.text(
                "INSERT INTO inbound_faxes (id, status, backend, provider_sid, sha256, pdf_path, pdf_token, "
                "pdf_token_expires_at, created_at, received_at, updated_at) VALUES ('stand-in', 'received', 'phaxio', "
                "'777', :digest, :path, 'synthetic-token', :later, :now, :now, :now)"),
                {'digest': hashlib.sha256(stand_in).hexdigest(), 'path': str(document), 'now': now,
                 'later': datetime(2100, 1, 1)})
    finally:
        engine.dispose()
    with TestClient(app) as client:
        [item] = client.get('/inbound', headers={'X-API-Key': BOOTSTRAP}).json()
        assert item['status'] == 'failed' and item['can_fetch_again'] is False and item['sha256'] is None
        assert item['status_text'] == 'Faxbot never received the document for this fax; ask the sender to send it again.'
        for params, headers in (({}, {'X-API-Key': BOOTSTRAP}), ({'token': 'synthetic-token'}, {})):
            refused = client.get('/inbound/stand-in/pdf', params=params, headers=headers)
            assert refused.status_code == 404 and refused.json()['detail'] == 'The document has not been received yet.'
        from api.tests.test_inbound_acquisition import feed
        assert feed() == 0
    engine = sa.create_engine(isolated_installation['DATABASE_URL'])
    try:
        with engine.connect() as c:
            assert c.execute(sa.text("SELECT status FROM inbound_faxes WHERE id = 'stand-in'")).scalar_one() == 'received'
    finally:
        engine.dispose()
