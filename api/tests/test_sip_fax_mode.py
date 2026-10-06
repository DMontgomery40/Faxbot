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
            # A person answered and hung up: the verdict is the same, but it never switches the installation.
            listener.callbacks[0]({**NO_T38_DATA, 'Error': 'The call dropped prematurely'})
            assert not sip_fax_mode._pending
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


NO_DATA_INBOUND = {'UniqueID': '1791075343.12', 'Caller': '+13035550100', 'DID': '+15555550100', 'Status': 'FAILED',
                   'Error': 'Timed out waiting for initial communication', 'Cause': '16', 'Pages': '0', 'Mode': 'T38',
                   'Answered': '1791075343',
                   'Ended': '1791075357'}


def test_t38_turned_off_after_a_no_data_call_before_any_record_shows_that_call_as_the_reason(client, network):
    from app import sip_calls
    network['result'] = KEEPS_PORTS
    runtime = client.app.state.configuration_runtime
    records = sip_calls.SipCallRecords(runtime.manager.store.engine)
    records.record_inbound_event(NO_DATA_INBOUND)
    # Exactly what the acceptance install stored for its 21:00 call: the reason is cut short.
    assert records.latest()['error_cause'] == 'no_t38_data_back: Timed out waiting for initial commu (cause 16)'
    # A person turned T.38 off right after that call, before Faxbot kept its own record.
    _set_t38(client, False)
    status = client.get('/admin/sip/status', headers=ADMIN).json()
    assert status['t38_off_reason'] == 'no_data_back'
    assert status['t38_off_at'] == '2026-10-04T00:55:57Z'
    values = runtime.manager.store.read().active.values
    assert sip_fax_mode.read(values) == {'mode': 'audio', 'reason': 'no_data_back', 'at': '2026-10-04T00:55:57Z',
                                         'derived': True}
    # A later Apply keeps that reason instead of recording a person's choice.
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert client.get('/admin/settings', headers=ADMIN).json()['sip']['trunk']['t38_off_reason'] == 'no_data_back'


def test_a_t38_fax_that_went_through_since_leaves_no_derived_reason(client, network):
    from app import sip_calls
    network['result'] = KEEPS_PORTS
    runtime = client.app.state.configuration_runtime
    records = sip_calls.SipCallRecords(runtime.manager.store.engine)
    records.record_inbound_event(NO_DATA_INBOUND)
    records.record_inbound({'started_at': '1791079000', 'answered_at': '1791079001', 'ended_at': '1791079060',
                            'did': '+15555550100', 'caller': '+13035550100', 't38': True, 'pages': 2},
                           call_id='1791079000.40', inbound_fax_id='c' * 32, fax_status='SUCCESS')
    _set_t38(client, False)
    assert client.get('/admin/sip/status', headers=ADMIN).json()['t38_off_reason'] is None
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    values = runtime.manager.store.read().active.values
    assert sip_fax_mode.read(values)['reason'] == 'chosen'


class _Calls:
    def __init__(self, *items):
        self.items = list(items)

    def page(self, **_):
        return {'items': self.items, 'next_cursor': None}


@pytest.mark.parametrize('calls, audio, sentence', [
    # Tonight's install: T.38 got no data back, then an audio fax went through.
    ((({'t38': 'no', 'verdict': 'sent'}), {'t38': 'yes', 'verdict': 'no_t38_data_back', 'error_cause': 'no_t38_data_back: Timed out waiting for the first messa (cause 16)'}), True,
     "Faxbot's internet address is 198.51.100.7; your network changes port numbers, and Telnyx follows Faxbot's "
     "audio packets (a fax went through) but not its T.38 packets, so Faxbot uses audio fax."),
    (({'t38': 'yes', 'verdict': 'no_t38_data_back', 'error_cause': 'no_t38_data_back: Timed out waiting for the first messa (cause 16)'},), False,
     "Faxbot's internet address is 198.51.100.7; your network changes port numbers, and the last T.38 fax got no fax "
     "data back, so Telnyx does not follow Faxbot's T.38 packets on this network."),
    (({'t38': 'yes', 'verdict': 'received'}, {'t38': 'yes', 'verdict': 'no_t38_data_back', 'error_cause': 'no_t38_data_back: Timed out waiting for the first messa (cause 16)'}), False,
     "Faxbot's internet address is 198.51.100.7; your network changes port numbers, so Telnyx has to follow Faxbot's "
     "packets, and a T.38 fax that went through shows it does."),
    (({'t38': 'no', 'verdict': 'received'},), False,
     "Faxbot's internet address is 198.51.100.7; your network changes port numbers, so Telnyx has to follow Faxbot's "
     "packets, and a fax that went through shows it does."),
    ((), False,
     "Faxbot's internet address is 198.51.100.7; your network changes port numbers, so Telnyx has to follow Faxbot's "
     "packets, and the first test fax shows whether it does."),
    # A plain hang-up says nothing about T.38 on this network.
    (({'t38': 'yes', 'verdict': 'no_t38_data_back', 'error_cause': 'no_t38_data_back: The call dropped prematurely'},), False,
     "Faxbot's internet address is 198.51.100.7; your network changes port numbers, so Telnyx has to follow Faxbot's "
     "packets, and the first test fax shows whether it does."),
])
def test_the_internet_address_sentence_says_what_calls_have_shown(calls, audio, sentence):
    observed = sip_http._observed(_Calls(*calls))
    assert sip_http._address_text({}, CHANGES_PORTS, 'Telnyx', observed, audio) == sentence


