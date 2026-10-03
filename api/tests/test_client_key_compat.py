"""X-API-Key contract used by the TestFlight iOS app, SDKs and MCP servers.

Keys issued through the console and keys stored before the access migration must
both work on exactly these calls, and stop working once revoked.
"""
import base64
from datetime import datetime
import hashlib
import secrets

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient  # type: ignore

from api.app.main import app
from api.app.schema import create_database_engine
from api.tests.test_access_schema import at_revision


BOOTSTRAP = 'bootstrap_admin_only'
SCOPES = ['fax:send', 'fax:read', 'inbound:list']


@pytest.fixture
def client_environment(isolated_installation, monkeypatch):
    values = {'API_KEY': BOOTSTRAP, 'REQUIRE_API_KEY': 'true', 'PUBLIC_API_URL': 'https://testserver',
              'FAX_DISABLED': 'true', 'FAX_BACKEND': 'phaxio', 'INBOUND_ENABLED': 'true'}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return {**isolated_installation, **values}


def mobile_client():
    # Mobile and SDK clients send only X-API-Key: no cookie, Origin or CSRF header.
    return TestClient(app, base_url='https://testserver')


def store_pre_access_key(database_url, scopes):
    """Store a key exactly as the pre-access helpers did, before revision0005 runs."""
    key_id, secret, salt = secrets.token_hex(6), secrets.token_urlsafe(32), secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)

    def encode(raw):
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')

    engine = create_database_engine(database_url)
    try:
        at_revision(engine)
        with engine.begin() as connection:
            connection.execute(sa.text(
                'INSERT INTO api_keys (id, key_id, key_hash, name, owner, scopes, created_at) '
                'VALUES (:id, :key_id, :key_hash, :name, :owner, :scopes, :created_at)'),
                dict(id=secrets.token_hex(16), key_id=key_id,
                     key_hash=f'scrypt${encode(salt)}${encode(digest)}$n=16384$r=8$p=1',
                     name='iPhone', owner='front desk', scopes=','.join(scopes),
                     created_at=datetime(2025, 1, 2, 3, 4, 5)))
    finally:
        engine.dispose()
    return key_id, f'fbk_live_{key_id}_{secret}'


def send(client, token):
    return client.post('/fax', headers={'X-API-Key': token}, data={'to': '+15551234567'},
                       files={'file': ('note.txt', b'hello', 'text/plain')})


def assert_client_calls_succeed(client, token):
    headers = {'X-API-Key': token}
    sent = send(client, token)
    assert sent.status_code == 202, sent.text
    job = sent.json()
    assert job['status'] == 'queued'
    status = client.get(f"/fax/{job['id']}", headers=headers)
    assert status.status_code == 200, status.text
    assert status.json()['id'] == job['id']
    inbound = client.get('/inbound', headers=headers)
    assert inbound.status_code == 200, inbound.text
    assert isinstance(inbound.json(), list)
    assert client.get('/health', headers=headers).status_code == 200
    return job['id']


def revoke(client, key_id):
    response = client.delete(f'/admin/api-keys/{key_id}', headers={'X-API-Key': BOOTSTRAP})
    assert response.status_code == 200, response.text


def assert_revoked(client, token, job_id):
    headers = {'X-API-Key': token}
    assert send(client, token).status_code == 401
    assert client.get(f'/fax/{job_id}', headers=headers).status_code == 401
    assert client.get('/inbound', headers=headers).status_code == 401
    # Liveness stays public, so a client can tell an outage from a revoked key.
    assert client.get('/health', headers=headers).status_code == 200


def test_freshly_issued_key_serves_the_mobile_client_calls(client_environment):
    with mobile_client() as client:
        issued = client.post('/admin/api-keys', headers={'X-API-Key': BOOTSTRAP},
                             json={'name': 'iPhone', 'owner': 'front desk', 'scopes': SCOPES})
        assert issued.status_code == 200, issued.text
        key = issued.json()
        assert key['token'].startswith(f"fbk_live_{key['key_id']}_")
        job_id = assert_client_calls_succeed(client, key['token'])
        revoke(client, key['key_id'])
        assert_revoked(client, key['token'], job_id)


def test_key_stored_before_the_access_migration_keeps_serving_the_mobile_client(client_environment):
    key_id, token = store_pre_access_key(client_environment['DATABASE_URL'], SCOPES)
    with mobile_client() as client:
        job_id = assert_client_calls_succeed(client, token)
        listed = client.get('/admin/api-keys', headers={'X-API-Key': BOOTSTRAP})
        assert listed.status_code == 200, listed.text
        assert [key['scopes'] for key in listed.json() if key['key_id'] == key_id] == [sorted(SCOPES)]
        revoke(client, key_id)
        assert_revoked(client, token, job_id)
