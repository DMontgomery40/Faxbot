"""Mobile pairing: a console-minted single-use code becomes one phone's own revocable device key."""
import re

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from api.tests.test_access_management_http import B, ORIGIN, _environment, policy_version, ready_user

DEVICE_CEILING = [{'permission': permission, 'resource_id': 'installation'} for permission in sorted(
    ['fax:send', 'fax:read', 'fax:document', 'inbound:list', 'inbound:read', 'inbound:document'])]


@pytest.fixture
def client(monkeypatch, tmp_path):
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    with TestClient(main_module.app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def fresh_pair_limits(monkeypatch):
    monkeypatch.setattr(main_module, '_PAIR_ATTEMPTS', {})


def mint(client, headers=B):
    response = client.post('/admin/tunnel/pair', headers=headers, json={})
    assert response.status_code == 200, response.text
    assert re.fullmatch(r'[0-9]{6}', response.json()['code'])
    return response.json()['code']


def pair(client, code, device_name='Synthetic iPhone'):
    # The phone sends no credential, Origin or CSRF header.
    return client.post('/mobile/pair', json={'code': code, 'device_name': device_name}, headers={'Origin': ''})


def test_paired_device_sends_faxes_until_its_key_is_revoked(client, monkeypatch):
    monkeypatch.setenv('MOBILE_LOCAL_BASE', 'http://192.0.2.20:8080')
    admin, admin_principal = ready_user(client, 'admin', role='role_administrator')
    code = mint(client, admin.headers())
    paired = pair(client, code)
    assert paired.status_code == 200, paired.text
    assert paired.headers['cache-control'] == 'no-store'
    body = paired.json()
    assert body['base_urls'] == {'local': 'http://192.0.2.20:8080', 'tunnel': None, 'public': ORIGIN}
    token = body['token']
    assert token.startswith('fbk_live_')
    assert pair(client, code).status_code == 403

    device = {'X-API-Key': token, 'Origin': ''}
    sent = client.post('/fax', headers=device, data={'to': '+15551230001'},
                       files={'file': ('note.txt', b'synthetic paired note', 'text/plain')})
    assert sent.status_code == 202, sent.text
    assert set(sent.json()) >= {'id', 'status'}
    job = '/fax/' + sent.json()['id']
    assert client.get(job, headers=device).status_code == 200
    inbound = client.get('/inbound', headers=device)
    assert inbound.status_code == 200 and inbound.json() == []
    assert client.get('/health', headers={'X-API-Key': ''}).json() == {'status': 'ok'}

    keys = client.get('/access/keys', headers=B).json()['items']
    (key,) = [item for item in keys if token.startswith('fbk_live_' + item['id'])]
    assert key['principal']['display_name'] == 'Device: Synthetic iPhone' and key['principal']['kind'] == 'integration'
    assert key['name'] == 'Synthetic iPhone' and key['ceiling'] == DEVICE_CEILING and key['revoked_at'] is None
    issued = client.get('/access/audit', headers=B, params={'operation': 'issue_integration_key'}).json()['items']
    assert [(row['actor']['id'], row['target']['kind'], row['outcome']) for row in issued] == [
        (admin_principal['id'], 'binding', 'allowed')]
    operations = {row['operation'] for row in client.get('/access/audit', headers=B, params={'limit': 200}).json()['items']}
    assert {'capability.issue', 'capability.consume'} <= operations

    revoked = client.post(f"/access/keys/{key['id']}/revoke", headers=B,
                          json={'version': key['version'], 'expected_policy_version': policy_version(client)})
    assert revoked.status_code == 200, revoked.text
    assert client.get(job, headers=device).status_code == 401
    assert client.post('/fax', headers=device, data={'to': '+15551230001'},
                       files={'file': ('note.txt', b'synthetic paired note', 'text/plain')}).status_code == 401


def test_every_pairing_failure_is_a_plain_403(client):
    code = mint(client)
    attempts = [
        client.post('/mobile/pair', json={'code': '000000' if code != '000000' else '111111', 'device_name': 'x'}),
        client.post('/mobile/pair', json={'code': 'abcdef'}),
        client.post('/mobile/pair', json={'device_name': 'no code'}),
        client.post('/mobile/pair', content=b'{not json', headers={'Content-Type': 'application/json'}),
    ]
    assert [response.status_code for response in attempts] == [403] * 4
    assert {response.json()['detail'] for response in attempts} == {main_module._PAIR_REFUSED}
    # A fifth try from this address still works; the sixth is refused even with the right code.
    assert pair(client, code).status_code == 200
    limited = pair(client, mint(client))
    assert limited.status_code == 403 and limited.json()['detail'] == 'Too many pairing attempts. Wait a minute and try again.'


def test_pairing_attempts_are_capped_across_addresses(monkeypatch):
    monkeypatch.setattr(main_module, '_PAIR_ATTEMPTS', {})
    allowed = [main_module._pair_attempt_allowed(f'198.51.100.{index}') for index in range(40)]
    assert allowed.count(True) == main_module._PAIR_ATTEMPTS_TOTAL


def test_minting_requires_tunnels_pair_and_the_right_to_issue_the_device_key(client):
    assert client.post('/admin/tunnel/pair', json={}).status_code == 401
    plain, _ = ready_user(client, 'plain')
    assert plain.post('/admin/tunnel/pair', {}).status_code == 403
    role = client.post('/access/roles', headers=B, json={'name': 'Pairing only', 'permissions': ['tunnels:pair'],
        'expected_policy_version': policy_version(client)}).json()['role']
    pairer, principal = ready_user(client, 'pairer')
    granted = client.post('/access/assignments', headers=B, json={
        'subject': {'kind': 'principal', 'id': principal['id'],
                    'version': client.get(f"/access/users/{principal['id']}", headers=B).json()['version']},
        'role': {'id': role['id'], 'version': role['version']}, 'resource_id': 'installation',
        'expected_policy_version': policy_version(client)})
    assert granted.status_code == 200, granted.text
    # tunnels:pair alone cannot issue the device key, so no code is minted.
    assert pairer.post('/admin/tunnel/pair', {}).status_code == 403


def test_a_code_dies_with_the_session_that_minted_it(client):
    admin, _ = ready_user(client, 'admin', role='role_administrator')
    code = mint(client, admin.headers())
    assert admin.post('/auth/logout', {}).status_code == 200
    assert pair(client, code).status_code == 403
