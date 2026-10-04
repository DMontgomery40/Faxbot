"""SIP trunk administration over the real HTTPS stack and access policy."""
import asyncio
import json
import os
import stat
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main, sip_calls, sip_http, sip_trunk, stun
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


# What STUN shows from behind an ordinary router: a public address that is not
# the container's own, with port numbers changed on the way out.
ROUTER = stun.Probe(public_ip='198.51.100.7', local_ip='172.18.0.5', local_port=40000,
                    mapped=(('stun.telnyx.com:3478', 61001), ('stun.cloudflare.com:3478', 61002)))


@pytest.fixture(autouse=True)
def network(monkeypatch):
    """No test reaches the internet: the STUN probe answers from this fixture."""
    seen = {'result': ROUTER, 'servers': []}

    def probe(servers, **_):
        seen['servers'].append(tuple(servers))
        return seen['result']
    monkeypatch.setattr(stun, 'probe', probe)
    monkeypatch.setattr(sip_http, '_probes', {})
    return seen


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
                    'registration': 'unknown', 'registration_transport': None,
                    'registration_text': 'Registration status is not available.',
                    'reachability': 'unknown',
                    'reachability_text': 'Faxbot cannot tell yet whether the carrier answers.',
                    'round_trip_ms': None, 'internet_address': None, 'behind_router': None, 'port_numbers': None,
                    'public_address_text': None, 'ports_text': None, 'last_call_text': None, 'last_call_at': None,
                    'address_changed': False, 'last_call_verdict': None, 'suggest_audio': False,
                    'engine_managed': False, 'engine_restarting': False, 'in_use': False,
                    'handover_ready': None, 'handover_text': None, 't38_off_reason': None, 't38_off_at': None,
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
    assert body['message'] == 'Faxbot connects to its fax engine when the SIP trunk is the provider in use.'
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
                    [{'ObjectName': 'trunk-registration', 'Status': 'Registered', 'Transport': 'transport-tls'}])
        if fields['Action'] == 'PJSIPShowContacts':
            return ({'response': 'Success', 'value': '', 'message': ''},
                    [{'ObjectName': 'trunk-aor@@1f2e3d', 'Status': 'Reachable', 'RoundtripUsec': '38412'}])
        return {'response': 'Success', 'value': 'NOT_INUSE', 'message': ''}, []

    client.post('/admin/sip/apply', headers=ADMIN)
    monkeypatch.setattr(ami_client, 'status_query', status_query)
    monkeypatch.setattr(ami_client._connected, 'is_set', lambda: True)
    body = client.get('/admin/sip/status', headers=ADMIN).json()
    assert (body['registration'], body['reachability'], body['message']) == ('registered', 'reachable',
                                                                             'The trunk is ready.')
    assert calls == [{'Action': 'PJSIPShowRegistrationsOutbound'},
                     {'Action': 'Getvar', 'Variable': 'DEVICE_STATE(PJSIP/trunk-endpoint)'},
                     {'Action': 'PJSIPShowContacts'}]
    # The transport Asterisk registered over, the check round trip and the address in use, in plain words.
    assert body['registration_transport'] == 'tls' and body['transport'] == 'tls'
    assert body['registration_text'] == "The carrier accepted Faxbot's registration over TLS."
    assert body['reachability_text'] == "The carrier answered Faxbot's check in 38 ms."
    assert body['internet_address'] == '198.51.100.7' and body['behind_router'] is True
    assert body['public_address_text'] == ("Faxbot's internet address is 198.51.100.7; your network changes port "
                                           "numbers, so Telnyx has to follow Faxbot's packets, and the first test "
                                           "fax shows whether it does.")
    assert body['ports_text'] == 'No ports need to be opened or forwarded.'

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


def test_status_names_the_address_and_port_behavior_stun_finds(client, network):
    client.post('/admin/sip/apply', headers=ADMIN)
    network['result'] = stun.Probe(public_ip='198.51.100.7', local_ip='172.18.0.5', local_port=40000,
                                   mapped=(('a', 40000), ('b', 40000)))
    sip_http._probes.clear()
    body = client.get('/admin/sip/status', headers=ADMIN).json()
    assert body['port_numbers'] == 'preserved'
    assert body['public_address_text'] == ("Faxbot's internet address is 198.51.100.7, "
                                           "and your network keeps port numbers.")
    network['result'] = stun.Probe(public_ip=None, local_ip=None, local_port=40000, mapped=(('a', None),))
    sip_http._probes.clear()
    body = client.get('/admin/sip/status', headers=ADMIN).json()
    assert body['internet_address'] is None and body['behind_router'] is None
    assert body['public_address_text'] == ("Faxbot could not learn its internet address, so Telnyx must follow "
                                           "Faxbot's packets; the first test fax shows whether it does.")
    assert network['servers'][0] == ('stun.telnyx.com:3478', 'stun.cloudflare.com:3478')


