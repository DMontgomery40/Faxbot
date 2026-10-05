"""The network check for fax over IP: where Faxbot runs, whether T.38 data can come back, what to do.

The fixtures are the measurements of 2026-10-03/04 (Mac host, Colima's three
networks, all from a container), with every internet address replaced by a
documentation address. No test reaches the internet or a router.
"""
import asyncio
import socket
import struct
import sys

import pytest
import yaml
from fastapi.testclient import TestClient

from app import main, sip_fax_mode, sip_http, sip_network, sip_trunk, stun
from app.config_values import ConfigurationValues
from app.sip_network import BLOCKED, OPEN, UNKNOWN, Discovery

BOOTSTRAP = 'synthetic-network-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
TRUNK = {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser', 'SIP_TRUNK_PASSWORD': 'synthetic-Pass!42',
         'SIP_TRUNK_CALLER_ID': '+15555550100'}
APPLE = {'kernel': '6.8.0-64-generic', 'vendor': 'Apple Inc.', 'product': 'Apple Virtualization Generic Platform',
         'in_container': True, 'cpus': 2, 'memory_gib': 4, 'disk_gib': 20}


def probe(local, *ports, public='198.51.100.7'):
    return stun.Probe(public_ip=public, local_ip='172.18.0.5', local_port=local,
                      mapped=tuple(zip(('stun.telnyx.com:3478', 'stun.cloudflare.com:3478', 'c'), ports)))


# Rows of the measurement table (brief 17), as a container sees them.
COLIMA_USER = (Discovery(lima=True, hops=('172.17.0.1', None, None, None, None, None), **APPLE),
               probe(4123, 57386, 61984))
COLIMA_SHARED = (Discovery(lima=True, hops=('172.17.0.1', '192.168.64.1', '192.168.68.1', '203.0.113.2', None, None),
                           **APPLE), probe(4001, 48497, 48497))
COLIMA_BRIDGED = (Discovery(lima=True, hops=('172.17.0.1', '192.168.68.1', '203.0.113.2', '203.0.113.30', None, None),
                            **APPLE), probe(4002, 4002, 4002))
MAC_HOST = (Discovery(), probe(40123, 40123, 40123))
DESKTOP_MAC = (Discovery(kernel='6.10.14-linuxkit', vendor='Apple Inc.', in_container=True,
                         hops=('172.17.0.1', None, None)), probe(4000, 52001, 52002))
DESKTOP_WINDOWS = (Discovery(kernel='5.15.167.4-microsoft-standard-WSL2', in_container=True), probe(4000, 52001, 52001))
LINUX_LAN = (Discovery(kernel='6.1.0-25-amd64', vendor='Dell Inc.', in_container=True,
                       hops=('172.18.0.1', '192.168.1.1', '203.0.113.9', None)), probe(4000, 4000, 4000))
LINUX_LAN_CHANGES = (LINUX_LAN[0], probe(4000, 61001, 61001))
CLOUD = (Discovery(kernel='6.8.0-1015-aws', vendor='Amazon EC2', in_container=True, cloud_metadata=True,
                   hops=('172.18.0.1', None, None)), probe(4000, 4000, 4000))
CLOUD_NAT = (CLOUD[0], probe(4000, 1024, 1024))
PUBLIC_HOST = (Discovery(kernel='6.1.0-25-amd64', in_container=True, hops=('172.18.0.1', '203.0.113.1', None)),
               probe(4000, 4000, 4000))
SHARED_ISP = (Discovery(kernel='6.1.0-25-amd64', in_container=True,
                        hops=('172.18.0.1', '192.168.1.1', '100.72.0.1', '203.0.113.5')), probe(4000, 61001, 61002))
NO_STUN = (LINUX_LAN[0], stun.Probe(public_ip=None, local_ip=None, local_port=4000, mapped=(('a', None), ('b', None))))


def values(**settings):
    return ConfigurationValues.from_environment({**TRUNK, 'FAX_DATA_DIR': '/nonexistent', **settings})


