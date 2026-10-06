"""The network check for fax over IP: where Faxbot runs, whether T.38 data can come back, what to do.

The fixtures are the measurements of 2026-10-03/04 (Mac host, Colima's three
networks, all from a container), with every internet address replaced by a
documentation address. No test reaches the internet or a router.
"""
import asyncio
import json
import socket
import struct
import sys
import time

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
        'Fax over IP (T.38) cannot work here: your network changes port numbers, which Telnyx cannot handle for fax '
        'over IP.')
    assert sip_network.verdict_text(check, 'SignalWire') == (
        'Fax over IP (T.38) most likely cannot work here, because your network changes port numbers.')
    remedy = sip_network.fix(check)
    assert remedy['text'].startswith('Connect Docker on this Mac directly to your local network')
    # Recreate keeps the machine's size; delete never takes --data (that would erase Faxbot's faxes).
    assert remedy['steps'] == [
        'colima version   # 0.9 or later keeps your faxes when the machine is recreated',
        'colima list', 'colima delete default',
        'colima start default --cpu 2 --memory 4 --disk 20 --network-address --network-mode bridged '
        '--network-interface "$(route -n get default | awk \'/interface:/{print $2}\')" --network-preferred-route',
        'docker compose up -d']
    assert not any('--data' in step or ' -f' in step or ' -d' in step.split('compose up')[0] for step in remedy['steps'])
    assert 'Never add --data to the delete command' in remedy['note'] and 'The disk keeps its old size' in remedy['note']
    bridged = sip_network.assess(values(), *reversed(COLIMA_BRIDGED))
    assert sip_network.fix(bridged) is None
    assert sip_network.verdict_text(bridged, 'Telnyx') == (
        'Fax over IP (T.38) can work here, because your network keeps port numbers unchanged.')


PUBLISHED = (4000, 4039)


@pytest.mark.parametrize('row, published, mapping, start, steps', [
    (DESKTOP_MAC, None, None, 'Forward UDP ports 4000–4039 on your router to this computer, restart Faxbot with its '
     'fax ports (the command below), and enter your internet address under Internet address.', 'fax ports'),
    (DESKTOP_MAC, PUBLISHED, None, 'Forward UDP ports 4000–4039 on your router to this computer, and enter your '
     'internet address', None),
    (LINUX_LAN_CHANGES, None, {'state': 'no_fax_ports'}, 'Restart Faxbot with its fax ports (the first command below), '
     'and Faxbot asks your router to open them.', 'fax ports'),
    (LINUX_LAN_CHANGES, PUBLISHED, {'state': 'refused'}, 'Turn on UPnP or NAT-PMP on your router so Faxbot can open UDP '
     'ports 4000–4039 itself. Or forward those ports to this computer', None),
    (LINUX_LAN_CHANGES, PUBLISHED, {'state': 'off'}, 'Forward UDP ports 4000–4039 on your router to this computer, '
     'and enter', None),
    (CLOUD_NAT, None, None, 'Give the server its own public address, open UDP ports 4000–4039 in its firewall',
     'fax ports'),
    (SHARED_ISP, None, None, 'No router setting can change this.', None),
    (NO_STUN, None, None, 'If a firewall limits outgoing traffic', None),
])
def test_each_platform_gets_its_own_fix_and_audio_fax_keeps_working(row, published, mapping, start, steps, tmp_path):
    found, result = row
    check = sip_network.assess(values(), result, found, mapping=mapping, published=published)
    remedy = sip_network.fix(check)
    assert remedy['text'].startswith(start)
    assert (sip_network.FAX_PORTS_COMMAND in remedy['steps']) is (steps == 'fax ports')
    settings = values(FAX_DATA_DIR=str(tmp_path))
    sip_network.write_check(settings, check)
    assert sip_network.report(settings)['audio_text'] == 'Audio fax keeps working meanwhile.'


def test_colima_on_the_local_network_behind_a_router_that_changes_ports_lists_its_own_address():
    found = Discovery(lima=True, hops=('172.17.0.1', '192.168.68.1', '203.0.113.2'), **APPLE)
    check = sip_network.assess(values(), probe(4002, 61001, 61001), found, mapping={'state': 'refused'},
                               published=PUBLISHED)
    assert check['platform'] == 'colima_bridged'
    remedy = sip_network.fix(check)
    assert remedy['text'].endswith('Or forward those ports to the address the command below lists and enter your '
                                   'internet address under Internet address.')
    assert remedy['steps'] == ['colima list']