def test_a_typed_address_that_differs_from_stun_is_called_out(isolated_installation, monkeypatch):
    with _client(monkeypatch, {**TRUNK, 'SIP_EXTERNAL_ADDRESS': '203.0.113.10'}) as client:
        body = client.get('/admin/sip/status', headers=ADMIN).json()
    assert body['public_address'] == '203.0.113.10'
    assert body['public_address_text'] == ('The address you entered differs from the one Faxbot sees from the '
                                           'internet (198.51.100.7).')


def test_server_ip_sign_in_behind_a_router_is_refused_in_one_sentence(isolated_installation, monkeypatch, network):
    refusal = ('Your Faxbot runs behind a router, so sign in with a username and password; '
               'server IP sign-in needs a public address.')
    with _client(monkeypatch, {**TRUNK, 'SIP_TRUNK_AUTH': 'ip'}) as client:
        response = client.post('/admin/sip/apply', headers=ADMIN)
        assert response.status_code == 400 and response.json()['detail'] == refusal
        status = client.get('/admin/sip/status', headers=ADMIN).json()
        assert status['message'] == refusal and status['ports_text'] == refusal
        assert status['registration_text'] == 'Registration status is not available.'
        # On a host with its own public address, the same sign-in is applied.
        network['result'] = stun.Probe(public_ip='198.51.100.7', local_ip='198.51.100.7', local_port=40000,
                                       mapped=(('a', 40000),))
        assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
        sip_http._probes.clear()
        status = client.get('/admin/sip/status', headers=ADMIN).json()
        assert status['ports_text'] is None and status['message'] != refusal
        assert status['public_address_text'] == ("Faxbot's internet address is 198.51.100.7, "
                                                 "and Faxbot is not behind a router.")


def test_status_shows_the_newest_call_in_one_sentence(client):
    engine = client.app.state.configuration_runtime.manager.store.engine
    records = sip_calls.SipCallRecords(engine)
    records.record_inbound_event({'UniqueID': '1791075343.12', 'Caller': '+13035550100', 'DID': '+15555550100',
                                  'Status': 'FAILED', 'Error': 'The call dropped prematurely', 'Pages': '0',
                                  'Mode': 'T38', 'Answered': '1791075343', 'Ended': '1791075357'})
    body = client.get('/admin/sip/status', headers=ADMIN).json()
    assert body['last_call_text'] == 'A fax call from +13035550100 came in, but no fax data arrived from the carrier.'
    assert body['last_call_at'].endswith('Z')
    # T.38 carried nothing back, so the screen offers audio fax for new calls; it never switches by itself.
    assert body['last_call_verdict'] == 'no_t38_data_back' and body['suggest_audio'] is True
    calls = client.get('/admin/sip/calls', headers=ADMIN).json()['items']
    assert calls[0]['verdict'] == 'no_t38_data_back' and calls[0]['summary'] == body['last_call_text']


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


def test_apply_records_the_internet_address_and_status_says_when_asterisk_needs_a_restart(
        client, isolated_installation, network, monkeypatch):
    folder = os.path.join(isolated_installation['FAX_DATA_DIR'], 'asterisk')
    network['result'] = stun.Probe(public_ip='198.51.100.7', local_ip='172.18.0.5', local_port=40000,
                                   mapped=(('a', 40000), ('b', 40000)))
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert json.loads(open(os.path.join(folder, 'public-address')).read())['ports_preserved'] is True
    assert '@FAXBOT_PUBLIC_ADDRESS@' in open(os.path.join(folder, 'pjsip.conf')).read()
    # Asterisk started and advertised the address it was given.
    open(os.path.join(folder, 'public-address.applied'), 'w').write('198.51.100.7\n')
    sip_http._probes.clear()
    body = client.get('/admin/sip/status', headers=ADMIN).json()
    assert body['address_changed'] is False and body['advertised_address'] == '198.51.100.7'
    assert body['public_address_text'] == ("Faxbot's internet address is 198.51.100.7, and your network keeps "
                                           "port numbers, so Telnyx is told exactly where to send fax data.")
    # The router got a new address; Asterisk still advertises the old one until it restarts.
    async def registered(fields, *, collect=False):
        if fields['Action'] == 'PJSIPShowRegistrationsOutbound':
            return ({'response': 'Success', 'value': '', 'message': ''},
                    [{'ObjectName': 'trunk-registration', 'Status': 'Registered', 'Transport': 'transport-tls'}])
        return {'response': 'Success', 'value': 'NOT_INUSE', 'message': ''}, []
    monkeypatch.setattr(ami_client, 'status_query', registered)
    monkeypatch.setattr(ami_client._connected, 'is_set', lambda: True)
    network['result'] = stun.Probe(public_ip='198.51.100.9', local_ip='172.18.0.5', local_port=40000,
                                   mapped=(('a', 40000), ('b', 40000)))
    sip_http._probes.clear()
    body = client.get('/admin/sip/status', headers=ADMIN).json()
    assert body['address_changed'] is True
    assert body['message'] == ('Your internet address changed. Restart the Asterisk service so the carrier '
                               'gets the new address.')


