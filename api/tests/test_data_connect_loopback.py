"""NTT Data Connect calls between two Faxbot Asterisk containers (asterisk patch 0010; brief 99, item 2).

Runs only with FAXBOT_NATIVE_PROOF=1, like the T.38 loopback it builds on (``test_t38_loopback``): the Faxbot
Asterisk image with every patch, two containers on a private network with no internet access, each side's trunk
rendered by Faxbot itself, and the sender's call placed with Faxbot's own Originate fields.

- A receiver that takes a T.38-only first INVITE (an ordinary Faxbot endpoint with T.38 on) answers the Data Connect
  offer: the call is T.38 from its first INVITE, the offer carries T.38 Annex D's parameters and b=AS:64, the pages
  arrive unchanged, the sender's FaxResult says DataConnect connected, and the session stays inside 50 datagrams a
  second, which with patch 0010's 130-octet cap is within the 64 kbit/s it declared and is priced at.
- A receiver that refuses it (an ordinary T.38 endpoint, which like Asterisk answers a T.38-only first INVITE 488 Not
  Acceptable Here) makes the sender send a FaxDataConnect event; Faxbot's real ``AMIClient``, running in a Faxbot API container on
  the same network, calls again at once as an ordinary fax call for the same attempt, holds and drops the refused
  call's failure, and the pages arrive by the ordinary call.

Nothing here reaches NTT; NTT's detailed interface is not published.
"""
import json
import os
import re
import subprocess
import time
import uuid

import pytest

from app import ami, sip_trunk
from api.tests.test_t38_loopback import (AMI_PASSWORD, AMI_USER, CALLER, CAPTURE, DID, ROOT, SECRET, Docker,
                                         exchange, page_digests, page_heights, proof_pages, trunk_values,
                                         wait_booted)


pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_NATIVE_PROOF') != '1',
                       reason='Set FAXBOT_NATIVE_PROOF=1 to run the Data Connect loopback proof.'),
]

IMAGE = os.environ.get('FAXBOT_NATIVE_IMAGE') or 'faxbot-mny:asterisk-0010'
API_IMAGE = os.environ.get('FAXBOT_API_IMAGE') or 'faxbot-verified:main-f85d48db'


@pytest.fixture(autouse=True)
def built_image(monkeypatch):
    """The image with patch 0010, built once (``exchange`` builds only when no image is named)."""
    if subprocess.run(['docker', '--context', os.environ.get('FAXBOT_DOCKER_CONTEXT', 'colima-faxbot-refresh'),
                       'image', 'inspect', IMAGE], capture_output=True).returncode:
        subprocess.run(['docker', '--context', os.environ.get('FAXBOT_DOCKER_CONTEXT', 'colima-faxbot-refresh'),
                        'build', '--quiet', '--tag', IMAGE, str(ROOT / 'asterisk')], check=True, timeout=3600)
    monkeypatch.setenv('FAXBOT_NATIVE_IMAGE', IMAGE)


def _same_pages(sent, received):
    """The received pages are the sent ones below the header line the sending engine adds (as test_t38_loopback)."""
    sent_heights, received_heights = page_heights(sent), page_heights(received)
    header_rows = received_heights[0] - sent_heights[0] if received_heights and sent_heights else -1
    if len(sent_heights) != len(received_heights) or not 0 < header_rows <= 64:
        return False
    return ([page['body_sha256'] for page in page_digests(received, skip_rows=header_rows)]
            == [page['body_sha256'] for page in page_digests(sent)])


def _invite_sdp(log):
    """The SDP of the first INVITE the receiver logged (pjsip set logger on)."""
    start = log.find('INVITE sip:')
    assert start >= 0, 'The receiver logged no INVITE'
    end = log.find('<--- ', start + 1)
    return log[start:end if end > 0 else start + 4000]


def takes_data_connect(text):
    """The receiver's trunk endpoint as Faxbot renders an NTT Hikari Denwa trunk's: it takes Data Connect faxes."""
    assert 't38_udptl_nat=yes' in text
    return text.replace('t38_udptl_nat=yes', 't38_udptl_nat=yes\nset_var=FAXBOT_DATA_CONNECT_ACCEPT=yes', 1)


