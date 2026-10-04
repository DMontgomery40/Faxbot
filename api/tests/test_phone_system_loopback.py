"""A phone system on the local network exchanges faxes with Faxbot, both ways, over T.38 and as audio.

Runs only with FAXBOT_NATIVE_PROOF=1 (``make native-proof``). Nothing is
published on the Docker host: a router container stands in for the Linux
Docker host and does what docker-compose.phone-system.yml makes Docker do,
on two --internal networks with no internet access:

- the "LAN": the router's address there is FAXBOT_LAN_ADDRESS, and a second
  Asterisk acts as the phone system (Avaya IP Office in production);
- Faxbot's own Docker network: Faxbot's Asterisk, whose default route is the
  router, as a container's is the Docker host.

The router forwards SIP (5060, UDP and TCP) and the 20 published media ports
from its LAN address to Faxbot (DNAT keeps the phone system's own address, as
Docker Engine on Linux does) and masquerades Faxbot's own traffic to its LAN
address. Faxbot's trunk is the avaya-ipoffice preset exactly as Faxbot
renders it (UK, numbers dialled as a phone there dials them, outside-line
prefix 9), started by the image's own start.sh with the overlay's
environment. The phone system is strict: it sends media only to the address
in Faxbot's SDP (no latching), and it recognises Faxbot only by the router's
LAN address, so a wrong advertised address leaves the fax without data.

Asserts each fax went through with the expected mode and pages, the phone
system received the dialled string 9 + the national number, Faxbot identified
the phone system's INVITE by its own address, and Faxbot's container address
never appears anywhere in what the phone system received (Via, Contact, SDP
o= and c=): the only address Faxbot names is the LAN address.
"""
import json
import os
from pathlib import Path
import re
import time
import uuid

import pytest

from app import ami, sip_trunk
from app.config_values import ConfigurationValues
from tests.test_t38_loopback import (AMI_PASSWORD, AMI_USER, CAPTURE, ROUTER_DOCKERFILE, SECRET, Docker,
                                     _ami_originate, _interface_for, never_latch, proof_pages, wait_booted)


pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_NATIVE_PROOF') != '1',
                       reason='Set FAXBOT_NATIVE_PROOF=1 to run the phone system loopback proof.'),
]

ROOT = Path(__file__).resolve().parents[2]
MEDIA_PORTS = '4000-4019'
# Synthetic UK numbers (Ofcom drama ranges): Faxbot's fax number on the phone system, and an outside number.
FAXBOT_NUMBER = '+442079460001'
OUTSIDE = '+441632960123'
OUTSIDE_DIALLED = '901632960123'


def _faxbot_values(pbx, *, t38):
    return ConfigurationValues.from_environment({
        'SIP_TRUNK_PRESET': 'avaya-ipoffice', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': pbx,
        'FAX_DEFAULT_COUNTRY': 'GB', 'SIP_TRUNK_DIAL_FORMAT': 'local', 'SIP_TRUNK_DIAL_PREFIX': '9',
        'SIP_TRUNK_CALLER_ID': FAXBOT_NUMBER, 'SIP_TRUNK_DIDS': FAXBOT_NUMBER,
        'SIP_T38_ENABLED': 'true' if t38 else 'false', 'FAX_HEADER': 'Faxbot phone system proof'})


def _pbx_values(lan_address, *, t38):
    """The phone system's side: a SIP trunk to Faxbot's LAN address, recognised by that address."""
    return ConfigurationValues.from_environment({
        'SIP_TRUNK_PRESET': 'custom', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': lan_address,
        'SIP_TRUNK_CALLER_ID': OUTSIDE, 'SIP_T38_ENABLED': 'true' if t38 else 'false',
        'FAX_HEADER': 'Phone system proof'})