def test_network_open_says_t38_is_tried_first_and_audio_carries_the_fax_when_the_carrier_declines(tmp_path):
    settings = values(FAX_DATA_DIR=str(tmp_path))
    sip_network.write_check(settings, sip_network.assess(settings, COLIMA_BRIDGED[1], COLIMA_BRIDGED[0]))
    report = sip_network.report(settings)
    assert report['tries_text'] == ('Faxbot tries fax over IP (T.38) first. When the carrier declines it, the fax goes '
                                    'through as audio.')
    assert report['office_text'] == 'Your network is ready for faxing over the internet.'
    assert report['audio_text'] is None and report['fix_text'] is None
    # With T.38 switched off above it (by Faxbot or a person), the panel never says T.38 is tried first.
    off = values(FAX_DATA_DIR=str(tmp_path), SIP_T38_ENABLED='false')
    assert sip_network.report(off)['tries_text'] == ('Your network allows fax over IP (T.38), but new calls use '
                                                     'audio fax, as set above.')


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
    assert report['platform_text'] == 'Faxbot runs in Docker on this Mac, on a private network inside the Mac.'
    assert report['checked_at'].endswith('Z') and report['fix_steps'][1] == 'colima list'
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
        assert report['fix_text'].startswith('Forward UDP ports 4000\u20134039 on your router to this computer')
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
                'Off: your network changes port numbers, so fax over IP (T.38) most likely cannot work; Faxbot sends '
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


def test_system_diagnostics_shows_the_network_check(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app import config, diagnostics_report as report
    settings = values(FAX_DATA_DIR=str(tmp_path))
    monkeypatch.setattr(report, '_uses_trunk', lambda request: True)
    monkeypatch.setattr(config, 'configuration_values', lambda: settings)
    context = SimpleNamespace(request=None, identity=None)
    [waiting] = asyncio.run(report.network_for_fax(context))
    assert (waiting.id, waiting.section, waiting.status, waiting.sentence) == (
        'engine.network', 'engine', report.OK, 'Faxbot has not checked this network yet.')
    sip_network.write_check(settings, sip_network.assess(settings, COLIMA_USER[1], COLIMA_USER[0]))
    [blocked] = asyncio.run(report.network_for_fax(context))
    assert (blocked.status, blocked.fix_page) == (report.ATTENTION, 'providers/trunk')
    assert blocked.sentence == ('Your network needs one change so faxes can go over the internet. The carrier page '
                                'shows what to do. Faxes still go through meanwhile.')
    assert blocked.title == 'Faxing over the internet'
    monkeypatch.setattr(report, '_uses_trunk', lambda request: False)
    assert asyncio.run(report.network_for_fax(context)) == []


# -- when the check runs ---------------------------------------------------------------------------------------

def test_the_start_check_waits_and_is_skipped_by_tests_unless_asked(client, network):
    runtime = client.app.state.configuration_runtime
    assert asyncio.run(sip_network.check_at_start(runtime)) is None
    assert sip_network.read_check(_values(client)) is None
    # The start check acts only on the same answer twice in a row: it confirms once more before switching.
    outcome = asyncio.run(sip_network.check_at_start(runtime, delay=0, confirm=0, keep=False))
    assert outcome['check']['t38'] == BLOCKED and outcome['check']['count'] == 2 and outcome['switched'] == 'audio'
    assert sip_network.read_check(_values(client))['platform'] == 'colima_user'


def test_unattended_checks_switch_only_after_the_same_answer_twice_and_apply_switches_at_once(client, network):
    runtime = client.app.state.configuration_runtime
    first = asyncio.run(sip_network.run_check(runtime, unattended=True))
    assert (first['switched'], first['awaiting'], first['check']['count']) == (None, 'audio', 1)
    assert _t38(client) is True
    second = asyncio.run(sip_network.run_check(runtime, unattended=True))
    assert (second['switched'], second['check']['count']) == ('audio', 2) and _t38(client) is False
    # One good answer is not enough to switch back unattended; Check again is.
    network['row'] = COLIMA_BRIDGED
    assert asyncio.run(sip_network.run_check(runtime, unattended=True))['awaiting'] == 't38' and _t38(client) is False
    network['row'] = COLIMA_USER
    flapped = asyncio.run(sip_network.run_check(runtime, unattended=True))
    assert flapped['switched'] is None and flapped['check']['count'] == 1 and _t38(client) is False
    network['row'] = COLIMA_BRIDGED
    assert _check(client)['switched'] == 't38' and _t38(client) is True


@pytest.mark.asyncio
async def test_the_watcher_runs_the_whole_check_with_the_running_installation(monkeypatch):
    settings = values()
    runs = []

    async def run_check(runtime, records=None, *, fresh=True, unattended=False):
        runs.append((runtime, unattended))
    monkeypatch.setattr(sip_network, 'run_check', run_check)
    task = asyncio.create_task(sip_http.watch_public_address(minutes=0.001, values_source=lambda: settings,
                                                             runtime='the installation'))
    try:
        # Waits for the watcher (a hang guard, not a timing assumption): a loaded machine can take seconds.
        deadline = asyncio.get_running_loop().time() + 30
        while not runs and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
    assert runs and runs[0] == ('the installation', True)


# -- Faxbot's fax ports: forwarded by the person, or opened on the router by Faxbot ------------------------------

class StandInRouter:
    """Stands in for port_mapping.Router: records what Faxbot asked; the wire format is in test_port_mapping.py."""
    opened, renewed, closed = [], [], []
    external, refuse = '198.51.100.7', False

    def __init__(self, gateway, **_):
        self.gateway = gateway

    def open(self, first, last, *, lifetime=3600):
        if StandInRouter.refuse:
            return None, ['pcp_no_address', 'natpmp_no_answer', 'upnp_no_address']
        StandInRouter.opened.append((self.gateway, first, last))
        from app.port_mapping import Lease
        return Lease('natpmp', self.gateway, first, last, StandInRouter.external, lifetime, time.time()), None

    def renew(self, lease, *, lifetime=3600):
        StandInRouter.renewed.append((lease.first, lease.last))
        lease.granted_at = time.time()
        return lease

    def close(self, lease):
        StandInRouter.closed.append((lease.gateway, lease.first, lease.last))


@pytest.fixture
def stand_in_router(monkeypatch):
    from app import port_mapping
    StandInRouter.opened, StandInRouter.renewed, StandInRouter.closed = [], [], []
    StandInRouter.external, StandInRouter.refuse = '198.51.100.7', False
    monkeypatch.setattr(port_mapping, 'Router', StandInRouter)
    return StandInRouter


def _publish_fax_ports(client, first=4000, last=4039):
    """What asterisk/start.sh records when docker-compose.fax-ports.yml publishes the range."""
    path = sip_network.media_ports_path(_values(client))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'media_ports': f'{first}-{last}'}) + '\n')