def test_a_receiver_that_takes_t38_from_the_first_invite_answers_a_data_connect_call(tmp_path, monkeypatch):
    # The receiver logs every T.38 datagram it gets from the sender, with its length, to measure the session; the
    # log is read before the containers go.
    datagrams = {}
    close = Docker.close

    def read_then_close(self):
        for container in self.containers:
            if container.endswith('-receiver'):
                datagrams['log'] = self.read(container, '/tmp/udptl.log')
        close(self)
    monkeypatch.setattr(Docker, 'close', read_then_close)
    outcome = exchange(tmp_path, extra_variables={'FAXBOT_DATA_CONNECT': 'yes'}, receiver_edit=takes_data_connect,
                       receiver_commands=('logger add channel /tmp/udptl.log verbose(3)', 'udptl set debug on'))
    result = outcome['result']
    evidence = {'result': {key: result.get(key) for key in ('Status', 'Error', 'Pages', 'Mode', 'DataConnect',
                                                            'Answered', 'Ended')},
                'image': outcome['image'], 'asterisk': outcome['asterisk']}
    print(json.dumps(evidence, indent=2))
    assert result['Status'] == 'SUCCESS' and result['Mode'] == 'T38' and result['DataConnect'] == 'connected'
    assert outcome['received'] is not None
    assert _same_pages(outcome['sent'], outcome['received'])
    # The first INVITE offered T.38 alone, with Annex D's parameters and the bandwidth Faxbot prices it at.
    sdp = _invite_sdp(outcome['receiver_log'])
    assert 'm=image ' in sdp and 'udptl t38' in sdp and 'm=audio' not in sdp
    assert 'b=AS:64' in sdp and 'a=T38FaxVersion:0' in sdp and 'a=T38MaxBitRate:14400' in sdp
    assert 'a=T38FaxRateManagement:transferredTCF' in sdp
    assert 'NTT Data Connect call answered with T.38' in outcome['sender_log']
    # The session stays inside what it declared, in every second of the call: the datagrams the receiver got from
    # the sender, each with its UDP and IPv4 headers, never more than 64 kbit/s; and none over the 160-octet cap.
    from app.routing import data_connect
    per_second, largest, count = {}, 0, 0
    for second, length in re.findall(r'\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\][^\n]*UDPTL \(\S+\): packet from '
                                     r'[\d.]+:\d+ \(seq -?\d+, len (\d+)\)', datagrams.get('log', '')):
        per_second[second] = per_second.get(second, 0) + (int(length) + data_connect.IP_UDP_OCTETS) * 8
        largest, count = max(largest, int(length)), count + 1
    assert count > 100, 'The receiver logged no T.38 datagrams'
    busiest = max(per_second.values())
    print(json.dumps({'datagrams': count, 'largest_octets': largest, 'busiest_second_bits': busiest,
                      'seconds': len(per_second)}))
    assert largest <= data_connect.DATAGRAM_LIMIT
    assert busiest <= data_connect.SESSION_KBPS * 1000
    assert 'UDPTL asked to send' not in outcome['sender_log']  # no fax data was cut to fit


CLIENT = r'''
import asyncio, json, sys, types
from app import ami
ami.settings = types.SimpleNamespace(ami_host=sys.argv[1], ami_port=5038, ami_username=sys.argv[2],
                                     ami_password=sys.argv[3])
fields = json.loads(sys.argv[4])
seen = []
client = ami.AMIClient()
for name in ('OriginateResponse', 'FaxResult', 'FaxDataConnect'):
    client._listen(name, lambda event, name=name: seen.append([name, dict(event)]))

async def main():
    await client.connect()
    for _ in range(200):
        if client._connected.is_set():
            break
        await asyncio.sleep(0.1)
    # As originate_sendfax does for a Data Connect offer: kept, then sent.
    client._data_connect_offers[fields['ActionID']] = dict(fields)
    await client._send_action(fields)
    for _ in range(2400):
        if any(name == 'FaxResult' for name, _ in seen):
            break
        await asyncio.sleep(0.1)
    sent = [text for text in getattr(client, '_proof_sent', [])]
    print(json.dumps({'seen': seen, 'retries_left': len(client._data_connect_retries)}))

asyncio.run(main())
'''


