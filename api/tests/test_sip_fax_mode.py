"""Faxbot chooses audio fax for new trunk calls when T.38 cannot work here, and says why.

It never resends the failed fax; it changes only what new calls use, records
the reason, and leaves a person's own choice alone.
"""
import asyncio

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main, sip_fax_mode, sip_http, stun
from app.ami import ami_client


BOOTSTRAP = 'synthetic-mode-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
TRUNK = {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser', 'SIP_TRUNK_PASSWORD': 'synthetic-Pass!42',
         'SIP_TRUNK_CALLER_ID': '+15555550100'}
CHANGES_PORTS = stun.Probe(public_ip='198.51.100.7', local_ip='172.18.0.5', local_port=40000,
                           mapped=(('stun.telnyx.com:3478', 61001), ('stun.cloudflare.com:3478', 61002)))
KEEPS_PORTS = stun.Probe(public_ip='198.51.100.7', local_ip='172.18.0.5', local_port=40000,
                         mapped=(('a', 40000), ('b', 40000)))
# What Asterisk reports for a sent fax whose call switched to T.38 and got no fax data back.
NO_T38_DATA = {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': 'a' * 32, 'AttemptID': 'b' * 32,
               'Status': 'FAILED', 'Mode': 'T38', 'Pages': '0', 'Answered': '1791075343', 'Ended': '1791075381',
               'Error': 'Timed out waiting for the first message'}


@pytest.fixture
def network(monkeypatch):
    seen = {'result': CHANGES_PORTS}
    monkeypatch.setattr(stun, 'probe', lambda servers, **_: seen['result'])
    monkeypatch.setattr(sip_http, '_probes', {})
    return seen


@pytest.fixture
def client(isolated_installation, monkeypatch, network):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        **TRUNK}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def _t38(client):
    return client.get('/admin/settings', headers=ADMIN).json()['sip']['trunk']['t38_enabled']


def _actors(client):
    store = client.app.state.configuration_runtime.manager.store
    with store.engine.connect() as connection:
        return connection.execute(sa.select(store.revisions.c.actor)).scalars().all()


def _set_t38(client, enabled):
    current = client.get('/admin/settings', headers=ADMIN).json()
    response = client.put('/admin/settings', headers=ADMIN, json={
        'expected_revision_id': current['_meta']['desired_revision_id'], 'sip_t38_enabled': enabled})
    assert response.status_code == 200, response.text


def test_a_new_telnyx_trunk_on_a_network_that_changes_ports_starts_with_audio_fax(client):
    assert _t38(client) is True
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert _t38(client) is False
    assert 'system' in _actors(client)
    status = client.get('/admin/sip/status', headers=ADMIN).json()
    assert status['t38_off_reason'] == 'network' and status['t38_off_at'].endswith('Z')
    assert 't38_udptl=no' in open(sip_fax_mode.record_path(
        client.app.state.configuration_runtime.manager.store.read().active.values).with_name('pjsip.conf')).read()
    # Try T.38 again: the person's choice stands, and Apply does not switch it back.
    _set_t38(client, True)
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert _t38(client) is True
    assert client.get('/admin/sip/status', headers=ADMIN).json()['t38_off_reason'] is None


def test_a_network_that_keeps_ports_keeps_t38(client, network):
    network['result'] = KEEPS_PORTS
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert _t38(client) is True


def test_a_person_turning_t38_off_gives_no_reason_to_show(client, network):
    network['result'] = KEEPS_PORTS
    _set_t38(client, False)
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert client.get('/admin/sip/status', headers=ADMIN).json()['t38_off_reason'] is None


def test_a_t38_call_with_no_fax_data_back_switches_new_calls_to_audio_without_resending(client, network):
    network['result'] = KEEPS_PORTS
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    runtime = client.app.state.configuration_runtime

    class Listener:
        def __init__(self):
            self.callbacks = []

        def on_fax_result(self, callback):
            self.callbacks.append(callback)

        def on_inbound_call(self, callback):
            self.callbacks.append(callback)

    listener = Listener()
    sip_fax_mode.attach(listener, runtime)
    sent = []
    original = ami_client.originate_sendfax

    async def no_resend(*args, **kwargs):
        sent.append(args)
    ami_client.originate_sendfax = no_resend
    try:
        async def deliver():
            listener.callbacks[0]({**NO_T38_DATA, 'Status': 'SUCCESS', 'Pages': '2'})
            listener.callbacks[0](NO_T38_DATA)
            await asyncio.gather(*list(sip_fax_mode._pending))
        asyncio.run(deliver())
    finally:
        ami_client.originate_sendfax = original
        sip_fax_mode.detach()
    assert _t38(client) is False
    status = client.get('/admin/sip/status', headers=ADMIN).json()
    assert status['t38_off_reason'] == 'no_data_back'
    assert sent == []
    # Once audio fax is in use, a second such call changes nothing.
    assert asyncio.run(sip_fax_mode.switch_to_audio(runtime, sip_fax_mode.NO_DATA_BACK)) is None