@pytest.mark.asyncio
async def test_the_address_watcher_records_a_new_address_and_ignores_unanswered_probes(isolated_installation,
                                                                                    network, tmp_path):
    from app.config_values import ConfigurationValues
    settings = ConfigurationValues.from_environment({**TRUNK, 'FAX_DATA_DIR': str(tmp_path)})
    network['result'] = stun.Probe(public_ip='198.51.100.9', local_ip='172.18.0.5', local_port=40000,
                                   mapped=(('a', 40000), ('b', 40000)))
    task = asyncio.create_task(sip_http.watch_public_address(minutes=0.001, values_source=lambda: settings))
    try:
        for _ in range(100):
            await asyncio.sleep(0.02)
            if sip_trunk.read_public_address(settings):
                break
        assert sip_trunk.read_public_address(settings)['ip'] == '198.51.100.9'
        network['result'] = stun.Probe(public_ip=None, local_ip=None, local_port=40000, mapped=(('a', None),))
        await asyncio.sleep(0.2)
        assert sip_trunk.read_public_address(settings)['ip'] == '198.51.100.9'
    finally:
        task.cancel()
    assert await sip_http.watch_public_address(minutes=0) is None


# Apply and connect: when Asterisk shares Faxbot's data folder (the Compose
# install), Apply restarts it once no call is up, and status follows the restart.

class FakeEngine:
    """Answers Faxbot's manager actions the way Asterisk 22 does; records what was asked."""

    def __init__(self, calls_up=0, may_command=True):
        self.calls_up, self.may_command = calls_up, may_command
        self.actions = []
        self.folder = None

    async def status_query(self, fields, *, collect=False):
        self.actions.append(fields['Command'] if fields['Action'] == 'Command' else fields['Action'])
        if fields['Action'] == 'CoreShowChannels':
            return {'response': 'Success', 'value': '', 'message': 'Channels will follow'}, [
                {'Uniqueid': f'1700000000.{index}'} for index in range(self.calls_up)]
        if fields['Action'] == 'Command':
            if not self.may_command:
                return {'response': 'Error', 'value': '', 'message': 'Permission denied'}, []
            # Asterisk exits inside the command, so the reply never arrives.
            raise ConnectionError('AMI connection closed')
        if fields['Action'] == 'PJSIPShowRegistrationsOutbound':
            return ({'response': 'Success', 'value': '', 'message': ''},
                    [{'ObjectName': 'trunk-registration', 'Status': 'Registered', 'Transport': 'transport-tls'}])
        return {'response': 'Success', 'value': 'NOT_INUSE', 'message': ''}, []


@pytest.fixture
def engine(monkeypatch, isolated_installation):
    """An Asterisk that started from Faxbot's data folder and is connected."""
    folder = os.path.join(isolated_installation['FAX_DATA_DIR'], 'asterisk')
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, 'engine-started'), 'w') as marker:
        marker.write('1700000000\n')
    fake = FakeEngine()
    fake.folder = folder
    monkeypatch.setattr(ami_client, 'status_query', fake.status_query)
    monkeypatch.setattr(ami_client._connected, 'is_set', lambda: True)
    monkeypatch.setattr(ami_client, 'connected_at', 1.0)
    monkeypatch.setattr(sip_http, '_restart', {'at': None})
    return fake


def _asterisk_started_with_current_files(folder):
    """What asterisk/start.sh and faxbot-public-address record when Asterisk loads the trunk."""
    with open(os.path.join(folder, 'pjsip.conf')) as source, open(os.path.join(folder, 'pjsip.conf.started'), 'w') as copy:
        copy.write(source.read())
    record = json.loads(open(os.path.join(folder, 'public-address')).read())
    with open(os.path.join(folder, 'public-address.applied'), 'w') as applied:
        applied.write((record['ip'] + '\n') if record['ports_preserved'] else '')