def _put(client, **changes):
    current = client.get('/admin/settings', headers=ADMIN).json()
    response = client.put('/admin/settings', headers=ADMIN, json={
        'expected_revision_id': current['_meta']['desired_revision_id'], **changes})
    assert response.status_code == 200, response.text


def test_forwarded_fax_ports_bring_t38_back_without_touching_the_switch(client, network, stand_in_router):
    network['row'] = DESKTOP_MAC
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert _t38(client) is False
    # The person forwards the ports, starts Faxbot with the fax ports file and types the internet address.
    _publish_fax_ports(client)
    _put(client, sip_external_address='198.51.100.7')
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    report = client.get('/admin/sip/network', headers=ADMIN).json()
    assert (report['t38'], report['why'], report['action']) == (OPEN, 'forwarded', 'turned_on')
    assert report['text'] == "Fax over IP (T.38) can work here, because your router forwards Faxbot's fax ports."
    assert _t38(client) is True and stand_in_router.opened == []  # a typed address: the person forwards, not Faxbot
    assert 'external_media_address=198.51.100.7' in sip_trunk.configuration_path(_values(client)).read_text()


def test_faxbot_opens_its_fax_ports_on_the_router_advertises_them_and_closes_them_when_turned_off(
        client, network, stand_in_router):
    network['row'] = LINUX_LAN_CHANGES
    _publish_fax_ports(client)
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert stand_in_router.opened == [('192.168.1.1', 4000, 4039)]
    report = client.get('/admin/sip/network', headers=ADMIN).json()
    assert (report['t38'], report['why'], report['router_state']) == (OPEN, 'router_mapped', 'open')
    assert report['router_text'] == ('Faxbot opened UDP ports 4000–4039 on your router so fax data can come back. '
                                     'It renews them while it runs and closes them when it stops.')
    assert _t38(client) is True
    # The address and these exact ports are advertised, as on a network that keeps port numbers.
    assert sip_trunk.read_public_address(_values(client)) | {'probed_at': None} == {
        'ip': '198.51.100.7', 'ports_preserved': True, 'router_ports': True, 'probed_at': None}
    assert sip_network.network_allows_t38(_values(client)) is True
    # The next check keeps the same lease (renewal waits for half its lifetime).
    assert _check(client)['router_state'] == 'open' and len(stand_in_router.opened) == 1
    # Turned off: Faxbot closes the ports, and T.38 follows the network at once.
    _put(client, sip_router_ports=False)
    body = _check(client)
    assert stand_in_router.closed == [('192.168.1.1', 4000, 4039)]
    assert (body['router_state'], body['t38'], body['switched'], body['router_ports_enabled']) == (
        'off', BLOCKED, 'audio', False)
    assert body['router_text'] == 'Faxbot does not ask your router to open ports, because that is turned off.'
    assert not sip_network.lease_path(_values(client)).exists()
    assert sip_trunk.read_public_address(_values(client))['ports_preserved'] is False