@pytest.mark.parametrize('row, where, ports, t38, why', [
    (MAC_HOST, 'unknown', 'kept', OPEN, 'ports_kept'),
    (COLIMA_USER, 'colima_user', 'changed_per_destination', BLOCKED, 'ports_change'),
    (COLIMA_SHARED, 'colima_shared', 'changed_same', BLOCKED, 'ports_change'),
    (COLIMA_BRIDGED, 'colima_bridged', 'kept', OPEN, 'ports_kept'),
    (DESKTOP_MAC, 'docker_desktop_mac', 'changed_per_destination', BLOCKED, 'ports_change'),
    (DESKTOP_WINDOWS, 'docker_desktop_windows', 'changed_same', BLOCKED, 'ports_change'),
    (LINUX_LAN, 'linux_lan', 'kept', OPEN, 'ports_kept'),
    (LINUX_LAN_CHANGES, 'linux_lan', 'changed_same', BLOCKED, 'ports_change'),
    (CLOUD, 'cloud', 'kept', OPEN, 'ports_kept'),
    (CLOUD_NAT, 'cloud', 'changed_same', BLOCKED, 'ports_change'),
    (PUBLIC_HOST, 'public_host', 'kept', OPEN, 'ports_kept'),
    (SHARED_ISP, 'linux_lan', 'changed_per_destination', BLOCKED, 'shared_address'),
    (NO_STUN, 'linux_lan', None, UNKNOWN, 'no_address'),
])
def test_each_measured_network_is_classified(row, where, ports, t38, why):
    found, result = row
    check = sip_network.assess(values(), result, found, now=lambda: 1791075343.0)
    assert (check['platform'], check['ports'], check['t38'], check['why']) == (where, ports, t38, why)
    assert check['checked_at'] == 1791075343.0


def test_colima_without_traceroute_is_told_apart_by_its_ports():
    bare = Discovery(lima=True, **APPLE)
    assert sip_network.platform(bare, COLIMA_USER[1]) == 'colima_user'
    assert sip_network.platform(bare, COLIMA_SHARED[1]) == 'colima_shared'
    assert sip_network.platform(bare, COLIMA_BRIDGED[1]) == 'colima_bridged'
    assert sip_network.platform(bare, None) == 'colima'


def test_a_server_whose_own_address_stun_sees_has_a_public_address():
    own = stun.Probe(public_ip='203.0.113.7', local_ip='203.0.113.7', local_port=4000, mapped=(('a', 4000),))
    check = sip_network.assess(values(), own, Discovery(kernel='6.1.0-25-amd64'))
    assert (check['platform'], check['t38'], check['why']) == ('public_host', OPEN, 'public')


def test_the_internet_providers_shared_address_space_is_seen_on_the_way_out():
    assert sip_network.shared_address(*SHARED_ISP) is True
    assert sip_network.shared_address(*LINUX_LAN) is False
    # Through the provider's shared address, ports kept end to end still bring T.38 data back.
    kept = sip_network.assess(values(), probe(4000, 4000, 4000), SHARED_ISP[0])
    assert (kept['t38'], kept['shared_address']) == (OPEN, True)


@pytest.mark.parametrize('typed, result, observed, t38, why', [
    ('198.51.100.7', probe(4000, 4000, 4000), None, OPEN, 'typed'),
    ('198.51.100.99', probe(4000, 4000, 4000), None, UNKNOWN, 'typed_differs'),
    ('198.51.100.7', probe(4000, 61001, 61001), None, UNKNOWN, 'typed'),
    ('198.51.100.7', probe(4000, 61001, 61001), {'t38_ok': True}, OPEN, 't38_worked'),
    ('', probe(4000, 61001, 61002), {'t38_ok': True, 't38_failed': False, 'audio_ok': False}, OPEN, 't38_worked'),
    ('', probe(4000, 61001, 61002), {'t38_ok': False, 't38_failed': True, 'audio_ok': True}, BLOCKED, 'ports_change'),
])
def test_a_typed_address_or_a_fax_that_went_through_changes_the_verdict(typed, result, observed, t38, why):
    check = sip_network.assess(values(SIP_EXTERNAL_ADDRESS=typed), result, LINUX_LAN[0], observed)
    assert (check['t38'], check['why']) == (t38, why)


def test_sentences_say_what_the_check_found_and_what_to_do():
    found, result = COLIMA_USER
    check = sip_network.assess(values(), result, found)
    assert sip_network.verdict_text(check, 'Telnyx') == (
        'Your network changes port numbers, and Telnyx does not follow such changes for T.38 fax data, so it cannot '
        'come back to Faxbot.')
    assert sip_network.verdict_text(check, 'SignalWire') == (
        "Your network changes port numbers, so SignalWire's T.38 fax data most likely cannot come back to Faxbot.")
    remedy = sip_network.fix(check)
    assert remedy['text'].startswith('Move Colima onto your office network')
    # Recreate keeps the machine's size; delete never takes --data (that would erase Faxbot's faxes).
    assert remedy['steps'] == [
        'colima list', 'colima delete default',
        'colima start default --cpu 2 --memory 4 --disk 20 --network-address --network-mode bridged '
        '--network-interface "$(route -n get default | awk \'/interface:/{print $2}\')" --network-preferred-route',
        'docker compose up -d']
    assert not any('--data' in step or ' -f' in step or ' -d' in step.split('compose up')[0] for step in remedy['steps'])
    assert 'Never add --data to the delete command' in remedy['note'] and 'Colima 0.9 or later' in remedy['note']
    bridged = sip_network.assess(values(), *reversed(COLIMA_BRIDGED))
    assert sip_network.fix(bridged) is None
    assert sip_network.verdict_text(bridged, 'Telnyx') == (
        "Your network keeps port numbers, so Telnyx's T.38 fax data can come back to Faxbot.")


