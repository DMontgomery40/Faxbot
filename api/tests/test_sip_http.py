"""SIP trunk administration over the real HTTPS stack and access policy."""
import json
import os
import stat
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main, sip_calls
from app.ami import ami_client


BOOTSTRAP = 'synthetic-sip-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
PASSWORD = 'synthetic-Trunk-Pass!42'
TRUNK = {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser', 'SIP_TRUNK_PASSWORD': PASSWORD,
         'SIP_TRUNK_CALLER_ID': '+15555550100', 'SIP_TRUNK_DIDS': '+15555550100,+15555550101'}


def _client(monkeypatch, extra):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'MAX_REQUESTS_PER_MINUTE': '0', **extra}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    return TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'})


@pytest.fixture
def client(isolated_installation, monkeypatch):
    with _client(monkeypatch, TRUNK) as client:
        yield client


@pytest.fixture
def bare_client(isolated_installation, monkeypatch):
    with _client(monkeypatch, {}) as client:
        yield client


def scoped_key(client, scopes):
    response = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': scopes})
    assert response.status_code == 200, response.text
    return {'X-API-Key': response.json()['token']}


def test_presets_list_documented_carriers_with_dated_sources(client):
    response = client.get('/admin/sip/presets', headers=ADMIN)
    assert response.status_code == 200
    presets = {preset['id']: preset for preset in response.json()['presets']}
    assert set(presets) == {'telnyx', 'signalwire', 'sinch', 'anveo', 'flowroute', 'custom'}
    assert presets['anveo']['auth_modes'] == ['ip'] and presets['signalwire']['needs_host'] is True
    assert all(source['read_on'] == '2026-10-03' for preset in presets.values() for source in preset['sources'])


def test_status_without_a_trunk_says_so_in_one_sentence(bare_client):
    body = bare_client.get('/admin/sip/status', headers=ADMIN).json()
    assert body == {'configured': False, 'applied': False, 'asterisk_connected': False,
                    'registration': 'unknown', 'registration_text': 'Registration status is not available.',
                    'reachability': 'unknown',
                    'reachability_text': 'Faxbot cannot tell yet whether the carrier answers.',
                    'message': 'No SIP trunk is set up. Choose your carrier to start.'}


def test_apply_writes_the_private_trunk_file_and_status_never_shows_the_password(client, isolated_installation):
    before = client.get('/admin/sip/status', headers=ADMIN)
    assert before.status_code == 200
    assert before.json()['applied'] is False
    assert before.json()['message'] == 'Apply these settings to Asterisk, then restart the Asterisk service.'
    applied = client.post('/admin/sip/apply', headers=ADMIN)
    assert applied.status_code == 200, applied.text
    assert applied.json()['message'] == 'Saved for Asterisk. Restart the Asterisk service to use these settings.'
    path = os.path.join(isolated_installation['FAX_DATA_DIR'], 'asterisk', 'pjsip.conf')
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert f'password={PASSWORD}' in open(path).read()
    after = client.get('/admin/sip/status', headers=ADMIN)
    body = after.json()
    assert body['applied'] is True and body['preset_label'] == 'Telnyx' and body['host'] == 'sip.telnyx.com'
    assert body['dids'] == ['+15555550100', '+15555550101'] and body['missing'] == []
    assert body['message'] == 'Faxbot is not connected to Asterisk.'
    assert PASSWORD not in after.text + before.text + applied.text
    settings = client.get('/admin/settings', headers=ADMIN)
    assert settings.status_code == 200
    assert PASSWORD not in settings.text
    assert settings.json()['sip']['trunk']['password'] == '***'


def test_status_reports_registration_and_reachability_from_asterisk(client, monkeypatch):
    calls = []

    async def status_query(fields, *, collect=False):
        calls.append(fields)
        if fields['Action'] == 'PJSIPShowRegistrationsOutbound':
            return ({'response': 'Success', 'value': '', 'message': ''},
                    [{'ObjectName': 'trunk-registration', 'Status': 'Registered'}])
        return {'response': 'Success', 'value': 'NOT_INUSE', 'message': ''}, []

    client.post('/admin/sip/apply', headers=ADMIN)
    monkeypatch.setattr(ami_client, 'status_query', status_query)
    monkeypatch.setattr(ami_client._connected, 'is_set', lambda: True)
    body = client.get('/admin/sip/status', headers=ADMIN).json()
    assert (body['registration'], body['reachability'], body['message']) == ('registered', 'reachable',
                                                                             'The trunk is ready.')
    assert calls == [{'Action': 'PJSIPShowRegistrationsOutbound'},
                     {'Action': 'Getvar', 'Variable': 'DEVICE_STATE(PJSIP/trunk-endpoint)'}]

    async def rejected(fields, *, collect=False):
        if fields['Action'] == 'PJSIPShowRegistrationsOutbound':
            return ({'response': 'Success', 'value': '', 'message': ''},
                    [{'ObjectName': 'trunk-registration', 'Status': 'Rejected'}])
        return {'response': 'Error', 'value': '', 'message': 'Permission denied'}, []

    monkeypatch.setattr(ami_client, 'status_query', rejected)
    body = client.get('/admin/sip/status', headers=ADMIN).json()
    assert body['registration'] == 'rejected'
    assert body['message'] == ('Asterisk does not let Faxbot read trunk status. '
                               'Restart the Asterisk service to update its access.')