def test_apply_and_connect_restarts_a_managed_asterisk_once_no_call_is_up(client, engine, monkeypatch):
    before = client.get('/admin/sip/status', headers=ADMIN).json()
    assert before['engine_managed'] is True
    assert before['message'] == 'Select Apply and connect so Asterisk uses these settings.'
    engine.actions.clear()
    applied = client.post('/admin/sip/apply', headers=ADMIN).json()
    assert applied == {'ok': True, 'engine': 'restarting', 'message': 'Asterisk is restarting to use the new settings.'}
    assert engine.actions == ['CoreShowChannels', 'core stop gracefully']
    # Until Faxbot logs in to the restarted Asterisk, status says it is restarting.
    during = client.get('/admin/sip/status', headers=ADMIN).json()
    assert during['engine_restarting'] is True
    assert during['message'] == 'Asterisk is restarting to use the new settings.'
    # Asterisk started again from the files Faxbot wrote, and Faxbot logged in again.
    _asterisk_started_with_current_files(engine.folder)
    monkeypatch.setattr(ami_client, 'connected_at', sip_http._restart['at'] + 1)
    after = client.get('/admin/sip/status', headers=ADMIN).json()
    assert (after['engine_restarting'], after['in_use'], after['registration']) == (False, True, 'registered')
    assert after['message'] == 'The trunk is ready.'
    assert 'Restart the Asterisk service' not in json.dumps(after)
    # Applying the same settings again restarts nothing.
    engine.actions.clear()
    again = client.post('/admin/sip/apply', headers=ADMIN).json()
    assert again == {'ok': True, 'engine': 'current', 'message': 'Saved. Asterisk already uses these settings.'}
    assert engine.actions == []


def test_apply_and_connect_waits_for_calls_in_progress(client, engine):
    engine.calls_up = 1
    applied = client.post('/admin/sip/apply', headers=ADMIN).json()
    assert applied['engine'] == 'busy'
    assert applied['message'] == ('Saved. A call is in progress, so Asterisk keeps its current settings until you '
                                  'apply again after it ends.')
    assert engine.actions == ['CoreShowChannels']
    status = client.get('/admin/sip/status', headers=ADMIN).json()
    assert status['message'] == 'Asterisk still uses earlier trunk settings; select Apply and connect to load these.'


def test_apply_and_connect_says_what_to_do_once_when_asterisk_refuses_the_restart(client, engine):
    engine.may_command = False
    applied = client.post('/admin/sip/apply', headers=ADMIN).json()
    assert applied['engine'] == 'not_allowed'
    assert applied['message'] == ('Saved. Asterisk does not let Faxbot restart it yet; restart the Asterisk service '
                                  'once, and Apply and connect restarts it from then on.')


def test_apply_and_connect_without_a_manager_connection_keeps_the_files_for_the_next_start(client, engine,
                                                                                          monkeypatch):
    monkeypatch.setattr(ami_client._connected, 'is_set', lambda: False)
    monkeypatch.setattr(ami_client, '_connection_task', object())
    monkeypatch.setattr(ami_client, 'problem', 'unreachable')
    applied = client.post('/admin/sip/apply', headers=ADMIN).json()
    assert applied['engine'] == 'not_connected'
    assert applied['message'] == "Saved. Faxbot can't reach its fax engine. Check that the Asterisk service is running."
    assert engine.actions == []
    assert os.path.exists(os.path.join(engine.folder, 'pjsip.conf'))


def test_an_asterisk_this_install_does_not_manage_keeps_the_manual_sentence(client, monkeypatch):
    actions = []

    async def status_query(fields, *, collect=False):
        actions.append(fields['Action'])
        return {'response': 'Success', 'value': '', 'message': ''}, []
    monkeypatch.setattr(ami_client, 'status_query', status_query)
    monkeypatch.setattr(ami_client._connected, 'is_set', lambda: True)
    applied = client.post('/admin/sip/apply', headers=ADMIN).json()
    assert applied == {'ok': True, 'engine': 'manual',
                       'message': 'Saved for Asterisk. Restart the Asterisk service to use these settings.'}
    assert 'CoreShowChannels' not in actions