@pytest.mark.parametrize('row, start', [
    (DESKTOP_MAC, 'Docker Desktop changes port numbers: forward UDP ports 4000–4039 on your router to this computer'),
    (LINUX_LAN_CHANGES, 'Your router changes port numbers: forward UDP ports 4000–4039 on your router to the computer'),
    (CLOUD_NAT, 'Give the server its own public address, open UDP ports 4000–4039 in its firewall or security group'),
    (SHARED_ISP, 'No router setting can change this.'),
    (NO_STUN, 'If a firewall limits outgoing traffic'),
])
def test_each_platform_gets_its_own_fix_and_audio_fax_keeps_working(row, start, tmp_path):
    found, result = row
    check = sip_network.assess(values(), result, found)
    remedy = sip_network.fix(check)
    assert remedy['text'].startswith(start)
    if remedy['steps']:
        assert remedy['steps'] == [sip_network.FAX_PORTS_COMMAND]
        assert 'enter your internet address, 198.51.100.7, under Internet address' in remedy['text'] \
            or row is CLOUD_NAT
    settings = values(FAX_DATA_DIR=str(tmp_path))
    sip_network.write_check(settings, check)
    assert sip_network.report(settings)['audio_text'] == 'Audio fax keeps working meanwhile.'


def test_the_fax_ports_file_publishes_exactly_the_range_the_sentences_name():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    asterisk = yaml.safe_load((root / sip_network.FAX_PORTS_FILE).read_text())['services']['asterisk']
    first, last = sip_network.FAX_PORTS
    assert asterisk['ports'] == [f'{first}-{last}:{first}-{last}/udp']
    assert asterisk['environment'] == [f'FAXBOT_MEDIA_PORTS={first}-{last}']
    assert sip_trunk.faxes_at_once(first, last) == 13


# -- traceroute through the socket error queue ----------------------------------------------------------

def _ancillary(address, icmp_type=11):
    error = struct.pack('=IBBBBII', 113, 2, icmp_type, 0, 0, 0, 0)
    offender = struct.pack('!HH4s8x', socket.AF_INET, 0, socket.inet_aton(address))
    return [(socket.IPPROTO_IP, sip_network.IP_RECVERR, error + offender)]


def test_a_router_report_names_the_router():
    assert sip_network.parse_hop(_ancillary('192.168.64.1')) == ('192.168.64.1', 11)
    assert sip_network.parse_hop(_ancillary('203.0.113.9', icmp_type=3)) == ('203.0.113.9', 3)
    assert sip_network.parse_hop([(socket.IPPROTO_IP, sip_network.IP_RECVERR, b'\0' * 8)]) is None
    assert sip_network.parse_hop(_ancillary('192.168.64.1', icmp_type=5)) is None


def test_traceroute_is_skipped_where_the_error_queue_does_not_exist(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    assert sip_network.trace() == ()


def test_the_live_look_is_skipped_in_tests():
    assert sip_network.discover_live() == Discovery()


# -- the one question the fax engine asks ------------------------------------------------------------------

def test_network_allows_t38_reads_only_the_stored_check(tmp_path):
    settings = values(FAX_DATA_DIR=str(tmp_path))
    assert sip_network.network_allows_t38(settings) is None
    for row, expected in ((COLIMA_USER, False), (COLIMA_BRIDGED, True), (NO_STUN, None)):
        sip_network.write_check(settings, sip_network.assess(settings, row[1], row[0]))
        assert sip_network.network_allows_t38(settings) is expected
    assert sip_network.network_allows_t38(values(SIP_TRUNK_PRESET='avaya-ipoffice', FAX_DATA_DIR=str(tmp_path))) is True
    assert sip_network.network_allows_t38(ConfigurationValues.from_environment({'FAX_DATA_DIR': str(tmp_path)})) is None


# -- T.38 turns itself off and on with the network --------------------------------------------------------

@pytest.fixture
def network(monkeypatch):
    seen = {'row': COLIMA_USER}
    monkeypatch.setattr(stun, 'probe', lambda servers, **_: seen['row'][1])
    monkeypatch.setattr(sip_http, '_probes', {})

    async def discover(*, fresh=False):
        return seen['row'][0]
    monkeypatch.setattr(sip_network, 'discover', discover)
    return seen


def _client(monkeypatch, extra):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'MAX_REQUESTS_PER_MINUTE': '0', **extra}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    return TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'})