def test_trunk_status_agrees_when_the_router_opened_or_the_person_forwarded_the_fax_ports(
        client, network, stand_in_router):
    network['row'] = LINUX_LAN_CHANGES
    _publish_fax_ports(client)
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    # Asterisk restarted and names the internet address with the opened ports.
    sip_trunk.public_address_path(_values(client)).with_name('public-address.applied').write_text('198.51.100.7\n')
    sip_http._probes.clear()
    status = client.get('/admin/sip/status', headers=ADMIN).json()
    assert (status['network_t38'], status['address_changed'], status['ports_text']) == (OPEN, False, None)
    assert status['public_address_text'] == ("Faxbot's internet address is 198.51.100.7, and your router passes Faxbot's "
                                             'fax ports through, so Telnyx is told exactly where to send fax data.')
    assert 'internet address changed' not in status['message']
    # The person forwards the ports instead and enters the address.
    _put(client, sip_external_address='198.51.100.7')
    network['row'] = DESKTOP_MAC
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    status = client.get('/admin/sip/status', headers=ADMIN).json()
    assert (status['network_t38'], status['address_changed'], status['ports_text']) == (OPEN, False, None)
    assert status['public_address_text'] == ('Faxbot tells Telnyx to send calls and fax data to 198.51.100.7, the '
                                             'address you entered.')


def test_a_router_that_refuses_or_sits_behind_another_router_gets_the_forward_to_do(client, network, stand_in_router):
    network['row'] = LINUX_LAN_CHANGES
    _publish_fax_ports(client)
    stand_in_router.refuse = True
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    report = client.get('/admin/sip/network', headers=ADMIN).json()
    assert (report['t38'], report['router_state']) == (BLOCKED, 'refused')
    assert report['router_text'] == 'Your router did not open UDP ports 4000–4039 for Faxbot.'
    assert report['fix_text'].startswith('Turn on UPnP or NAT-PMP on your router')
    # The router opens them, but its own internet address is not the one the internet sees.
    stand_in_router.refuse, stand_in_router.external = False, '100.72.0.9'
    body = _check(client)
    assert (body['router_state'], body['why'], body['shared_address']) == ('behind_another_router', 'shared_address',
                                                                            True)
    assert stand_in_router.closed == [('192.168.1.1', 4000, 4039)]
    stand_in_router.external = '192.168.0.2'
    body = _check(client)
    assert (body['why'], body['shared_address']) == ('behind_another_router', False)
    assert body['fix_text'].startswith('Forward UDP ports 4000–4039 on the router in front of yours too')


def test_nothing_is_asked_of_the_router_when_ports_are_kept_or_not_published(client, network, stand_in_router):
    network['row'] = LINUX_LAN
    _publish_fax_ports(client)
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    assert client.get('/admin/sip/network', headers=ADMIN).json()['router_state'] == 'not_needed'
    network['row'] = LINUX_LAN_CHANGES
    sip_network.media_ports_path(_values(client)).unlink()
    assert _check(client)['router_state'] == 'no_fax_ports'
    # Behind Docker Desktop or Colima's own network another layer changes ports: the router is not asked.
    _publish_fax_ports(client)
    network['row'] = COLIMA_SHARED
    assert _check(client)['router_state'] == 'no_router'
    assert stand_in_router.opened == []


def test_the_router_ports_are_renewed_and_closed_when_faxbot_stops(client, network, stand_in_router):
    network['row'] = LINUX_LAN_CHANGES
    _publish_fax_ports(client)
    assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
    runtime = client.app.state.configuration_runtime
    lease = sip_network.read_lease(_values(client))
    lease.granted_at -= lease.lifetime  # half its lifetime has passed
    sip_network._keep_lease(_values(client), lease)

    async def run_until_renewed():
        task = asyncio.create_task(sip_network.keep_router_ports(runtime, idle=0.05))
        deadline = asyncio.get_running_loop().time() + 30  # a hang guard, not a timing assumption
        while not stand_in_router.renewed and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run_until_renewed())
    assert stand_in_router.renewed == [(4000, 4039)]
    assert stand_in_router.closed == [('192.168.1.1', 4000, 4039)]
    assert sip_network.read_lease(_values(client)) is None


def test_the_media_ports_record_is_read_only_when_well_formed(tmp_path):
    settings = values(FAX_DATA_DIR=str(tmp_path))
    path = sip_network.media_ports_path(settings)
    path.parent.mkdir(parents=True)
    for text, expected in (('{"media_ports": "4000-4039"}', (4000, 4039)), ('{"media_ports": "4039-4000"}', None),
                           ('{"media_ports": "80-90"}', None), ('not json', None)):
        path.write_text(text)
        assert sip_network.read_media_ports(settings) == expected