def test_status_says_whether_received_faxes_reach_faxbot(isolated_installation, monkeypatch):
    with _client(monkeypatch, {**TRUNK, 'FAX_BACKEND': 'sip', 'INBOUND_ENABLED': 'true'}) as client:
        # Faxbot created the inbound secret at start and wrote it where Asterisk reads it; nobody typed it.
        body = client.get('/admin/sip/status', headers=ADMIN).json()
        assert (body['handover_ready'], body['handover_text']) == (True, 'Received faxes reach Faxbot: ready.')
        secret = os.path.join(isolated_installation['FAX_DATA_DIR'], 'asterisk', 'inbound.secret')
        os.remove(secret)
        body = client.get('/admin/sip/status', headers=ADMIN).json()
        assert body['handover_ready'] is False
        assert body['handover_text'] == ('Received faxes cannot reach Faxbot yet; apply these settings to Asterisk, '
                                         'then restart the Asterisk service.')
        assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
        assert client.get('/admin/sip/status', headers=ADMIN).json()['handover_ready'] is True
        assert open(secret).read() not in client.get('/admin/sip/status', headers=ADMIN).text
        # A trunk that only sends says nothing about received faxes.
        current = client.get('/admin/settings', headers=ADMIN).json()
        assert client.put('/admin/settings', headers=ADMIN, json={
            'expected_revision_id': current['_meta']['desired_revision_id'], 'inbound_enabled': False}).status_code == 200
        assert client.get('/admin/sip/status', headers=ADMIN).json()['handover_text'] is None


def test_readiness_waits_for_a_trunk_on_each_direction_that_uses_it(isolated_installation, monkeypatch):
    monkeypatch.setattr(ami_client._connected, 'is_set', lambda: True)
    with _client(monkeypatch, {'FAX_BACKEND': 'sip', 'INBOUND_ENABLED': 'true'}) as client:
        ready = client.get('/health/ready')
        assert ready.status_code == 503
        assert ready.json()['message'] == 'No SIP trunk is set up. Choose your carrier to start.'
        health = client.get('/admin/health-status', headers=ADMIN).json()
        assert health['backend_healthy'] is False
        assert health['backend_message'] == 'No SIP trunk is set up. Choose your carrier to start.'
        current = client.get('/admin/settings', headers=ADMIN).json()
        saved = client.put('/admin/settings', headers=ADMIN, json={
            'expected_revision_id': current['_meta']['desired_revision_id'], 'sip_trunk_preset': 'telnyx',
            'sip_trunk_username': 'faxbotuser'})
        assert saved.status_code == 200, saved.text
        assert client.get('/health/ready').json()['message'] == 'Some trunk settings are missing.'
        current = client.get('/admin/settings', headers=ADMIN).json()
        client.put('/admin/settings', headers=ADMIN, json={
            'expected_revision_id': current['_meta']['desired_revision_id'], 'sip_trunk_password': PASSWORD,
            'sip_trunk_caller_id': '+15555550100'})
        assert 'message' not in client.get('/health/ready').json()


def test_a_refused_registration_names_the_password_the_carrier_wants(client, monkeypatch):
    async def rejected(fields, *, collect=False):
        if fields['Action'] == 'PJSIPShowRegistrationsOutbound':
            return ({'response': 'Success', 'value': '', 'message': ''},
                    [{'ObjectName': 'trunk-registration', 'Status': 'Rejected', 'Transport': 'transport-tls'}])
        return {'response': 'Success', 'value': 'UNAVAILABLE', 'message': ''}, []

    client.post('/admin/sip/apply', headers=ADMIN)
    monkeypatch.setattr(ami_client, 'status_query', rejected)
    monkeypatch.setattr(ami_client._connected, 'is_set', lambda: True)
    body = client.get('/admin/sip/status', headers=ADMIN).json()
    telnyx = ("Telnyx refused the username or password. Use the SIP connection's password, "
              'not your Telnyx account password.')
    assert body['message'] == telnyx and body['registration_text'] == telnyx
    # Another carrier gets the same advice in its own words.
    current = client.get('/admin/settings', headers=ADMIN).json()
    assert client.put('/admin/settings', headers=ADMIN, json={
        'expected_revision_id': current['_meta']['desired_revision_id'], 'sip_trunk_preset': 'custom',
        'sip_trunk_host': 'sip.example.net'}).status_code == 200
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert client.get('/admin/sip/status', headers=ADMIN).json()['message'] == (
        'The carrier refused the username or password. Use the SIP credentials the carrier gave for this trunk, '
        'not your account login.')


def test_the_telnyx_preset_names_the_connection_password_and_no_inbound_transport():
    telnyx = next(preset for preset in sip_trunk.preset_catalog() if preset['id'] == 'telnyx')
    text = ' '.join(telnyx['notes']) + ' ' + telnyx['t38']
    assert "SIP connection's password (connection → Authentication and routing), not your Telnyx account password" in text
    assert 'inbound SIP transport' not in text and 'set the connection' not in text
    assert 'Enable T.38 Fax Gateway' in telnyx['t38'] and 'T.38 fax re-invite initiated by' in telnyx['t38']