def test_the_switch_waits_for_the_failed_call_to_hang_up_then_restarts_asterisk_once(client, network, monkeypatch):
    """The result arrives from the call's hangup handler while its channel still exists."""
    import os
    network['result'] = KEEPS_PORTS
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    runtime = client.app.state.configuration_runtime
    values = runtime.manager.store.read().active.values
    open(os.path.join(values.fax_data_dir, 'asterisk', 'engine-started'), 'w').write('1700000000\n')
    channels = [1, 1, 0]
    actions = []

    async def status_query(fields, *, collect=False):
        actions.append(fields.get('Command') or fields['Action'])
        if fields['Action'] == 'CoreShowChannels':
            up = channels.pop(0) if channels else 0
            return {'response': 'Success', 'value': '', 'message': ''}, [{'Uniqueid': '1.1'}] * up
        raise ConnectionError('AMI connection closed')  # Asterisk exits inside "core stop gracefully"
    monkeypatch.setattr(ami_client, 'status_query', status_query)
    monkeypatch.setattr(ami_client._connected, 'is_set', lambda: True)
    monkeypatch.setattr(sip_http, '_restart', {'at': None})
    monkeypatch.setattr(sip_fax_mode, 'BUSY_RETRY_SECONDS', 0.01)
    result = asyncio.run(sip_fax_mode.switch_to_audio(runtime, sip_fax_mode.NO_DATA_BACK))
    assert result['engine'] == 'restarting'
    assert actions == ['CoreShowChannels', 'CoreShowChannels', 'CoreShowChannels', 'core stop gracefully']
    assert _t38(client) is False


def test_a_switch_that_finds_calls_up_reloads_asterisk_once_they_end_and_says_so(client, network, monkeypatch):
    """Review round 4: after 120 s of busy lines the setting was saved but Asterisk never loaded it."""
    import os
    network['result'] = KEEPS_PORTS
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    runtime = client.app.state.configuration_runtime
    values = runtime.manager.store.read().active.values
    open(os.path.join(values.fax_data_dir, 'asterisk', 'engine-started'), 'w').write('1700000000\n')
    busy = {'up': True}
    actions = []

    async def status_query(fields, *, collect=False):
        actions.append(fields.get('Command') or fields['Action'])
        if fields['Action'] == 'CoreShowChannels':
            return {'response': 'Success', 'value': '', 'message': ''}, [{'Uniqueid': '1.1'}] if busy['up'] else []
        raise ConnectionError('AMI connection closed')  # Asterisk exits inside "core stop gracefully"
    monkeypatch.setattr(ami_client, 'status_query', status_query)
    monkeypatch.setattr(ami_client._connected, 'is_set', lambda: True)
    monkeypatch.setattr(sip_http, '_restart', {'at': None})
    monkeypatch.setattr(sip_fax_mode, 'BUSY_RETRY_SECONDS', 0.01)
    monkeypatch.setattr(sip_fax_mode, 'BUSY_WAIT_SECONDS', 0.05)

    async def scenario():
        result = await sip_fax_mode.switch_to_audio(runtime, sip_fax_mode.NO_DATA_BACK)
        assert (result['engine'], result['waiting'], result['message']) == (
            'busy', True, 'Saved. Asterisk loads the new settings as soon as no call is up.')
        assert sip_fax_mode.reload_waiting() and 'core stop gracefully' not in actions
        busy['up'] = False  # the call ends
        for _ in range(200):
            if not sip_fax_mode.reload_waiting():
                break
            await asyncio.sleep(0.01)
        assert not sip_fax_mode.reload_waiting()
    asyncio.run(scenario())
    assert actions.count('core stop gracefully') == 1 and _t38(client) is False


def test_the_status_says_asterisk_loads_a_switched_setting_once_calls_end(client, network, monkeypatch):
    import os
    network['result'] = KEEPS_PORTS
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    values = client.app.state.configuration_runtime.manager.store.read().active.values
    open(os.path.join(values.fax_data_dir, 'asterisk', 'engine-started'), 'w').write('1700000000\n')
    monkeypatch.setattr(sip_fax_mode, 'reload_waiting', lambda: True)
    status = client.get('/admin/sip/status', headers=ADMIN).json()
    assert status['reload_waiting'] is True
    assert status['message'] == 'Saved. Asterisk loads the new settings as soon as no call is up.'


def test_a_t38_call_that_was_simply_hung_up_gives_no_derived_reason(client, network):
    from app import sip_calls
    network['result'] = KEEPS_PORTS
    runtime = client.app.state.configuration_runtime
    sip_calls.SipCallRecords(runtime.manager.store.engine).record_inbound_event(
        {**NO_DATA_INBOUND, 'Error': 'The call dropped prematurely'})
    _set_t38(client, False)
    assert client.get('/admin/sip/status', headers=ADMIN).json()['t38_off_reason'] is None