def phone_system_exchange(tmp_path, *, t38):
    docker = Docker()
    image = os.environ.get('FAXBOT_NATIVE_IMAGE') or 'faxbot-native:t38-proof'
    if not os.environ.get('FAXBOT_NATIVE_IMAGE'):
        docker.run('build', '--quiet', '--tag', image, str(ROOT / 'asterisk'), timeout=3600)
    router_image = os.environ.get('FAXBOT_ROUTER_IMAGE') or 'faxbot-native:nat-router'
    if not os.environ.get('FAXBOT_ROUTER_IMAGE'):
        docker.run('build', '--quiet', '--tag', router_image, '-', input_text=ROUTER_DOCKERFILE, timeout=1200)
    lan = docker.prefix + '-lan'
    try:
        docker.network = docker.prefix
        for network in (docker.network, lan):
            docker.run('network', 'create', '--internal', '--label', 'com.faxbot.scope=t38-proof', network)
        host = f'{docker.prefix}-host'
        docker.run('run', '--detach', '--name', host, '--network', docker.network, '--cap-add', 'NET_ADMIN',
                   '--sysctl', 'net.ipv4.ip_forward=1', '--label', 'com.faxbot.scope=t38-proof',
                   router_image, 'sleep', 'infinity')
        docker.containers.append(host)
        docker.run('network', 'connect', lan, host)
        faxbot = f'{docker.prefix}-faxbot'
        docker.run('run', '--detach', '--name', faxbot, '--network', docker.network, '--cap-add', 'NET_ADMIN',
                   '--label', 'com.faxbot.scope=t38-proof', '--entrypoint', 'sleep', image, 'infinity')
        docker.containers.append(faxbot)
        pbx = f'{docker.prefix}-pbx'
        docker.run('run', '--detach', '--name', pbx, '--network', lan, '--label', 'com.faxbot.scope=t38-proof',
                   '--entrypoint', 'sleep', image, 'infinity')
        docker.containers.append(pbx)
        capture = docker.start('api', 'python:3.11-slim', 'python', '-c', CAPTURE)
        docker.run('network', 'connect', lan, capture)

        def on(container, network):
            return docker.run('inspect', '--format',
                              '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}',
                              container).stdout.strip()
        host_bridge, lan_address = on(host, docker.network), on(host, lan)
        faxbot_address, pbx_address = on(faxbot, docker.network), on(pbx, lan)
        capture_bridge, capture_lan = on(capture, docker.network), on(capture, lan)
        assert all((host_bridge, lan_address, faxbot_address, pbx_address, capture_bridge, capture_lan))

        # What Docker Engine does for the published ports: DNAT from the LAN address, masquerade on the way out.
        first, last = MEDIA_PORTS.split('-')
        lan_interface = _interface_for(docker, host, lan_address)
        for rule in (['-p', 'udp', '--dport', '5060', '-j', 'DNAT', '--to-destination', f'{faxbot_address}:5060'],
                     ['-p', 'tcp', '--dport', '5060', '-j', 'DNAT', '--to-destination', f'{faxbot_address}:5060'],
                     ['-p', 'udp', '--dport', f'{first}:{last}', '-j', 'DNAT', '--to-destination', faxbot_address]):
            docker.run('exec', host, 'iptables', '-t', 'nat', '-A', 'PREROUTING', '-d', lan_address, *rule)
        docker.run('exec', host, 'iptables', '-t', 'nat', '-A', 'POSTROUTING', '-o', lan_interface, '-j',
                   'MASQUERADE')
        docker.run('exec', faxbot, 'ip', 'route', 'replace', 'default', 'via', host_bridge)

        faxbot_values, pbx_values = _faxbot_values(pbx_address, t38=t38), _pbx_values(lan_address, t38=t38)
        for container, text in ((faxbot, sip_trunk.render_pjsip(faxbot_values)),
                                (pbx, never_latch(sip_trunk.render_pjsip(pbx_values)) if t38 else
                                 sip_trunk.render_pjsip(pbx_values).replace('rtp_symmetric=yes', 'rtp_symmetric=no'))):
            rendered = tmp_path / f'{container}.conf'
            rendered.write_text(text)
            docker.run('exec', container, 'mkdir', '-p', '/faxdata/asterisk', '/faxdata/outbound')
            docker.run('cp', str(rendered), f'{container}:/faxdata/asterisk/pjsip.conf')
        logger = tmp_path / 'logger.conf'
        logger.write_text('[general]\ndateformat=%F %T\n\n[logfiles]\nconsole => notice,warning,error,verbose\n')
        login = ['--env', f'ASTERISK_AMI_USERNAME={AMI_USER}', '--env', f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}',
                 '--env', f'ASTERISK_INBOUND_SECRET={SECRET}']
        for container, api, extra in (
                (faxbot, capture_bridge, ['--env', f'FAXBOT_PHONE_SYSTEM_ADDRESS={lan_address}',
                                          '--env', f'FAXBOT_MEDIA_PORTS={MEDIA_PORTS}']),
                (pbx, capture_lan, [])):
            docker.run('cp', str(logger), f'{container}:/etc/asterisk/logger.conf')
            docker.run('exec', '--detach', *login, '--env', f'FAXBOT_API_URL=http://{api}:8080', *extra, container,
                       'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        wait_booted(docker, faxbot)
        wait_booted(docker, pbx)
        for container in (faxbot, pbx):
            docker.asterisk(container, 'pjsip set logger on')
        # Each side's OPTIONS check reaches the other through the router before any fax.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not all(
                'Avail' in docker.asterisk(container, 'pjsip show contacts') for container in (faxbot, pbx)):
            time.sleep(1)

        sent = tmp_path / 'proof.tiff'
        pages = proof_pages()
        pages[0].save(sent, save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
        results = {}
        for direction, sender, values, number in (('to_phone_system', faxbot, faxbot_values, OUTSIDE),
                                                  ('from_phone_system', pbx, pbx_values, FAXBOT_NUMBER)):
            docker.run('cp', str(sent), f'{sender}:/faxdata/outbound/proof.tiff')
            docker.run('exec', capture, 'rm', '-f', '/tmp/capture.json', check=False)
            fields = ami.originate_fields_for(values, uuid.uuid4().hex, number, '/faxdata/outbound/proof.tiff',
                                              attempt_id=uuid.uuid4().hex)
            result = _ami_originate(docker, sender, fields)
            captured, deadline = None, time.monotonic() + 30
            while time.monotonic() < deadline and captured is None:
                probe = docker.read(capture, '/tmp/capture.json')
                captured = json.loads(probe) if probe.strip() else None
                if captured is None:
                    time.sleep(1)
            results[direction] = {'dialled': fields.get('Variable', ''), 'result': result, 'captured': captured}
        return {
            'addresses': {'lan_address': lan_address, 'faxbot_container': faxbot_address, 'pbx': pbx_address,
                          'router_on_faxbot_network': host_bridge},
            'results': results,
            'pbx_log': docker.read(pbx, '/tmp/asterisk.log'),
            'faxbot_log': docker.read(faxbot, '/tmp/asterisk.log'),
            'lan_record': docker.read(faxbot, '/faxdata/asterisk/lan-address'),
            'faxbot_transport': docker.run('exec', faxbot, 'grep', '-E', 'external_|local_net',
                                           '/etc/asterisk/pjsip.conf', check=False).stdout.strip(),
            'faxbot_media': docker.run('exec', faxbot, 'grep', '-hE', '^(rtp|udptl)(start|end)=',
                                       '/etc/asterisk/rtp.conf', '/etc/asterisk/udptl.conf').stdout.split(),
        }
    finally:
        docker.close()
        docker.run('network', 'rm', lan, check=False)


def _received_by_pbx(log, lan_address):
    """The SIP messages the phone system received from Faxbot, as its pjsip logger printed them."""
    blocks = re.split(r'\n(?=<--- |\[.*?\] VERBOSE.*?<--- )', log)
    return [block for block in blocks if 'Received SIP' in block and lan_address in block.split('\n', 1)[0]]


@pytest.mark.parametrize('t38', [True, False], ids=['t38', 'audio'])
def test_a_phone_system_on_the_local_network_faxes_with_faxbot_both_ways(tmp_path, t38):
    outcome = phone_system_exchange(tmp_path, t38=t38)
    addresses, results = outcome['addresses'], outcome['results']
    lan_address, faxbot_address = addresses['lan_address'], addresses['faxbot_container']
    pbx_log, faxbot_log = outcome['pbx_log'], outcome['faxbot_log']
    received = _received_by_pbx(pbx_log, lan_address)
    faxbot_sdp = sorted(set(re.findall(r'c=IN IP4 ([0-9.]+)', '\n'.join(received))))
    evidence = {
        'addresses': addresses,
        'faxbot_transport': outcome['faxbot_transport'],
        'faxbot_media_ranges': outcome['faxbot_media'],
        'lan_record': json.loads(outcome['lan_record']) if outcome['lan_record'].strip() else None,
        'messages_from_faxbot_seen_by_pbx': len(received),
        'faxbot_sdp_addresses_seen_by_pbx': faxbot_sdp,
        'faxbot_container_address_in_pbx_log': faxbot_address in pbx_log,
        'pbx_invite_reached_faxbot_from_its_own_address': bool(re.search(
            r'Received SIP request \(\d+ bytes\) from UDP:' + re.escape(addresses['pbx']) + r':5060 --->\s*INVITE',
            faxbot_log)),
    }
    for direction, item in results.items():
        result = item['result'] or {}
        body = (item['captured'] or {}).get('body') or {}
        evidence[direction] = {'sender_status': result.get('Status'), 'sender_pages': result.get('Pages'),
                               'mode': result.get('Mode'), 'receiver_status': body.get('faxstatus'),
                               'receiver_pages': body.get('faxpages'), 'to_number': body.get('to_number'),
                               'from_number': body.get('from_number'),
                               'receiver_t38': (body.get('call') or {}).get('t38')}
    print(f'\nPHONE_SYSTEM_{"T38" if t38 else "AUDIO"}_EVIDENCE ' + json.dumps(evidence, indent=2))

    assert evidence['lan_record'] == {'address': lan_address, 'sip_port': 5060, 'media_ports': MEDIA_PORTS}
    assert outcome['faxbot_transport'] == (f'external_media_address={lan_address}\n'
                                           f'external_signaling_address={lan_address}')
    assert outcome['faxbot_media'] == ['rtpstart=4006', 'rtpend=4019', 'udptlstart=4000', 'udptlend=4005']
    # The phone system only ever hears the LAN address from Faxbot; Docker's network address never leaks.
    assert received and faxbot_sdp == [lan_address]
    assert not evidence['faxbot_container_address_in_pbx_log']
    # Faxbot recognised the phone system's INVITE by the phone system's own address.
    assert evidence['pbx_invite_reached_faxbot_from_its_own_address']
    expected_mode = 'T38' if t38 else 'audio'
    for direction in ('to_phone_system', 'from_phone_system'):
        item = evidence[direction]
        assert (item['sender_status'], item['sender_pages'], item['mode']) == ('SUCCESS', '2', expected_mode), item
        assert (item['receiver_status'], item['receiver_pages']) == ('SUCCESS', 2), item
        assert item['receiver_t38'] is t38
    # Faxbot dialled the outside number the way a phone in the UK does, after the outside-line prefix.
    assert evidence['to_phone_system']['to_number'] == OUTSIDE_DIALLED
    assert evidence['from_phone_system']['to_number'] == FAXBOT_NUMBER