def test_a_receiver_that_refuses_it_gets_the_same_attempt_again_as_an_ordinary_fax_call(tmp_path):
    docker = Docker()
    try:
        docker.network = docker.prefix
        docker.run('network', 'create', '--internal', '--label', 'com.faxbot.scope=t38-proof', docker.network)
        capture = docker.start('api', 'python:3.11-slim', 'python', '-c', CAPTURE)
        client = docker.start('client', API_IMAGE, 'infinity', entrypoint='sleep')
        receiver = docker.start('receiver', IMAGE, 'infinity', entrypoint='sleep')
        sender = docker.start('sender', IMAGE, 'infinity', entrypoint='sleep')
        addresses = {name: docker.address(container) for name, container in
                     (('api', capture), ('client', client), ('receiver', receiver), ('sender', sender))}
        for container, peer in ((receiver, addresses['sender']), (sender, addresses['receiver'])):
            # The receiver is an ordinary T.38 fax endpoint (Faxbot's own, not an NTT trunk's): like Asterisk
            # itself, it answers a T.38-only first INVITE 488 and takes T.38 later by re-INVITE.
            text = sip_trunk.render_pjsip(trunk_values(peer))
            assert 'FAXBOT_DATA_CONNECT_ACCEPT' not in text
            rendered = tmp_path / f'{container}.conf'
            rendered.write_text(text)
            docker.run('exec', container, 'mkdir', '-p', '/faxdata/asterisk', '/faxdata/outbound')
            docker.run('cp', str(rendered), f'{container}:/faxdata/asterisk/pjsip.conf')
        logger = tmp_path / 'logger.conf'
        logger.write_text('[general]\ndateformat=%F %T\n\n[logfiles]\nconsole => notice,warning,error,verbose\n')
        docker.run('cp', str(logger), f'{receiver}:/etc/asterisk/logger.conf')
        docker.run('exec', '--detach', '--env', f'ASTERISK_INBOUND_SECRET={SECRET}',
                   '--env', f'FAXBOT_API_URL=http://{addresses["api"]}:8080', receiver,
                   'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        # The sender's manager port admits the Faxbot API container, as docker-compose.yml's address does.
        docker.run('exec', '--detach', '--env', f'ASTERISK_AMI_USERNAME={AMI_USER}',
                   '--env', f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}',
                   '--env', f'FAXBOT_API_ADDRESS={addresses["client"]}', sender,
                   'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        wait_booted(docker, receiver)
        wait_booted(docker, sender)
        docker.asterisk(receiver, 'pjsip set logger on')
        sent = tmp_path / 'proof.tiff'
        pages = proof_pages()
        pages[0].save(sent, save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
        docker.run('cp', str(sent), f'{sender}:/faxdata/outbound/proof.tiff')
        job, attempt = uuid.uuid4().hex, uuid.uuid4().hex
        values = trunk_values(addresses['receiver'], FAX_LOCAL_STATION_ID=CALLER, FAX_HEADER='Faxbot proof')
        fields = ami.originate_fields_for(values, job, DID, '/faxdata/outbound/proof.tiff', attempt_id=attempt)
        fields['Variable'] += ',' + ami.DATA_CONNECT_VARIABLE
        # Faxbot's own code, this branch's, in the API image (its dependencies), with the real AMIClient.
        docker.run('exec', client, 'mkdir', '-p', '/proof')
        docker.run('cp', str(ROOT / 'api' / 'app'), f'{client}:/proof/app')
        script = tmp_path / 'client.py'
        script.write_text(CLIENT)
        docker.run('cp', str(script), f'{client}:/proof/client.py')
        run = docker.run('exec', '--workdir', '/proof', '--env', 'PYTHONPATH=/proof', client, 'python',
                         '/proof/client.py', addresses['sender'], AMI_USER, AMI_PASSWORD, json.dumps(fields),
                         check=False, timeout=400)
        assert run.returncode == 0, run.stderr[-3000:]
        report = json.loads(run.stdout.strip().splitlines()[-1])
        seen = report['seen']
        names = [name for name, _ in seen]
        print(json.dumps({'events': seen, 'stderr_tail': run.stderr[-800:]}, indent=2))
        refused = next(event for name, event in seen if name == 'FaxDataConnect')
        assert names.index('FaxDataConnect') < names.index('FaxResult')
        assert refused['Outcome'] == 'refused' and refused['Code'] == '488'
        assert refused['JobID'] == job and refused['AttemptID'] == attempt
        result = next(event for name, event in seen if name == 'FaxResult')
        assert result['Status'] == 'SUCCESS' and result['AttemptID'] == attempt and result.get('DataConnect', '') == ''
        assert result['Mode'] == 'T38'  # the ordinary call, moved to T.38 by re-INVITE as every trunk call is
        # The refused call's own failure never reached Faxbot's listeners: the attempt went on as an ordinary call.
        assert not any(name == 'OriginateResponse' and event.get('Response') == 'Failure' for name, event in seen)
        assert report['retries_left'] == 0
        sender_log = docker.read(sender, '/tmp/asterisk.log')
        assert 'the NTT Data Connect offer was refused (488' in sender_log
        receiver_log = docker.read(receiver, '/tmp/asterisk.log')
        assert '488 Not Acceptable Here' in receiver_log
        deadline = time.monotonic() + 60
        captured = None
        while time.monotonic() < deadline and captured is None:
            probe = docker.read(capture, '/tmp/capture.json')
            captured = json.loads(probe) if probe.strip() else None
            if captured is None:
                time.sleep(1)
        assert captured is not None, 'The receiver stored no fax'
        received = tmp_path / 'received.tiff'
        docker.run('cp', f'{receiver}:{captured["body"]["tiff_path"]}', str(received))
        assert _same_pages(sent, received)
    finally:
        docker.close()
