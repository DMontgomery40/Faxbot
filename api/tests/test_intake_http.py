"""Intake administration over the real HTTPS stack and access policy."""
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import main
from api.tests.test_intake import Recorder, free_port, smtp
from api.tests.test_routing_http import ADMIN, BOOTSTRAP, scoped_key


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def connector(port, **changes):
    return {'name': 'Front desk', 'host': '127.0.0.1', 'port': port, 'security': 'none',
            'from_address': 'fax@clinic.example', 'recipients': ['frontdesk@clinic.example'],
            'password': 'synthetic-password', **changes}


def test_connector_lifecycle_and_test_email(client, smtp):
    recorder, port = smtp
    created = client.post('/intake/connectors', headers=ADMIN, json=connector(port))
    assert created.status_code == 201, created.text
    body = created.json()
    assert body['has_password'] is True and 'password' not in body and body['version'] == 1
    assert 'synthetic-password' not in created.text
    tested = client.post(f"/intake/connectors/{body['id']}/test", headers=ADMIN)
    assert tested.json() == {'ok': True, 'detail': 'A test email was sent to frontdesk@clinic.example.'}
    assert len(recorder.messages) == 1 and b'This is a test from Faxbot.' in recorder.messages[0][2]
    updated = client.put(f"/intake/connectors/{body['id']}", headers=ADMIN,
                         json={**connector(port, name='Front desk email'), 'password': None, 'version': 1})
    assert updated.status_code == 200 and updated.json()['has_password'] is True
    stale = client.put(f"/intake/connectors/{body['id']}", headers=ADMIN, json={**connector(port), 'version': 1})
    assert stale.status_code == 409 and stale.json()['detail'] == 'This connector changed; reload and try again.'
    bad = client.post('/intake/connectors', headers=ADMIN, json=connector(port, from_address='nobody'))
    assert bad.status_code == 400 and bad.json()['detail'].startswith('Enter the address faxes are sent from')
    unreachable = client.post('/intake/connectors', headers=ADMIN, json=connector(free_port(), name='Closed'))
    failed = client.post(f"/intake/connectors/{unreachable.json()['id']}/test", headers=ADMIN)
    assert failed.json() == {'ok': False, 'detail': 'Faxbot could not reach the email server.'}
    listing = client.get('/intake/connectors', headers=ADMIN).json()['connectors']
    assert [item['name'] for item in listing] == ['Front desk email', 'Closed']
    assert client.delete(f"/intake/connectors/{body['id']}", headers=ADMIN).json() == {'deleted': True}
    assert client.delete(f"/intake/connectors/{body['id']}", headers=ADMIN).status_code == 404


def test_queue_lists_items_with_plain_status_and_retry(client):
    from app.intake.store import IntakeStore
    from app.intake.worker import ConnectorSecrets
    runtime = main.app.state.configuration_runtime
    store = IntakeStore(runtime.manager.store.engine, ConnectorSecrets(runtime.manager.store))
    now = datetime.utcnow() - timedelta(days=1)
    identity = uuid4().hex
    with store.engine.begin() as connection:
        connection.execute(store.inbound.insert().values(id=identity, from_number='+15550109999',
            to_number='+15550100001', status='received', backend='sip', pages=2, pdf_path='/nonexistent.pdf',
            created_at=now, received_at=now, updated_at=now))
    store.feed_inbound()
    response = client.get('/intake/items', headers=ADMIN)
    assert response.status_code == 200
    (item,) = response.json()['items']
    assert item['state'] == 'received' and item['needs_action'] is True
    assert item['status'] == 'No email delivery is set up for this number yet.'
    assert response.json()['counts'] == {'received': 1, 'sending': 0, 'delivered': 0, 'failed': 0}
    retried = client.post(f"/intake/items/{item['id']}/retry", headers=ADMIN)
    assert retried.status_code == 200 and retried.json()['status'] == 'Waiting to be delivered.'
    assert client.post('/intake/items/missing/retry', headers=ADMIN).status_code == 409


def test_intake_permissions(client):
    sender = scoped_key(client, ['fax:send', 'inbound:list'])
    for method, path, body in [('GET', '/intake/items', None), ('GET', '/intake/connectors', None),
                               ('POST', '/intake/connectors', connector(25)),
                               ('POST', '/intake/items/x/retry', None),
                               ('POST', '/intake/connectors/x/test', None),
                               ('DELETE', '/intake/connectors/x', None)]:
        response = client.request(method, path, json=body, headers=sender)
        assert response.status_code == 403, (method, path, response.status_code)