def test_apply_with_missing_settings_names_fields_only(isolated_installation, monkeypatch):
    with _client(monkeypatch, {**TRUNK, 'SIP_TRUNK_PASSWORD': ''}) as client:
        response = client.post('/admin/sip/apply', headers=ADMIN)
        assert response.status_code == 400
        assert response.json()['detail'] == 'Fill in the password before applying.'


def test_recent_calls_are_paginated_and_permissioned(client):
    engine = client.app.state.configuration_runtime.manager.store.engine
    records = sip_calls.SipCallRecords(engine)
    for index in range(3):
        records.record_submission({'JobID': f'{index:032x}', 'AttemptID': f'{index + 10:032x}',
                                   'Called': '+15555550123', 'CallerID': '+15555550100', 'Preset': 'telnyx'},
                                  now=datetime(2026, 10, 3, 12, index))
    first = client.get('/admin/sip/calls?limit=2', headers=ADMIN)
    assert first.status_code == 200, first.text
    page = first.json()
    assert [item['attempt_id'] for item in page['items']] == [f'{12:032x}', f'{11:032x}']
    assert page['items'][0]['disposition'] == 'ambiguous'
    rest = client.get('/admin/sip/calls', params={'limit': 2, 'cursor': page['next_cursor']}, headers=ADMIN).json()
    assert [item['attempt_id'] for item in rest['items']] == [f'{10:032x}'] and rest['next_cursor'] is None
    assert client.get('/admin/sip/calls?cursor=broken', headers=ADMIN).status_code == 400
    sender = scoped_key(client, ['fax:send'])
    for method, path in (('GET', '/admin/sip/calls'), ('GET', '/admin/sip/status'), ('POST', '/admin/sip/apply'),
                         ('GET', '/admin/sip/presets')):
        assert client.request(method, path, headers=sender).status_code == 403, path
        assert client.request(method, path).status_code == 401, path


def test_each_route_declares_the_permission_the_console_relies_on():
    from tests.test_route_policy_coverage import _declared, _routes
    declared = {key: [(item.permission, item.audit) for item in _declared(calls)]
                for key, calls in _routes() if key[1].startswith('/admin/sip/')}
    assert declared == {('GET', '/admin/sip/presets'): [('providers:read', False)],
                        ('GET', '/admin/sip/status'): [('providers:read', False)],
                        ('POST', '/admin/sip/apply'): [('providers:write', False)],
                        ('GET', '/admin/sip/calls'): [('diagnostics:read', False)]}


def test_console_save_then_apply_writes_the_new_trunk(bare_client, isolated_installation):
    current = bare_client.get('/admin/settings', headers=ADMIN).json()
    saved = bare_client.put('/admin/settings', headers=ADMIN, json={
        'expected_revision_id': current['_meta']['desired_revision_id'], 'sip_trunk_preset': 'flowroute',
        'sip_trunk_username': '12345678', 'sip_trunk_password': PASSWORD, 'sip_trunk_caller_id': '+15555550100',
        'sip_trunk_dids': '+15555550100', 'sip_fax_preference_header': True,
        'sip_external_address': '203.0.113.10', 'asterisk_inbound_secret': 'synthetic-inbound-secret'})
    assert saved.status_code == 200, saved.text
    assert saved.json()['_meta']['apply_state'] == 'applied'
    applied = bare_client.post('/admin/sip/apply', headers=ADMIN)
    assert applied.status_code == 200, applied.text
    folder = os.path.join(isolated_installation['FAX_DATA_DIR'], 'asterisk')
    text = open(os.path.join(folder, 'pjsip.conf')).read()
    assert 'Faxbot SIP trunk for Flowroute' in text and 'username=12345678' in text
    assert 'external_signaling_address=203.0.113.10' in text
    assert open(os.path.join(folder, 'inbound.secret')).read() == 'synthetic-inbound-secret'
    view = bare_client.get('/admin/settings', headers=ADMIN).json()['sip']['trunk']
    assert view['preset'] == 'flowroute' and view['password'] == '***' and view['fax_preference_header'] is True
    status = bare_client.get('/admin/sip/status', headers=ADMIN).json()
    assert status['applied'] is True and status['dids'] == ['+15555550100']