@pytest.fixture
def client(isolated_installation, monkeypatch, network):
    with _client(monkeypatch, TRUNK) as client:
        yield client


def _t38(client):
    return client.get('/admin/settings', headers=ADMIN).json()['sip']['trunk']['t38_enabled']


def _set_t38(client, enabled):
    current = client.get('/admin/settings', headers=ADMIN).json()
    response = client.put('/admin/settings', headers=ADMIN, json={
        'expected_revision_id': current['_meta']['desired_revision_id'], 'sip_t38_enabled': enabled})
    assert response.status_code == 200, response.text


def _check(client):
    response = client.post('/admin/sip/network/check', headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def _values(client):
    return client.app.state.configuration_runtime.manager.store.read().active.values


def test_fixing_the_network_brings_t38_back_without_touching_the_switch(client, network):
    assert client.get('/admin/sip/network', headers=ADMIN).json()['text'] == 'Faxbot has not checked this network yet.'
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert _t38(client) is False
    report = client.get('/admin/sip/network', headers=ADMIN).json()
    assert (report['t38'], report['platform'], report['action']) == (BLOCKED, 'colima_user', 'turned_off')
    assert report['platform_text'] == "Faxbot runs in Colima on a Mac, on Colima's built-in network."
    assert report['checked_at'].endswith('Z') and report['fix_steps'][0] == 'colima list'
    assert sip_fax_mode.read(_values(client))['network'] == BLOCKED
    # The owner recreates Colima on the local network; Check again turns T.38 back on by itself.
    network['row'] = COLIMA_BRIDGED
    body = _check(client)
    assert (body['t38'], body['switched'], body['action'], body['fix_text']) == (OPEN, 't38', 'turned_on', None)
    assert _t38(client) is True
    assert sip_fax_mode.read(_values(client)) | {'at': None} == {'mode': 't38', 'reason': 'network', 'at': None,
                                                                  'network': OPEN}
    assert 't38_udptl=yes' in sip_trunk.configuration_path(_values(client)).read_text()
    assert sip_network.network_allows_t38(_values(client)) is True
    # The network breaks again: off again, with the reason.
    network['row'] = COLIMA_SHARED
    assert _check(client)['switched'] == 'audio' and _t38(client) is False
    assert client.get('/admin/sip/status', headers=ADMIN).json()['t38_off_reason'] == 'network'


def test_a_person_who_tries_t38_again_on_the_same_network_keeps_it(client, network):
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    _set_t38(client, True)
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert _t38(client) is True
    assert _check(client)['switched'] is None and _t38(client) is True
    record = sip_fax_mode.read(_values(client))
    assert (record['reason'], record['network']) == ('chosen', BLOCKED)


def test_a_person_who_chose_audio_fax_is_left_alone_on_a_good_network(client, network):
    network['row'] = COLIMA_BRIDGED
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    _set_t38(client, False)
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert _check(client)['switched'] is None and _t38(client) is False


def test_a_fax_with_no_data_back_on_a_bad_network_is_retried_with_t38_only_once_the_network_is_fixed(client, network):
    network['row'] = COLIMA_BRIDGED
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    runtime = client.app.state.configuration_runtime
    # Same network, carrier trouble: the failed call keeps T.38 off however often the network is checked.
    asyncio.run(sip_fax_mode.switch_to_audio(runtime, sip_fax_mode.NO_DATA_BACK))
    assert sip_fax_mode.read(_values(client))['network'] == OPEN
    assert _check(client)['switched'] is None and _t38(client) is False
    # On a network that changed port numbers, the fix brings T.38 back.
    _set_t38(client, True)
    network['row'] = COLIMA_USER
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    asyncio.run(sip_fax_mode.switch_to_audio(runtime, sip_fax_mode.NO_DATA_BACK))
    assert sip_fax_mode.read(_values(client))['network'] == BLOCKED
    network['row'] = COLIMA_BRIDGED
    assert _check(client)['switched'] == 't38' and _t38(client) is True


def test_an_older_no_data_record_comes_back_on_once_the_network_is_fixed(client, network):
    """Before this check existed, the probe's record shows the port-changing network the failure happened on."""
    values = _values(client)
    sip_trunk.write_public_address(values, COLIMA_USER[1])
    _set_t38(client, False)
    sip_fax_mode.write(_values(client), 'audio', sip_fax_mode.NO_DATA_BACK, '2026-10-04T03:00:57')
    network['row'] = COLIMA_BRIDGED
    assert _check(client)['switched'] == 't38' and _t38(client) is True


def test_no_ports_is_not_claimed_while_the_network_section_asks_for_a_forward(isolated_installation, monkeypatch,
                                                                             network):
    network['row'] = DESKTOP_MAC
    with _client(monkeypatch, {**TRUNK, 'SIP_EXTERNAL_ADDRESS': '198.51.100.7'}) as client:
        assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
        report = client.get('/admin/sip/network', headers=ADMIN).json()
        assert (report['t38'], report['why']) == (UNKNOWN, 'typed')
        assert report['fix_text'].startswith('Check that your router forwards UDP ports 4000\u20134039')
        assert client.get('/admin/sip/status', headers=ADMIN).json()['ports_text'] is None
    network['row'] = NO_STUN
    with _client(monkeypatch, TRUNK) as client:
        assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
        assert client.get('/admin/sip/status', headers=ADMIN).json()['ports_text'] == (
            'No ports need to be opened or forwarded.')


@pytest.mark.parametrize('preset, extra, switched', [
    ('signalwire', {'SIP_TRUNK_HOST': 'example.sip.signalwire.com'}, 'audio'),
    ('bt-one-voice', {'SIP_TRUNK_HOST': 'sip.example.net', 'SIP_TRUNK_AUTH': 'ip',
                      'SIP_EXTERNAL_ADDRESS': '198.51.100.7'}, None),
])
def test_other_carriers_follow_the_network_unless_they_turn_t38_into_audio_themselves(isolated_installation, monkeypatch,
                                                                                    network, preset, extra, switched):
    with _client(monkeypatch, {**TRUNK, 'SIP_TRUNK_PRESET': preset, **extra}) as client:
        assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
        if preset == 'signalwire':
            assert _t38(client) is False
            assert sip_fax_mode.off_sentence('network', carrier='SignalWire') == (
                'Off: your network changes port numbers, so T.38 fax data most likely cannot come back; Faxbot uses '
                'audio fax until the network is fixed.')
        _set_t38(client, True)
        assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
        assert _check(client)['switched'] in (None, switched)


def test_a_phone_system_has_no_network_check(isolated_installation, monkeypatch, network):
    with _client(monkeypatch, {'SIP_TRUNK_PRESET': 'avaya-ipoffice', 'SIP_TRUNK_HOST': '192.168.1.20',
                               'SIP_TRUNK_AUTH': 'ip'}) as client:
        assert client.get('/admin/sip/network', headers=ADMIN).json() == {
            'applies': False, 'checked': False, 't38': None,
            'text': 'Your phone system is on your local network, so fax over IP needs no network check.'}
        assert _check(client)['applies'] is False


def test_check_again_needs_a_carrier_and_write_access(isolated_installation, monkeypatch, network):
    with _client(monkeypatch, {}) as client:
        response = client.post('/admin/sip/network/check', headers=ADMIN)
        assert response.status_code == 400
        assert response.json()['detail'] == 'Choose a carrier before checking the network.'
        sender = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': ['fax:send']})
        key = {'X-API-Key': sender.json()['token']}
        assert client.get('/admin/sip/network', headers=key).status_code == 403
        assert client.post('/admin/sip/network/check', headers=key).status_code == 403


# -- when the check runs ---------------------------------------------------------------------------------------

def test_the_start_check_waits_and_is_skipped_by_tests_unless_asked(client, network):
    runtime = client.app.state.configuration_runtime
    assert asyncio.run(sip_network.check_at_start(runtime)) is None
    assert sip_network.read_check(_values(client)) is None
    outcome = asyncio.run(sip_network.check_at_start(runtime, delay=0))
    assert outcome['check']['t38'] == BLOCKED and outcome['switched'] == 'audio'
    assert sip_network.read_check(_values(client))['platform'] == 'colima_user'


@pytest.mark.asyncio
async def test_the_watcher_runs_the_whole_check_with_the_running_installation(monkeypatch):
    settings = values()
    runs = []

    async def run_check(runtime, records=None, *, fresh=True):
        runs.append(runtime)
    monkeypatch.setattr(sip_network, 'run_check', run_check)
    task = asyncio.create_task(sip_http.watch_public_address(minutes=0.001, values_source=lambda: settings,
                                                             runtime='the installation'))
    try:
        for _ in range(100):
            await asyncio.sleep(0.02)
            if runs:
                break
    finally:
        task.cancel()
    assert runs and runs[0] == 'the installation'
