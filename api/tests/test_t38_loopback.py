"""Two Faxbot Asterisk containers exchange a real two-page fax over T.38.

Runs only with FAXBOT_NATIVE_PROOF=1 (``make native-proof``). It builds the
Faxbot Asterisk image, joins a sender and a receiver on a private Docker
network with no internet access, renders each side's trunk with
``app.sip_trunk`` (custom preset, IP authentication, the peer as the carrier),
starts both through the image's own start script, submits the call with the
production AMI Originate fields (fax preference on), and lets the receiver's
own inbound dialplan notify a capture service in place of the Faxbot API.

It asserts T.38 was negotiated on both sides, two pages arrived, the RFC 6913
Accept-Contact header reached the receiver exactly, and every received page
image equals the page that was sent. Evidence is printed as JSON.

The media tests make one side advertise a media address nothing on the proof
network can reach, the way Asterisk behind a router names an address the
carrier cannot send to, and let the other side stand in for the carrier. A
carrier that never sends media anywhere but the advertised address leaves the
call without fax data, and Faxbot must say so in one plain sentence.

The NAT matrix puts Faxbot behind a router container (iptables MASQUERADE,
optionally --random so ports change; the router image is built from Debian
with iptables and needs internet access to build). Faxbot registers through it
to a stand-in carrier that is also a registrar, and faxes go both ways over the
registered flow.

Sending together: two faxes waiting for one number are claimed as one call
through the real delivery store, their combined image (separator pages and
each fax's own pages) is sent over T.38, and the sender's real FaxResult is
applied to both faxes. A second call is cut part-way to record what the
sender's confirmed page count (FAXPAGES) was and which fax it delivered.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

import pytest

from app import ami, sip_calls, sip_trunk
from app.config_values import ConfigurationValues


pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_NATIVE_PROOF') != '1',
                       reason='Set FAXBOT_NATIVE_PROOF=1 to run the T.38 loopback proof.'),
]

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = os.environ.get('FAXBOT_DOCKER_CONTEXT', 'colima-faxbot-refresh')
DID = '+15555550199'
CALLER = '+15555550100'
SECRET = 'proof-' + uuid.uuid4().hex
AMI_USER, AMI_PASSWORD = 'proof_ami', 'Proof-' + uuid.uuid4().hex
# TEST-NET-1 (RFC 5737): nothing on the proof network answers this address.
UNREACHABLE = '192.0.2.10'

CAPTURE = r'''
import http.server, json
class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        record = {"path": self.path, "secret": self.headers.get("X-Internal-Secret"),
                  "body": json.loads(body)}
        with open("/tmp/capture.json", "w") as handle:
            json.dump(record, handle)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"id":"proof","status":"ok"}')
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
'''

AMI_SESSION = r'''
exec 3<>/dev/tcp/127.0.0.1/5038
cat >&3
deadline=$((SECONDS + 240))
seen=''
while [ "$SECONDS" -lt "$deadline" ]; do
  if IFS= read -r -t 5 line <&3; then
    printf '%s\n' "$line"
    case $line in
      *'UserEvent: FaxResult'*) seen=1 ;;
    esac
    if [ -n "$seen" ] && [ -z "${line%$'\r'}" ]; then exit 0; fi
  fi
done
exit 3
'''

# The same, waiting also for patch 0004's FaxFrames event, which follows FaxResult in the same hang-up handler.
AMI_SESSION_FRAMES = AMI_SESSION.replace("*'UserEvent: FaxResult'*) seen=1 ;;", "*'UserEvent: FaxFrames'*) seen=1 ;;")

# The receiving side's manager listener, as Faxbot's own connection would hear
# it: logs in from the container environment and keeps every user event.
AMI_LISTENER = r'''
exec 3<>/dev/tcp/127.0.0.1/5038
printf 'Action: Login\r\nUsername: %s\r\nSecret: %s\r\nEvents: user\r\n\r\n' \
  "$ASTERISK_AMI_USERNAME" "$ASTERISK_AMI_PASSWORD" >&3
deadline=$((SECONDS + 300))
while [ "$SECONDS" -lt "$deadline" ]; do
  if IFS= read -r -t 5 line <&3; then
    printf '%s\n' "$line" >> /tmp/ami-events.log
  fi
done
'''


class Docker:
    def __init__(self):
        self.base = ['docker', '--context', CONTEXT]
        self.prefix = 'faxbot-t38-proof-' + uuid.uuid4().hex[:10]
        self.containers = []
        self.network = None

    def run(self, *args, check=True, input_text=None, timeout=600):
        result = subprocess.run(self.base + list(args), input=input_text, capture_output=True,
                                text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f'docker {args[0]} failed: {result.stderr[-2000:]}')
        return result

    def start(self, name, image, *command, entrypoint=None):
        container = f'{self.prefix}-{name}'
        args = ['run', '--detach', '--name', container, '--network', self.network,
                '--label', 'com.faxbot.scope=t38-proof']
        if entrypoint:
            args += ['--entrypoint', entrypoint]
        self.run(*args, image, *command)
        self.containers.append(container)
        return container

    def address(self, container):
        output = self.run('inspect', '--format',
                          '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', container).stdout
        return output.strip()

    def asterisk(self, container, command):
        return self.run('exec', container, 'asterisk', '-rx', command, check=False).stdout

    def read(self, container, path):
        result = self.run('exec', container, 'cat', path, check=False)
        return result.stdout if result.returncode == 0 else ''

    def close(self):
        for container in reversed(self.containers):
            self.run('rm', '--force', '--volumes', container, check=False)
        if self.network:
            self.run('network', 'rm', self.network, check=False)


def proof_pages():
    """Two distinct 1728-pixel-wide bilevel pages at fax fine resolution."""
    from PIL import Image, ImageDraw
    pages = []
    for number in (1, 2):
        page = Image.new('1', (1728, 900), 1)
        draw = ImageDraw.Draw(page)
        draw.rectangle((40, 40, 1688, 860), outline=0, width=6)
        for row in range(number * 3):
            top = 120 + row * 90
            draw.rectangle((120 + number * 40, top, 1600 - row * 70, top + 40), fill=0)
        for column in range(0, 1728, 96 // number):
            draw.line((column, 760, column + 48, 840), fill=0, width=3)
        draw.text((120, 60), f'Faxbot T.38 loopback proof, page {number} of 2', fill=0)
        pages.append(page)
    return pages


def page_digests(path, *, skip_rows=0):
    """Per-page bitmap digests; ``skip_rows`` drops the fax header line the sender adds."""
    from PIL import Image, ImageSequence
    result = []
    for frame in ImageSequence.Iterator(Image.open(path)):
        bitmap = frame.convert('1')
        body = bitmap.crop((0, skip_rows, bitmap.size[0], bitmap.size[1]))
        header = bitmap.crop((0, 0, bitmap.size[0], skip_rows)) if skip_rows else None
        result.append({'size': list(bitmap.size),
                       'dpi': [round(float(value)) for value in frame.info.get('dpi', (0, 0))],
                       'compression': frame.info.get('compression'),
                       'header_rows': skip_rows,
                       'header_black_pixels': (sum(1 for value in header.get_flattened_data() if not value)
                                               if header else 0),
                       'body_sha256': hashlib.sha256(body.tobytes()).hexdigest()})
    return result


def page_heights(path):
    from PIL import Image, ImageSequence
    return [frame.size[1] for frame in ImageSequence.Iterator(Image.open(path))]


def parse_ami(text):
    events, block = [], {}
    for line in text.splitlines():
        line = line.rstrip('\r')
        if not line:
            if block:
                events.append(block)
                block = {}
            continue
        if ':' in line:
            key, value = line.split(':', 1)
            block[key.strip()] = value.strip()
    if block:
        events.append(block)
    return events


def trunk_values(peer_address, **extra):
    return ConfigurationValues.from_environment({
        'SIP_TRUNK_PRESET': 'custom', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': peer_address,
        'SIP_TRUNK_CALLER_ID': CALLER, 'SIP_TRUNK_DIDS': DID, **extra})


# Several trunks (provider-rules design §3.6): the first trunk points at an address nothing answers, and the
# second trunk, account key ``sip-b``, is the other container standing in for carrier B.
SECOND = 'sip-b'


def two_trunk_values(peer_address, **extra):
    from app.config_profiles import ConfigurationDocument
    first = trunk_values(UNREACHABLE, SIP_TRUNK_DIDS='+15555550198', **extra)
    return first.with_provider_accounts(ConfigurationDocument({SECOND: {
        'provider': 'sip', 'label': 'Carrier B', 'receives': True, 'numbers': [DID],
        'settings': {'preset': 'custom', 'auth': 'ip', 'host': peer_address, 'caller_id': CALLER}}}))


def wait_booted(docker, container):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if 'Asterisk 22' in docker.asterisk(container, 'core show version'):
            if docker.run('exec', container, 'asterisk', '-rx', 'core waitfullybooted', check=False).returncode == 0:
                return
        time.sleep(0.5)
    raise RuntimeError('Asterisk did not start')


def advertise_unreachable_media(text):
    """Faxbot's side names a media address the other side cannot reach.

    Without local_net, Asterisk puts external_media_address into every SDP it
    sends (audio and T.38); signaling keeps the container's real address.
    """
    lines = text.splitlines()
    index = next(number for number, line in enumerate(lines) if line.startswith('bind='))
    lines.insert(index + 1, f'external_media_address={UNREACHABLE}')
    return '\n'.join(lines)


def never_latch(text):
    """The carrier's side only ever sends media to the address in the SDP."""
    assert 'rtp_symmetric=yes' in text and 't38_udptl_nat=yes' in text
    return text.replace('rtp_symmetric=yes', 'rtp_symmetric=no').replace('t38_udptl_nat=yes', 't38_udptl_nat=no')


def exchange(tmp_path, *, sender_edit=None, receiver_edit=None, receiver_events=False, wait_for_receiver=60,
             fax_image=None, identity=None, hang_up_after=None, extra_variables=None, receiver_db=None,
             wait_frames=False, two_trunks=False):
    """Send the two proof pages from one container to the other and collect what each side saw.

    ``fax_image`` sends that fax image instead, under ``identity`` (job, attempt);
    ``hang_up_after`` makes the sender hang up that many seconds after the call is placed.
    ``extra_variables`` ({name: value}) go on the sent call as Faxbot sets them (patch 0004: FAXBOT_T38_NOW,
    FAXBOT_IAF); ``receiver_db`` ({family/key: value}) goes into the receiver's Asterisk database first;
    ``wait_frames`` also waits for the sender's FaxFrames event. ``two_trunks`` gives both sides two trunks, the
    other container being the second (``two_trunk_values``), and sends over that second trunk.
    """
    docker = Docker()
    image = os.environ.get('FAXBOT_NATIVE_IMAGE') or 'faxbot-native:t38-proof'
    if not os.environ.get('FAXBOT_NATIVE_IMAGE'):
        docker.run('build', '--quiet', '--tag', image, str(ROOT / 'asterisk'), timeout=3600)
    try:
        docker.network = docker.prefix
        docker.run('network', 'create', '--internal', '--label', 'com.faxbot.scope=t38-proof', docker.network)
        capture = docker.start('api', 'python:3.11-slim', 'python', '-c', CAPTURE)
        receiver = docker.start('receiver', image, 'infinity', entrypoint='sleep')
        sender = docker.start('sender', image, 'infinity', entrypoint='sleep')
        addresses = {name: docker.address(container)
                     for name, container in (('api', capture), ('receiver', receiver), ('sender', sender))}
        assert all(addresses.values())

        # Each side's trunk is the other container, rendered by Faxbot itself.
        for container, peer, edit in ((receiver, addresses['sender'], receiver_edit),
                                      (sender, addresses['receiver'], sender_edit)):
            text = sip_trunk.render_pjsip((two_trunk_values if two_trunks else trunk_values)(peer))
            rendered = tmp_path / f'{container}.conf'
            rendered.write_text(edit(text) if edit else text)
            docker.run('exec', container, 'mkdir', '-p', '/faxdata/asterisk', '/faxdata/outbound')
            docker.run('cp', str(rendered), f'{container}:/faxdata/asterisk/pjsip.conf')
        # Proof only: print the receiver's SIP messages so the INVITE headers and
        # the T.38 re-INVITE SDP can be checked from its container log.
        logger = tmp_path / 'logger.conf'
        logger.write_text('[general]\ndateformat=%F %T\n\n[logfiles]\nconsole => notice,warning,error,verbose\n')
        docker.run('cp', str(logger), f'{receiver}:/etc/asterisk/logger.conf')
        # The image's own start script, with Asterisk output kept in the container.
        receiver_environment = ['--env', f'ASTERISK_INBOUND_SECRET={SECRET}',
                                '--env', f'FAXBOT_API_URL=http://{addresses["api"]}:8080']
        if receiver_events:
            receiver_environment += ['--env', f'ASTERISK_AMI_USERNAME={AMI_USER}',
                                     '--env', f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}']
        docker.run('exec', '--detach', *receiver_environment, receiver, 'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        docker.run('exec', '--detach', '--env', f'ASTERISK_AMI_USERNAME={AMI_USER}',
                   '--env', f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}', sender,
                   'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        wait_booted(docker, receiver)
        wait_booted(docker, sender)
        docker.asterisk(receiver, 'pjsip set logger on')
        if receiver_events:
            docker.run('exec', '--detach', '--env', f'ASTERISK_AMI_USERNAME={AMI_USER}',
                       '--env', f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}', receiver, 'bash', '-c', AMI_LISTENER)
            deadline = time.monotonic() + 20
            while 'Authentication accepted' not in docker.read(receiver, '/tmp/ami-events.log'):
                assert time.monotonic() < deadline, 'The receiver manager listener did not log in'
                time.sleep(0.5)

        sent = tmp_path / 'proof.tiff'
        if fax_image is None:
            pages = proof_pages()
            pages[0].save(sent, save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
        else:
            sent.write_bytes(Path(fax_image).read_bytes())
        docker.run('cp', str(sent), f'{sender}:/faxdata/outbound/proof.tiff')

        job, attempt = identity or (uuid.uuid4().hex, uuid.uuid4().hex)
        values = (two_trunk_values if two_trunks else trunk_values)(
            addresses['receiver'], SIP_FAX_PREFERENCE_HEADER='true', FAX_LOCAL_STATION_ID='+15555550100',
            FAX_HEADER='Faxbot proof')
        fields = ami.originate_fields_for(values, job, DID, '/faxdata/outbound/proof.tiff', attempt_id=attempt,
                                          trunk=SECOND if two_trunks else None)
        if extra_variables:
            fields['Variable'] += ''.join(f',{name}={value}' for name, value in extra_variables.items())
        for name, value in (receiver_db or {}).items():
            family, key = name.split('/', 1)
            docker.asterisk(receiver, f'database put {family} {key} {value}')
        actions = (f'Action: Login\r\nActionID: proof-login\r\nUsername: {AMI_USER}\r\n'
                   f'Secret: {AMI_PASSWORD}\r\nEvents: call,user\r\n\r\n'
                   + ''.join(f'{key}: {value}\r\n' for key, value in fields.items()) + '\r\n')
        submitted = time.time()
        if hang_up_after is not None:
            # The sender ends the call part-way, the way a dropped line would.
            docker.run('exec', '--detach', sender, 'sh', '-c',
                       f'sleep {hang_up_after}; asterisk -rx "channel request hangup all"')
        session = docker.run('exec', '--interactive', sender, 'bash', '-c',
                             AMI_SESSION_FRAMES if wait_frames else AMI_SESSION,
                             input_text=actions, check=False, timeout=300)
        finished = time.time()
        events = parse_ami(session.stdout)
        result = next((event for event in events if event.get('UserEvent') == 'FaxResult'), None)
        frames = next((event for event in events if event.get('UserEvent') == 'FaxFrames'), None)
        originate = next((event for event in events if event.get('Event') == 'OriginateResponse'), {})
        assert result is not None, 'No fax result: ' + json.dumps(events[-6:])

        # The receiver reports a stored image over HTTP; a call that left no
        # image is reported as a manager event instead.
        deadline = time.monotonic() + wait_for_receiver
        captured = inbound_event = None
        while time.monotonic() < deadline and captured is None and inbound_event is None:
            probe = docker.read(capture, '/tmp/capture.json')
            if probe.strip():
                captured = json.loads(probe)
            elif receiver_events:
                inbound_event = next((event for event in parse_ami(docker.read(receiver, '/tmp/ami-events.log'))
                                      if event.get('UserEvent') == 'FaxInboundCall'), None)
            if captured is None and inbound_event is None:
                time.sleep(1)
        received = None
        if captured is not None:
            received = tmp_path / 'received.tiff'
            docker.run('cp', f'{receiver}:{captured["body"]["tiff_path"]}', str(received))
        return {
            'image': json.loads(docker.run('image', 'inspect', image).stdout)[0]['Id'],
            'asterisk': docker.asterisk(sender, 'core show version').strip(),
            'spandsp': docker.run('exec', sender, 'dpkg-query', '-W', '-f', '${Version}', 'libspandsp2t64').stdout,
            'fax_capabilities': docker.asterisk(sender, 'fax show capabilities').strip().splitlines()[-2:],
            'result': result, 'frames': frames, 'originate': originate, 'captured': captured,
            'inbound_event': inbound_event, 'sender_log': docker.read(sender, '/tmp/asterisk.log'),
            'sent': sent, 'received': received,
            'receiver_log': docker.read(receiver, '/tmp/asterisk.log'),
            'submit_to_result_seconds': round(finished - submitted, 1),
            'fields': fields,
            'sender_endpoints': docker.asterisk(sender, 'pjsip show endpoints'),
            'receiver_endpoints': docker.asterisk(receiver, 'pjsip show endpoints'),
        }
    finally:
        docker.close()


def media_evidence(outcome):
    """What each side saw, without page images; printed for the record."""
    result = outcome['result']
    return {
        'sender_result': {key: result.get(key) for key in ('Status', 'Error', 'Pages', 'Mode', 'Cause', 'RtpTx', 'RtpRx')},
        'sender_error': base64.b64decode(result.get('Error64') or '').decode('utf-8', 'replace'),
        'sender_remote_station': base64.b64decode(result.get('Station64') or '').decode('utf-8', 'replace'),
        'sender_connected_seconds': (int(result['Ended']) - int(result['Answered'])
                                     if result.get('Answered') and result.get('Ended') else None),
        'sender_verdict': sip_calls.verdict(result),
        'sender_summary': sip_calls.result_summary(result),
        'receiver_report': ({key: outcome['captured']['body'].get(key) for key in ('faxstatus', 'faxpages')}
                            if outcome['captured'] else None),
        'receiver_event': ({key: outcome['inbound_event'].get(key)
                            for key in ('Status', 'Pages', 'Mode', 'Cause', 'RtpTx', 'RtpRx', 'Answered', 'Ended')}
                           if outcome['inbound_event'] else None),
        'receiver_error': (base64.b64decode(outcome['inbound_event'].get('Error64') or '').decode('utf-8', 'replace')
                           if outcome['inbound_event'] else None),
        'receiver_verdict': sip_calls.verdict(outcome['inbound_event']) if outcome['inbound_event'] else None,
        'receiver_summary': (sip_calls.inbound_summary(outcome['inbound_event'])
                             if outcome['inbound_event'] else None),
        't38_image_offer_in_sdp': 'm=image' in outcome['receiver_log'],
        'unreachable_address_in_sdp': f'c=IN IP4 {UNREACHABLE}' in outcome['receiver_log'],
        'submit_to_result_seconds': outcome['submit_to_result_seconds'],
    }


def test_two_asterisk_containers_exchange_a_two_page_fax_over_t38(tmp_path):
    outcome = exchange(tmp_path)
    result, originate, captured = outcome['result'], outcome['originate'], outcome['captured']
    assert captured is not None, 'Receiver did not report the fax'
    body, sent, received, log_text = captured['body'], outcome['sent'], outcome['received'], outcome['receiver_log']

    artifacts = os.environ.get('FAXBOT_PROOF_ARTIFACTS')
    if artifacts:
        keep = Path(artifacts)
        keep.mkdir(parents=True, exist_ok=True)
        (keep / 'receiver.log').write_text(log_text)
        (keep / 'sent.tiff').write_bytes(sent.read_bytes())
        (keep / 'received.tiff').write_bytes(received.read_bytes())
    invite = re.search(r'INVITE sip:[^\n]*\n(?:[^\n]+\n)*?Accept-Contact: ([^\r\n]*)', log_text)
    image_offer = 'm=image' in log_text and 'udptl t38' in log_text.lower()

    # The sending engine adds one header line (FAXOPT headerinfo) above each
    # page; the image below it must be exactly what was sent.
    sent_heights, received_heights = page_heights(sent), page_heights(received)
    header_rows = received_heights[0] - sent_heights[0] if received_heights and sent_heights else -1
    sent_digests = page_digests(sent)
    received_digests = page_digests(received, skip_rows=max(header_rows, 0))
    evidence = {
        'image': outcome['image'],
        'asterisk': outcome['asterisk'],
        'spandsp': outcome['spandsp'],
        'fax_capabilities': outcome['fax_capabilities'],
        'originate_response': {key: originate.get(key) for key in ('Response', 'Reason')},
        'sender_result': {key: result.get(key) for key in ('Status', 'Error', 'Pages', 'Mode', 'Cause')},
        'sender_remote_station': base64.b64decode(result.get('Station64') or '').decode('utf-8', 'replace'),
        'connected_seconds': int(result['Ended']) - int(result['Answered']),
        'submit_to_result_seconds': outcome['submit_to_result_seconds'],
        'receiver_report': {key: body.get(key) for key in ('to_number', 'from_number', 'faxstatus', 'faxpages')},
        'receiver_call': body.get('call'),
        'secret_matched': captured['secret'] == SECRET,
        'accept_contact_seen_by_receiver': invite.group(1) if invite else None,
        't38_image_offer_in_sdp': image_offer,
        't38_sdp_attributes': sorted(set(re.findall(r'a=(T38[A-Za-z]+(?::[^\r\n]*)?)', log_text))),
        'sent_pages': sent_digests,
        'received_pages': received_digests,
    }
    print('\nT38_PROOF_EVIDENCE ' + json.dumps(evidence, indent=2))

    assert originate.get('Response') == 'Success'
    assert result['Status'] == 'SUCCESS' and result['Pages'] == '2'
    assert result['Mode'] == 'T38'
    assert body['call']['t38'] is True and body['faxpages'] == 2 and body['call']['pages'] == 2
    assert body['to_number'] == DID and body['from_number'] == CALLER
    assert captured['secret'] == SECRET and captured['path'] == '/_internal/asterisk/inbound'
    assert evidence['accept_contact_seen_by_receiver'] == '*;+sip.fax="t38"'
    assert image_offer
    assert 0 <= evidence['connected_seconds'] <= 240
    assert len(received_heights) == len(sent_heights) == 2
    assert 0 < header_rows <= 64
    assert all(received - sent_height == header_rows
               for received, sent_height in zip(received_heights, sent_heights))
    assert all(page['header_black_pixels'] > 0 for page in received_digests)
    assert [page['body_sha256'] for page in received_digests] == [page['body_sha256'] for page in sent_digests]


def test_a_carrier_that_never_latches_leaves_a_sent_fax_without_fax_data_and_faxbot_says_so(tmp_path):
    outcome = exchange(tmp_path, sender_edit=advertise_unreachable_media, receiver_edit=never_latch)
    evidence = media_evidence(outcome)
    print('\nNO_MEDIA_SENT_EVIDENCE ' + json.dumps(evidence, indent=2))
    assert evidence['unreachable_address_in_sdp']
    assert outcome['originate'].get('Response') == 'Success'
    assert outcome['result']['Status'] != 'SUCCESS' and outcome['result']['Pages'] == '0'
    assert evidence['sender_verdict'] == 'no_t38_data_back'
    assert evidence['sender_summary'] == sip_calls.NO_FAX_DATA


def test_a_carrier_that_never_latches_leaves_a_received_call_without_fax_data_and_faxbot_says_so(tmp_path):
    outcome = exchange(tmp_path, sender_edit=never_latch, receiver_edit=advertise_unreachable_media,
                       receiver_events=True, wait_for_receiver=120)
    evidence = media_evidence(outcome)
    print('\nNO_MEDIA_RECEIVED_EVIDENCE ' + json.dumps(evidence, indent=2))
    assert evidence['unreachable_address_in_sdp']
    assert outcome['captured'] is None and outcome['inbound_event'] is not None
    assert evidence['receiver_verdict'] == 'no_t38_data_back'
    assert evidence['receiver_summary'] == 'A fax call from +15555550100 came in, but no fax data arrived from the carrier.'


def test_a_latching_carrier_completes_a_sent_fax_when_faxbot_advertises_an_unreachable_address(tmp_path):
    outcome = exchange(tmp_path, sender_edit=advertise_unreachable_media)
    evidence = media_evidence(outcome)
    print('\nLATCHING_SENT_EVIDENCE ' + json.dumps(evidence, indent=2))
    assert evidence['unreachable_address_in_sdp']
    assert outcome['result']['Status'] == 'SUCCESS' and outcome['result']['Pages'] == '2'
    assert outcome['captured'] is not None and outcome['captured']['body']['faxpages'] == 2


def test_a_latching_carrier_delivers_a_received_fax_when_faxbot_advertises_an_unreachable_address(tmp_path):
    outcome = exchange(tmp_path, receiver_edit=advertise_unreachable_media)
    evidence = media_evidence(outcome)
    print('\nLATCHING_RECEIVED_EVIDENCE ' + json.dumps(evidence, indent=2))
    assert evidence['unreachable_address_in_sdp']
    assert outcome['captured'] is not None and outcome['captured']['body']['faxpages'] == 2
    assert outcome['result']['Status'] == 'SUCCESS'


# NAT matrix (06-nat-design.md section 7B) -----------------------------------------
#
# Faxbot's Asterisk sits on a private network behind a router container that
# masquerades onto a second network where a stand-in carrier runs. Nothing on
# the carrier side can reach Faxbot except through flows Faxbot started: the
# registration, and the media Faxbot sends first. The carrier is a registrar,
# so incoming calls must come back over the registered flow.

ROUTER_DOCKERFILE = '''FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a
RUN apt-get update && apt-get install -y --no-install-recommends iptables iproute2 conntrack && rm -rf /var/lib/apt/lists/*
'''
ROUTER_IMAGE = 'faxbot-native:nat-router'
TRUNK_USER, TRUNK_PASSWORD = 'faxbotuser', 'Trunk-' + uuid.uuid4().hex


def carrier_conf(router_wan, *, latches):
    """A registrar standing in for the carrier: Faxbot registers to it, and its calls go to that registration."""
    nat = 'yes' if latches else 'no'
    return '\n'.join([
        '[global]', 'type=global', '',
        '[transport-udp]', 'type=transport', 'protocol=udp', 'bind=0.0.0.0:5060', '',
        '[transport-tcp]', 'type=transport', 'protocol=tcp', 'bind=0.0.0.0:5060', '',
        '[trunk-auth]', 'type=auth', 'auth_type=userpass', f'username={TRUNK_USER}', f'password={TRUNK_PASSWORD}', '',
        # The AOR is named after the registering user, as a registrar finds it from the To header.
        f'[{TRUNK_USER}]', 'type=aor', 'max_contacts=1', 'remove_existing=yes', '',
        '[trunk-endpoint]', 'type=endpoint', f'aors={TRUNK_USER}', 'auth=trunk-auth', 'context=faxbot-inbound',
        'disallow=all', 'allow=ulaw,alaw', 't38_udptl=yes', 't38_udptl_ec=redundancy',
        't38_udptl_maxdatagram=400', f't38_udptl_nat={nat}', f'rtp_symmetric={nat}', 'force_rport=yes',
        'rewrite_contact=yes', 'direct_media=no', '',
        '[trunk-identify]', 'type=identify', 'endpoint=trunk-endpoint', f'match={router_wan}', ''])


def _interface_for(docker, container, address):
    for line in docker.run('exec', container, 'ip', '-o', '-4', 'addr', 'show').stdout.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[3].split('/')[0] == address:
            return parts[1]
    raise RuntimeError('Router interface not found')


def _wait_for(check, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(1)
    raise AssertionError(message)


def _ami_originate(docker, container, fields):
    actions = (f'Action: Login\r\nActionID: proof-login\r\nUsername: {AMI_USER}\r\n'
               f'Secret: {AMI_PASSWORD}\r\nEvents: call,user\r\n\r\n'
               + ''.join(f'{key}: {value}\r\n' for key, value in fields.items()) + '\r\n')
    session = docker.run('exec', '--interactive', container, 'bash', '-c', AMI_SESSION,
                         input_text=actions, check=False, timeout=300)
    events = parse_ami(session.stdout)
    return next((event for event in events if event.get('UserEvent') == 'FaxResult'), None)


def nat_exchange(tmp_path, *, transport, random_ports, latches, exact_address=False, idle_seconds=0):
    """Register through the router, then fax both ways; returns what each side saw."""
    docker = Docker()
    image = os.environ.get('FAXBOT_NATIVE_IMAGE') or 'faxbot-native:t38-proof'
    if not os.environ.get('FAXBOT_NATIVE_IMAGE'):
        docker.run('build', '--quiet', '--tag', image, str(ROOT / 'asterisk'), timeout=3600)
    docker.run('build', '--quiet', '--tag', ROUTER_IMAGE, '-', input_text=ROUTER_DOCKERFILE, timeout=1200)
    wan = docker.prefix + '-wan'
    try:
        docker.network = docker.prefix
        for network in (docker.network, wan):
            docker.run('network', 'create', '--internal', '--label', 'com.faxbot.scope=t38-proof', network)
        # The router and Faxbot's side manage routes; nothing else gets extra privileges.
        router = f'{docker.prefix}-router'
        docker.run('run', '--detach', '--name', router, '--network', docker.network, '--cap-add', 'NET_ADMIN',
                   '--sysctl', 'net.ipv4.ip_forward=1', '--label', 'com.faxbot.scope=t38-proof',
                   ROUTER_IMAGE, 'sleep', 'infinity')
        docker.containers.append(router)
        docker.run('network', 'connect', wan, router)
        faxbot = f'{docker.prefix}-faxbot'
        docker.run('run', '--detach', '--name', faxbot, '--network', docker.network, '--cap-add', 'NET_ADMIN',
                   '--label', 'com.faxbot.scope=t38-proof', '--entrypoint', 'sleep', image, 'infinity')
        docker.containers.append(faxbot)
        carrier = f'{docker.prefix}-carrier'
        docker.run('run', '--detach', '--name', carrier, '--network', wan, '--label', 'com.faxbot.scope=t38-proof',
                   '--entrypoint', 'sleep', image, 'infinity')
        docker.containers.append(carrier)
        capture = docker.start('api', 'python:3.11-slim', 'python', '-c', CAPTURE)
        docker.run('network', 'connect', wan, capture)

        def on(container, network):
            return docker.run('inspect', '--format',
                              '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}', container).stdout.strip()
        router_lan, router_wan = on(router, docker.network), on(router, wan)
        carrier_wan, capture_lan, capture_wan = on(carrier, wan), on(capture, docker.network), on(capture, wan)
        wan_interface = _interface_for(docker, router, router_wan)
        masquerade = ['iptables', '-t', 'nat', '-A', 'POSTROUTING', '-o', wan_interface, '-j', 'MASQUERADE']
        docker.run('exec', router, *(masquerade + (['--random'] if random_ports else [])))
        docker.run('exec', faxbot, 'ip', 'route', 'replace', 'default', 'via', router_lan)
        faxbot_net = docker.run('exec', faxbot, 'ip', '-o', '-4', 'route', 'show', 'scope', 'link').stdout.split()[0]

        # Faxbot's side: the trunk exactly as Faxbot renders it, registering with username and password.
        values = trunk_values(carrier_wan, SIP_TRUNK_AUTH='registration', SIP_TRUNK_TRANSPORT=transport,
                              SIP_TRUNK_USERNAME=TRUNK_USER, SIP_TRUNK_PASSWORD=TRUNK_PASSWORD)
        rendered = tmp_path / 'faxbot.conf'
        rendered.write_text(sip_trunk.render_pjsip(values))
        docker.run('exec', faxbot, 'mkdir', '-p', '/faxdata/asterisk', '/faxdata/outbound')
        docker.run('cp', str(rendered), f'{faxbot}:/faxdata/asterisk/pjsip.conf')
        if exact_address:
            # What Faxbot's STUN probe records on a network that keeps port numbers: the router's address.
            record = tmp_path / 'public-address'
            record.write_text(json.dumps({'ip': router_wan, 'ports_preserved': True, 'probed_at': 1}) + '\n')
            docker.run('cp', str(record), f'{faxbot}:/faxdata/asterisk/public-address')
        carrier_file = tmp_path / 'carrier.conf'
        carrier_file.write_text(carrier_conf(router_wan, latches=latches))
        docker.run('exec', carrier, 'mkdir', '-p', '/faxdata/asterisk', '/faxdata/outbound')
        docker.run('cp', str(carrier_file), f'{carrier}:/faxdata/asterisk/pjsip.conf')

        for container, api in ((faxbot, capture_lan), (carrier, capture_wan)):
            docker.run('exec', '--detach', '--env', f'ASTERISK_INBOUND_SECRET={SECRET}',
                       '--env', f'FAXBOT_API_URL=http://{api}:8080', '--env', f'ASTERISK_AMI_USERNAME={AMI_USER}',
                       '--env', f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}', container,
                       'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        wait_booted(docker, faxbot)
        wait_booted(docker, carrier)
        docker.asterisk(carrier, 'pjsip set logger on')
        registered = _wait_for(lambda: 'Registered' in docker.asterisk(faxbot, 'pjsip show registrations'),
                               60, 'Faxbot did not register through the router')
        contact = docker.asterisk(carrier, 'pjsip show contacts')
        if idle_seconds:
            time.sleep(idle_seconds)

        sent = tmp_path / 'proof.tiff'
        pages = proof_pages()
        pages[0].save(sent, save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
        results = {}
        for direction, sender, receiver_api, peer in (('sent', faxbot, None, carrier_wan),
                                                      ('received', carrier, None, router_wan)):
            docker.run('cp', str(sent), f'{sender}:/faxdata/outbound/proof.tiff')
            docker.run('exec', capture, 'rm', '-f', '/tmp/capture.json', check=False)
            fields = ami.originate_fields_for(trunk_values(peer, FAX_HEADER='Faxbot NAT proof'),
                                              uuid.uuid4().hex, DID, '/faxdata/outbound/proof.tiff',
                                              attempt_id=uuid.uuid4().hex)
            result = _ami_originate(docker, sender, fields)
            deadline = time.monotonic() + 30
            captured = None
            while time.monotonic() < deadline and captured is None:
                probe = docker.read(capture, '/tmp/capture.json')
                captured = json.loads(probe) if probe.strip() else None
                if captured is None:
                    time.sleep(1)
            results[direction] = {'result': result, 'captured': captured}
        carrier_log = docker.read(carrier, '/tmp/asterisk.log')
        # The router's media flows: original source port versus the port it used on the outside.
        listing = docker.run('exec', router, 'conntrack', '-L', '-p', 'udp', check=False).stdout
        conntrack = [' '.join(line.split()) for line in listing.splitlines() if re.search(r'port=4[0-9]{3}\b', line)]
        return {
            'registered': bool(registered), 'contact': contact, 'faxbot_net': faxbot_net,
            'router_wan': router_wan, 'results': results, 'carrier_log': carrier_log, 'conntrack': conntrack,
            'faxbot_sdp_addresses': sorted(set(re.findall(r'c=IN IP4 ([0-9.]+)', carrier_log))),
            'faxbot_transport_line': docker.run('exec', faxbot, 'grep', '-E', 'external_|local_net',
                                                '/etc/asterisk/pjsip.conf', check=False).stdout.strip(),
        }
    finally:
        docker.close()
        docker.run('network', 'rm', wan, check=False)


def nat_evidence(outcome):
    summary = {'registered': outcome['registered'], 'router_wan': outcome['router_wan'],
               'faxbot_transport_line': outcome['faxbot_transport_line'],
               'sdp_addresses_seen_by_carrier': outcome['faxbot_sdp_addresses']}
    for direction, item in outcome['results'].items():
        result = item['result'] or {}
        body = (item['captured'] or {}).get('body') or {}
        summary[direction] = {'sender_status': result.get('Status'), 'sender_pages': result.get('Pages'),
                              'mode': result.get('Mode'), 'sender_verdict': sip_calls.verdict(result) if result else None,
                              'receiver_status': body.get('faxstatus'), 'receiver_pages': body.get('faxpages')}
    return summary


def _delivered(outcome, direction):
    item = outcome['results'][direction]
    return (item['result'] or {}).get('Status') == 'SUCCESS' and ((item['captured'] or {}).get('body') or {}).get(
        'faxpages') == 2


@pytest.mark.parametrize('transport,random_ports', [('udp', False), ('tcp', True)])
def test_faxbot_behind_a_router_registers_and_faxes_both_ways_with_a_latching_carrier(tmp_path, transport,
                                                                                     random_ports):
    outcome = nat_exchange(tmp_path, transport=transport, random_ports=random_ports, latches=True)
    evidence = nat_evidence(outcome)
    print(f'\nNAT_{transport.upper()}_{"RANDOM" if random_ports else "KEEP"}_EVIDENCE ' + json.dumps(evidence, indent=2))
    assert outcome['registered'] and outcome['router_wan'] in outcome['contact']
    # No address was advertised: Faxbot's SDP names its private address and the carrier follows its packets.
    assert outcome['faxbot_transport_line'] == '' and outcome['router_wan'] not in evidence['sdp_addresses_seen_by_carrier']
    assert _delivered(outcome, 'sent') and _delivered(outcome, 'received')


def test_an_exact_address_reaches_the_carrier_through_a_port_keeping_router(tmp_path):
    """The address Faxbot found is what the carrier sees in the SDP.

    Delivery is recorded, not asserted: against a carrier that never latches,
    the carrier's first media packet can reach the router before Faxbot's, and
    Linux NAT then gives Faxbot's own flow a different outside port (see the
    router's conntrack lines in the evidence), so even an exact address does
    not guarantee the fax. A latching carrier is what the design relies on.
    """
    outcome = nat_exchange(tmp_path, transport='udp', random_ports=False, latches=False, exact_address=True)
    evidence = nat_evidence(outcome)
    evidence.update(delivered_sent=_delivered(outcome, 'sent'), delivered_received=_delivered(outcome, 'received'),
                    router_media_flows=outcome['conntrack'])
    print('\nNAT_EXACT_EVIDENCE ' + json.dumps(evidence, indent=2))
    assert outcome['registered']
    assert f'external_media_address={outcome["router_wan"]}' in outcome['faxbot_transport_line']
    assert f'local_net={outcome["faxbot_net"]}' in outcome['faxbot_transport_line']
    assert outcome['router_wan'] in evidence['sdp_addresses_seen_by_carrier']
    for direction in ('sent', 'received'):
        if not evidence[f'delivered_{direction}']:
            assert evidence[direction]['sender_status'] != 'SUCCESS'


def test_a_port_changing_router_and_a_carrier_that_never_latches_carry_no_fax_data(tmp_path):
    outcome = nat_exchange(tmp_path, transport='tcp', random_ports=True, latches=False)
    evidence = nat_evidence(outcome)
    print('\nNAT_NO_LATCH_EVIDENCE ' + json.dumps(evidence, indent=2))
    assert outcome['registered']
    assert not _delivered(outcome, 'sent')
    assert evidence['sent']['sender_verdict'] in sip_calls.NO_DATA_VERDICTS


# -- Sending together: one call carries two faxes, each gets its own outcome ---------------

def _together_installation(tmp_path):
    """A SIP installation with two faxes waiting for one number, claimed as one call."""
    from datetime import datetime, timedelta
    from app.schema import create_database_engine, upgrade_schema
    from app.config_store import ConfigurationStore
    from app.config_profiles import ProviderConfiguration
    from app.outbound_store import OutboundStore
    from app.routing.costs import RateCard
    from app.routing.store import RouteStore
    from app.batching import store as batching
    data = tmp_path / 'faxdata'
    data.mkdir()
    engine = create_database_engine('sqlite:///' + str(tmp_path / 'installation.db'))
    upgrade_schema(engine)
    configuration = ConfigurationStore(engine, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'FAX_DISABLED': 'false',
                                                   'FAX_DATA_DIR': str(data)})
    snapshot = configuration.initialize(values, actor='proof', providers={
        'outbound': ProviderConfiguration('sip', traits={'requires_tiff': True})})
    RouteStore(engine, sip_preset=lambda: '').replace_cards([RateCard(
        None, 'sip', 'outbound', 'Proof trunk', 'USD', 5000, 0, 0, 60, 60, None, datetime(2026, 10, 3))])
    batching.BatchingSettings(engine).save(DID, enabled=True, recipient_agreed=True, actor='principal:proof')
    pages = proof_pages()
    documents = [(uuid.uuid4().hex, pages[:1]), (uuid.uuid4().hex, pages)]
    accepted = datetime.utcnow() - timedelta(minutes=11)
    for number, (job_id, job_pages) in enumerate(documents):
        job_pages[0].save(data / (job_id + '.tiff'), save_all=True, append_images=job_pages[1:],
                          compression='group4', dpi=(204, 196))
        at = accepted + timedelta(seconds=number)
        with configuration._locked() as connection:
            configuration._accept_outbound_on(connection, snapshot.active, {
                'id': job_id, 'to_number': DID, 'file_name': 'proof.pdf', 'tiff_path': '', 'status': 'queued',
                'pages': len(job_pages), 'created_at': at, 'updated_at': at})
            batching.hold_on(connection, batching.tables(engine, connection), job_id,
                             batching.HoldPlan(DID, 'key:proof', 'Proof Desk', len(job_pages), False, 600), at)
    delivery = OutboundStore(configuration)
    claim = delivery.claim('proof-worker')
    assert [member.job_id for member in claim.members] == [job_id for job_id, _ in documents]
    return delivery, claim, data, [job_id for job_id, _ in documents]


def _send_together(tmp_path, **options):
    """Claim two waiting faxes as one call, send its image over T.38, and apply the sender's result."""
    from app.batching import results
    from app.batching.transport import call_image
    delivery, claim, data, jobs = _together_installation(tmp_path)
    image = call_image(delivery, data, claim)
    assert delivery.begin_submission(claim)
    delivery.record_receipt(claim, provider_sid=claim.job_id, status='in_progress')
    outcome = exchange(tmp_path, fax_image=image, identity=(claim.job_id, claim.attempt_id), **options)
    assert outcome['result']['JobID'] == claim.job_id and outcome['result']['AttemptID'] == claim.attempt_id
    applied = results.apply_fax_result(delivery, outcome['result'], failure_sentence=sip_calls.result_summary(
        outcome['result']))
    assert applied is True
    states = [delivery.get(job)['state'] for job in jobs]
    attempts = [delivery.get(job)['attempt_id'] for job in jobs]
    with delivery.configuration.engine.connect() as connection:
        import sqlalchemy as sa
        categories = [connection.scalar(sa.select(delivery.attempts.c.error_category).where(
            delivery.attempts.c.id == attempt)) for attempt in attempts]
    return outcome, states, categories


def test_one_call_carries_two_faxes_over_t38_and_each_gets_its_own_outcome(tmp_path):
    outcome, states, categories = _send_together(tmp_path)
    result, captured = outcome['result'], outcome['captured']
    assert captured is not None, 'Receiver did not report the fax'
    sent, received = outcome['sent'], outcome['received']
    sent_heights, received_heights = page_heights(sent), page_heights(received)
    header_rows = received_heights[0] - sent_heights[0]
    evidence = {
        'image': outcome['image'],
        'sender_result': {key: result.get(key) for key in ('Status', 'Error', 'Pages', 'Mode', 'Cause')},
        'receiver_report': {key: captured['body'].get(key) for key in ('faxstatus', 'faxpages')},
        'connected_seconds': int(result['Ended']) - int(result['Answered']),
        'pages_in_call': len(sent_heights),
        'outcomes': states, 'categories': categories,
    }
    print('\nSEND_TOGETHER_EVIDENCE ' + json.dumps(evidence, indent=2))
    # Separator, document one (1 page), separator, document two (2 pages).
    assert len(sent_heights) == len(received_heights) == 5
    assert result['Status'] == 'SUCCESS' and result['Pages'] == '5' and result['Mode'] == 'T38'
    assert captured['body']['faxpages'] == 5
    assert [page['body_sha256'] for page in page_digests(received, skip_rows=header_rows)] == [
        page['body_sha256'] for page in page_digests(sent)]
    assert states == ['success', 'success'] and categories == [None, None]


def test_a_shared_call_cut_part_way_delivers_only_the_confirmed_faxes(tmp_path):
    """FAXPAGES on a cut call: the sender's count of pages the receiving machine confirmed."""
    # 27 seconds ends the call after the first fax and its separator on the proof machine (2 pages confirmed).
    seconds = float(os.environ.get('FAXBOT_PROOF_CUT_SECONDS', '27'))
    outcome, states, categories = _send_together(tmp_path, hang_up_after=seconds, wait_for_receiver=20)
    result = outcome['result']
    report = (outcome['captured'] or {}).get('body', {}) if outcome['captured'] else {}
    confirmed = int(result.get('Pages') or 0)
    evidence = {
        'hang_up_after_seconds': seconds,
        'sender_result': {key: result.get(key) for key in ('Status', 'Error', 'Pages', 'Mode', 'Cause')},
        'receiver_report': {key: report.get(key) for key in ('faxstatus', 'faxpages')} if report else None,
        'outcomes': states, 'categories': categories,
    }
    print('\nSEND_TOGETHER_CUT_EVIDENCE ' + json.dumps(evidence, indent=2))
    assert result['Status'] != 'SUCCESS' and confirmed < 5
    # Pages 1-2 are the first fax with its separator; pages 3-5 the second.
    expected = ['success' if confirmed >= 2 else 'failed', 'failed']
    assert states == expected
    second = 'partly_sent' if confirmed >= 3 else None
    assert categories == [None if confirmed >= 2 else ('partly_sent' if confirmed >= 1 else None), second]
    if report.get('faxpages') is not None:
        # The receiver may hold one page more than the sender saw confirmed, never fewer.
        assert confirmed <= int(report['faxpages']) <= confirmed + 1


# Junk senders (inbound/screening.py): the receiver's dialplan turns a blocked caller away before answering.

ORIGINATE_SESSION = r'''
exec 3<>/dev/tcp/127.0.0.1/5038
cat >&3
deadline=$((SECONDS + 90))
seen=''
while [ "$SECONDS" -lt "$deadline" ]; do
  if IFS= read -r -t 5 line <&3; then
    printf '%s\n' "$line"
    case $line in
      *'Event: OriginateResponse'*) seen=1 ;;
    esac
    if [ -n "$seen" ] && [ -z "${line%$'\r'}" ]; then exit 0; fi
  fi
done
exit 3
'''


def screened_call(tmp_path, entries):
    """Two Asterisk containers; the receiver's database holds ``entries`` ({key: expiry epoch}) in faxbot-screen.

    The sender places one call with CALLER as its caller ID; the result says how the receiver took it.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    docker = Docker()
    image = os.environ.get('FAXBOT_NATIVE_IMAGE') or 'faxbot-native:t38-proof'
    if not os.environ.get('FAXBOT_NATIVE_IMAGE'):
        docker.run('build', '--quiet', '--tag', image, str(ROOT / 'asterisk'), timeout=3600)
    try:
        docker.network = docker.prefix
        docker.run('network', 'create', '--internal', '--label', 'com.faxbot.scope=t38-proof', docker.network)
        capture = docker.start('api', 'python:3.11-slim', 'python', '-c', CAPTURE)
        receiver = docker.start('receiver', image, 'infinity', entrypoint='sleep')
        sender = docker.start('sender', image, 'infinity', entrypoint='sleep')
        addresses = {name: docker.address(container)
                     for name, container in (('api', capture), ('receiver', receiver), ('sender', sender))}
        for container, peer in ((receiver, addresses['sender']), (sender, addresses['receiver'])):
            rendered = tmp_path / f'{container}.conf'
            rendered.write_text(sip_trunk.render_pjsip(trunk_values(peer)))
            docker.run('exec', container, 'mkdir', '-p', '/faxdata/asterisk', '/faxdata/outbound')
            docker.run('cp', str(rendered), f'{container}:/faxdata/asterisk/pjsip.conf')
        logger = tmp_path / 'logger.conf'
        logger.write_text('[general]\ndateformat=%F %T\n\n[logfiles]\nconsole => notice,warning,error,verbose\n')
        docker.run('cp', str(logger), f'{receiver}:/etc/asterisk/logger.conf')
        environment = ['--env', f'ASTERISK_AMI_USERNAME={AMI_USER}', '--env', f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}']
        docker.run('exec', '--detach', *environment, '--env', f'ASTERISK_INBOUND_SECRET={SECRET}',
                   '--env', f'FAXBOT_API_URL=http://{addresses["api"]}:8080', receiver,
                   'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        docker.run('exec', '--detach', *environment, sender, 'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        wait_booted(docker, receiver)
        wait_booted(docker, sender)
        docker.asterisk(receiver, 'pjsip set logger on')
        docker.run('exec', '--detach', *environment, receiver, 'bash', '-c', AMI_LISTENER)
        deadline = time.monotonic() + 20
        while 'Authentication accepted' not in docker.read(receiver, '/tmp/ami-events.log'):
            assert time.monotonic() < deadline, 'The receiver manager listener did not log in'
            time.sleep(0.5)
        for key, value in entries.items():
            docker.asterisk(receiver, f'database put faxbot-screen {key} {value}')
        pages = proof_pages()
        sent = tmp_path / 'proof.tiff'
        pages[0].save(sent, save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
        docker.run('cp', str(sent), f'{sender}:/faxdata/outbound/proof.tiff')
        values = trunk_values(addresses['receiver'], FAX_LOCAL_STATION_ID=CALLER, FAX_HEADER='Faxbot proof')
        fields = ami.originate_fields_for(values, uuid.uuid4().hex, DID, '/faxdata/outbound/proof.tiff',
                                          attempt_id=uuid.uuid4().hex)
        actions = (f'Action: Login\r\nActionID: proof-login\r\nUsername: {AMI_USER}\r\n'
                   f'Secret: {AMI_PASSWORD}\r\nEvents: call,user\r\n\r\n'
                   + ''.join(f'{key}: {value}\r\n' for key, value in fields.items()) + '\r\n')
        session = docker.run('exec', '--interactive', sender, 'bash', '-c', ORIGINATE_SESSION,
                             input_text=actions, check=False, timeout=120)
        originate = next((event for event in parse_ami(session.stdout)
                          if event.get('Event') == 'OriginateResponse'), {})
        # The receiver's event and queue entry come with its reply; wait for them, then end any call.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and 'FaxScreened' not in docker.read(receiver, '/tmp/ami-events.log') \
                and originate.get('Response') != 'Success':
            time.sleep(0.5)
        docker.asterisk(sender, 'channel request hangup all')
        return {'originate': originate, 'receiver_log': docker.read(receiver, '/tmp/asterisk.log'),
                'events': parse_ami(docker.read(receiver, '/tmp/ami-events.log')),
                'queue': docker.asterisk(receiver, 'database show faxbot-screened')}
    finally:
        docker.close()


def test_a_blocked_caller_is_declined_before_answer_and_an_expired_entry_lets_the_call_through(tmp_path):
    key = CALLER.lstrip('+')
    blocked = screened_call(tmp_path / 'blocked', {key: int(time.time()) + 3600})
    log = blocked['receiver_log']
    assert blocked['originate'].get('Response') == 'Failure', blocked['originate']
    assert 'SIP/2.0 603 Decline' in log
    # The INVITE was never answered: no 200 OK in reply to it, and no fax session started.
    assert not re.search(r'SIP/2\.0 200 OK[^\n]*\n(?:[^\n]+\n)*?CSeq: \d+ INVITE', log)
    assert 'ReceiveFAX' not in log
    screened = [event for event in blocked['events'] if event.get('UserEvent') == 'FaxScreened']
    assert len(screened) == 1 and screened[0]['Caller'] == CALLER and screened[0]['DID'] == DID
    assert f'{CALLER}:{DID}:' in blocked['queue']
    print(json.dumps({'blocked': {'originate': blocked['originate'], 'event': screened[0],
                                  'queue': blocked['queue'].strip().splitlines()[:2]}}, indent=2))
    expired = screened_call(tmp_path / 'expired', {key: int(time.time()) - 60})
    assert expired['originate'].get('Response') == 'Success', expired['originate']
    assert not any(event.get('UserEvent') == 'FaxScreened' for event in expired['events'])
    print(json.dumps({'expired': {'originate': expired['originate']}}, indent=2))



# Patch 0004 on real Asterisk: far-end frames, T.38 at once, and Internet Aware Fax between two Faxbots.

def four_pages(path):
    """Four distinct fine pages, so a paced call lasts long enough to compare."""
    from PIL import Image, ImageDraw
    pages = []
    for number in range(4):
        page = Image.new('1', (1728, 2200), 1)
        draw = ImageDraw.Draw(page)
        for row in range(100, 2150, 23 + number * 5):
            draw.rectangle([80 + number * 20, row, 1650 - number * 30, row + 7], fill=0)
        pages.append(page)
    pages[0].save(path, save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
    return path


def test_the_far_ends_frames_come_with_each_built_in_engine_call_and_t38_at_once_asks_at_the_answer(tmp_path):
    """Asterisk's own ReceiveFAX (the far end here) asks for T.38 three seconds after it answers, so a stock far
    end that never asks but accepts cannot be built from it; Faxbot's rule for such numbers is tested on synthetic
    calls in test_engine_frames.py. Here: without T.38 at once the far end asks first; with it, Faxbot asks at the
    answer, and both calls report who asked and when, with the far end's DIS."""
    from app import engine_frames
    from app.config_values import ConfigurationValues
    usual = exchange(tmp_path, wait_frames=True)
    now = exchange(tmp_path, wait_frames=True, extra_variables={'FAXBOT_T38_NOW': 'yes'})
    report = {}
    for name, outcome in (('usual', usual), ('now', now)):
        assert outcome['result']['Status'] == 'SUCCESS', outcome['result']
        row = engine_frames.parse_event(outcome['frames'] or {})
        assert row is not None, outcome['frames']
        dis = engine_frames.decode_dis(row['dis'])
        # The far end is Asterisk's own engine: V.17 with error correction, as Faxbot's receive settings offer.
        assert dis and dis['max_rate'] == 14400 and dis['ecm'], row['dis']
        assert row['rate_first'] == 14400 and row['trainings'] >= 1 and row['mode'] == 'T38', outcome['frames']
        report[name] = {'t38_by': row['t38_by'], 't38_after_ms': row['t38_after_ms'], 'dis': row['dis'],
                        'rates': row['rates'], 'seconds': outcome['submit_to_result_seconds']}
    assert report['usual']['t38_by'] == 'far' and report['usual']['t38_after_ms'] >= 2500, report
    assert report['now']['t38_by'] == 'faxbot' and report['now']['t38_after_ms'] < 2000, report
    assert 'Faxbot fax frames on' in usual['sender_log']
    # Calls where Faxbot had to ask late teach it to ask at once; the far end asking itself teaches nothing.
    late = dict(engine_frames.parse_event(usual['frames']), t38_by='faxbot', t38_after_ms=10600)
    defaults = ConfigurationValues.from_environment({})
    assert engine_frames.learn([late] * 3, defaults).t38_now
    assert not engine_frames.learn([engine_frames.parse_event(usual['frames'])] * 3, defaults).t38_now
    print(json.dumps({'frames_and_early_t38': report}, indent=2))


def test_audio_fax_for_one_number_keeps_its_calls_on_audio_both_ways_with_the_same_pages(tmp_path):
    """Engine learning (T8): fax over IP failed to or from one number, so Faxbot uses audio fax for it although both
    sides offer T.38. Sent: the call carries FAXBOT_AUDIO=yes (SendFAX F refuses the far end's T.38 request).
    Received: the caller's key in faxbot-inmode makes Faxbot answer with ReceiveFAX F; here the caller asks for T.38
    at the answer (FAXBOT_T38_NOW), is refused and goes on as audio. Every page arrives intact.

    Not covered, because it fails (measured 2026-10-07): a caller that stays silent for about ten seconds before
    asking for T.38 (Asterisk's own SendFAX z) misses Faxbot's three DIS and gets a DCN, as GOfax.IP's inbound
    fallback would. That failure teaches "audio failed" too, so the caller goes back to the usual settings."""
    from app import engine_frames
    (tmp_path / 'sent').mkdir()
    (tmp_path / 'received').mkdir()
    sent = exchange(tmp_path / 'sent', extra_variables={'FAXBOT_AUDIO': 'yes'})
    received = exchange(tmp_path / 'received', extra_variables={'FAXBOT_T38_NOW': 'yes'},
                        receiver_db={f'faxbot-inmode/{key}': 'audio' for key in engine_frames.caller_keys(CALLER)})
    report = {}
    for name, outcome in (('sent', sent), ('received', received)):
        assert outcome['result']['Status'] == 'SUCCESS' and outcome['result']['Pages'] == '2', (name, outcome['result'])
        assert outcome['result']['Mode'] == 'audio', (name, outcome['result'])
        body = outcome['captured']['body']
        assert body['call']['t38'] is False and body['faxpages'] == 2, (name, body['call'])
        sent_heights, received_heights = page_heights(outcome['sent']), page_heights(outcome['received'])
        header_rows = received_heights[0] - sent_heights[0]
        assert [page['body_sha256'] for page in page_digests(outcome['received'], skip_rows=header_rows)] == \
            [page['body_sha256'] for page in page_digests(outcome['sent'])], name
        report[name] = {'mode': outcome['result']['Mode'], 'seconds': outcome['submit_to_result_seconds']}
    print(json.dumps({'audio_for_one_number': report}, indent=2))


def test_internet_aware_fax_between_two_faxbots_is_shorter_with_the_same_pages(tmp_path):
    image = four_pages(tmp_path / 'four.tiff')
    key = CALLER.lstrip('+')
    paced = exchange(tmp_path, fax_image=image, wait_frames=True, extra_variables={'FAXBOT_T38_NOW': 'yes'})
    iaf = exchange(tmp_path, fax_image=image, wait_frames=True,
                   extra_variables={'FAXBOT_T38_NOW': 'yes', 'FAXBOT_IAF': 'peer'},
                   receiver_db={f'faxbot-iaf/{key}': 'peer'})
    report = {}
    for name, outcome in (('paced', paced), ('iaf', iaf)):
        assert outcome['result']['Status'] == 'SUCCESS' and outcome['result']['Pages'] == '4', outcome['result']
        # The sending engine adds one header line above each page; the image below it is exactly what was sent.
        header_rows = page_heights(outcome['received'])[0] - page_heights(image)[0]
        assert [page['body_sha256'] for page in page_digests(outcome['received'], skip_rows=header_rows)] == [
            page['body_sha256'] for page in page_digests(image)], name
        answered, ended = int(outcome['result']['Answered']), int(outcome['result']['Ended'])
        report[name] = {'seconds': outcome['submit_to_result_seconds'], 'answered_to_end': ended - answered,
                        'iaf': (outcome['frames'] or {}).get('Iaf')}
    assert report['iaf']['iaf'] == 'peer' and not report['paced']['iaf']
    assert 'Internet Aware Fax (peer)' in iaf['sender_log'] and 'Internet Aware Fax (peer)' in iaf['receiver_log']
    assert report['iaf']['seconds'] < report['paced']['seconds'], report
    print(json.dumps({'iaf': report}, indent=2))


def test_a_subaddress_the_built_in_engine_asks_for_reaches_the_receiving_engine_on_t38_and_audio(tmp_path,
                                                                                                    monkeypatch):
    """Patch 0005 (M17): the fax asks for subaddress 4021 through the production Originate fields (as a notice
    fax's notice ID or a sending rule's subaddress would). The receiving Asterisk, also 0005, says in its DIS that it
    takes a subaddress, so the sender's spandsp sends SUB, and the receiver hands it to Faxbot with the fax
    (sub_hex). Over T.38 and over audio alike; the sender's frames show the far end took it (carried)."""
    from app import engine_frames
    monkeypatch.setattr(ami, 'fax_subaddress', lambda job_id: '4021')
    (tmp_path / 't38').mkdir()
    (tmp_path / 'audio').mkdir()
    report = {}
    for name, extra in (('t38', None), ('audio', {'FAXBOT_AUDIO': 'yes'})):
        outcome = exchange(tmp_path / name, wait_frames=True, extra_variables=extra)
        result, captured = outcome['result'], outcome['captured']
        assert 'FAXBOT_TX_SUB=4021' in outcome['fields']['Variable'].split(','), outcome['fields']
        assert result['Status'] == 'SUCCESS' and result['Pages'] == '2', (name, result)
        assert result['Mode'] == ('audio' if name == 'audio' else 'T38'), (name, result)
        assert 'Faxbot: subaddress 4021 requested on' in outcome['sender_log'], name
        row = engine_frames.parse_event(outcome['frames'] or {})
        assert engine_frames.subaddress_carried('4021', row) is True, (name, row)
        assert captured is not None, name
        assert engine_frames.decode_sub(captured['body']['sub_hex']) == '4021', (name, captured['body'])
        report[name] = {'mode': result['Mode'], 'far_dis': row['dis'], 'received_sub': captured['body']['sub_hex'],
                        'seconds': outcome['submit_to_result_seconds'], 'image': outcome['image']}
    print(json.dumps({'subaddress_sent': report}, indent=2))


WIREGUARD_DOCKERFILE = '''FROM debian:trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a
RUN apt-get update && apt-get install -y --no-install-recommends wireguard-tools iproute2 \\
    && rm -rf /var/lib/apt/lists/*
'''
AMI_SESSION_ROUTE = AMI_SESSION.replace("*'UserEvent: FaxResult'*) seen=1 ;;", "*'UserEvent: FaxPeerRoute'*) seen=1 ;;")
SENDER_PEER, RECEIVER_PEER = 'a1' * 16, 'b2' * 16
TUNNEL = {'sender': '10.77.0.1', 'receiver': '10.77.0.2'}


def test_a_peer_fax_call_goes_inside_a_wireguard_tunnel_between_two_asterisks_with_no_carrier(tmp_path):
    """M1b over loopback: two Faxbot Asterisks on a private network, each with a WireGuard tunnel set up outside
    Faxbot (a WireGuard container sharing the Asterisk container's network, as the console says; the Asterisk
    containers get no extra privilege). Each side's file has the trunk Faxbot renders and the partner's peer endpoint
    (direct/peer_call.render_peers). Faxbot's route check, run in the sender's Asterisk through the production
    Originate (ami.peer_route_fields), sees the tunnel (wg0, wireguard); the fax goes to the partner's endpoint with
    Internet Aware Fax, arrives over T.38 through the tunnel, and the receiver hands it over naming the partner.
    Then the tunnel is taken down: the same check sees the ordinary network, so Faxbot places no peer call."""
    from app import engine_frames
    from app.direct import peer_call
    docker = Docker()
    image = os.environ.get('FAXBOT_NATIVE_IMAGE') or 'faxbot-native:t38-proof'
    if not os.environ.get('FAXBOT_NATIVE_IMAGE'):
        docker.run('build', '--quiet', '--tag', image, str(ROOT / 'asterisk'), timeout=3600)
    wireguard = docker.prefix + '-wireguard'
    context = tmp_path / 'wireguard-image'
    context.mkdir()
    (context / 'Dockerfile').write_text(WIREGUARD_DOCKERFILE)
    docker.run('build', '--quiet', '--tag', wireguard, str(context), timeout=900)
    try:
        docker.network = docker.prefix
        docker.run('network', 'create', '--internal', '--label', 'com.faxbot.scope=t38-proof', docker.network)
        capture = docker.start('api', 'python:3.11-slim', 'python', '-c', CAPTURE)
        sides = {name: docker.start(name, image, 'infinity', entrypoint='sleep') for name in ('receiver', 'sender')}
        addresses = {name: docker.address(container) for name, container in sides.items()}
        api_address = docker.address(capture)
        # The tunnel, outside Faxbot: one WireGuard container in each Asterisk container's network.
        tunnels, keys = {}, {}
        for name, container in sides.items():
            sidecar = f'{docker.prefix}-wg-{name}'
            docker.run('run', '--detach', '--name', sidecar, '--network', f'container:{container}', '--cap-add',
                       'NET_ADMIN', '--label', 'com.faxbot.scope=t38-proof', '--entrypoint', 'sleep', wireguard,
                       'infinity')
            docker.containers.append(sidecar)
            tunnels[name] = sidecar
            private = docker.run('exec', sidecar, 'wg', 'genkey').stdout.strip()
            public_key = docker.run('exec', '--interactive', sidecar, 'wg', 'pubkey',
                                    input_text=private + '\n').stdout.strip()
            keys[name] = (private, public_key)
        for name, sidecar in tunnels.items():
            other = 'receiver' if name == 'sender' else 'sender'
            script = (f"printf '%s' '{keys[name][0]}' > /tmp/key && ip link add wg0 type wireguard && "
                      f"wg set wg0 listen-port 51820 private-key /tmp/key peer {keys[other][1]} "
                      f"endpoint {addresses[other]}:51820 allowed-ips {TUNNEL[other]}/32 persistent-keepalive 5 && "
                      f"ip addr add {TUNNEL[name]}/24 dev wg0 && ip link set wg0 up")
            docker.run('exec', sidecar, 'sh', '-c', script)
        # Each side's file: the trunk as Faxbot renders it, then the partner's peer endpoint.
        partners = {'sender': {'id': RECEIVER_PEER, 'state': 'verified', 'expires_at': None,
                               'partner_peer_calls': 1, 'receive_peer_calls': 1,
                               'peer_call_address': TUNNEL['receiver']},
                    'receiver': {'id': SENDER_PEER, 'state': 'verified', 'expires_at': None,
                                 'partner_peer_calls': 1, 'receive_peer_calls': 1,
                                 'peer_call_address': TUNNEL['sender']}}
        for name, container in sides.items():
            other = 'receiver' if name == 'sender' else 'sender'
            text = sip_trunk.render_pjsip(trunk_values(addresses[other])) + '\n\n' + peer_call.render_peers(
                [partners[name]])
            rendered = tmp_path / f'{name}.conf'
            rendered.write_text(text)
            docker.run('exec', container, 'mkdir', '-p', '/faxdata/asterisk', '/faxdata/outbound')
            docker.run('cp', str(rendered), f'{container}:/faxdata/asterisk/pjsip.conf')
        logger = tmp_path / 'logger.conf'
        logger.write_text('[general]\ndateformat=%F %T\n\n[logfiles]\nconsole => notice,warning,error,verbose\n')
        docker.run('cp', str(logger), f"{sides['receiver']}:/etc/asterisk/logger.conf")
        docker.run('exec', '--detach', '--env', f'ASTERISK_INBOUND_SECRET={SECRET}',
                   '--env', f'FAXBOT_API_URL=http://{api_address}:8080', sides['receiver'], 'sh', '-c',
                   '/start.sh > /tmp/asterisk.log 2>&1')
        docker.run('exec', '--detach', '--env', f'ASTERISK_AMI_USERNAME={AMI_USER}',
                   '--env', f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}', sides['sender'],
                   'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        for container in sides.values():
            wait_booted(docker, container)
        docker.asterisk(sides['receiver'], 'pjsip set logger on')
        login = (f'Action: Login\r\nActionID: proof-login\r\nUsername: {AMI_USER}\r\n'
                 f'Secret: {AMI_PASSWORD}\r\nEvents: call,user\r\n\r\n')

        def route_check(address):
            token = uuid.uuid4().hex
            fields = ami.peer_route_fields(token, address)
            session = docker.run('exec', '--interactive', sides['sender'], 'bash', '-c', AMI_SESSION_ROUTE,
                                 input_text=login + ''.join(f'{k}: {v}\r\n' for k, v in fields.items()) + '\r\n',
                                 check=False, timeout=120)
            event = next((item for item in parse_ami(session.stdout) if item.get('UserEvent') == 'FaxPeerRoute'
                          and item.get('Check') == token), None)
            assert event is not None, session.stdout[-2000:]
            parts = event.get('Route', '').split('/')
            return peer_call.Tunnel(*parts) if len(parts) == 2 and parts[1] in peer_call.TUNNEL_KINDS else None, event

        # The partner answers Asterisk's checks over the tunnel (its contact is Reachable).
        deadline = time.monotonic() + 60
        while 'Avail' not in docker.asterisk(sides['sender'], f'pjsip show aor peer-{RECEIVER_PEER}-aor'):
            assert time.monotonic() < deadline, docker.asterisk(sides['sender'], 'pjsip show contacts')
            time.sleep(1)
        tunnel, route_event = route_check(TUNNEL['receiver'])
        assert tunnel == peer_call.Tunnel('wg0', 'wireguard'), route_event
        partner = {'organization': 'Valley Hospital', **partners['sender']}
        decision = peer_call.require_tunnel(partner, tunnel_lookup=lambda address: tunnel)
        call = peer_call.PeerCall(RECEIVER_PEER, peer_call.endpoint_name(RECEIVER_PEER), decision)

        sent = tmp_path / 'proof.tiff'
        pages = proof_pages()
        pages[0].save(sent, save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
        docker.run('cp', str(sent), f"{sides['sender']}:/faxdata/outbound/proof.tiff")
        values = trunk_values(addresses['receiver'], FAX_LOCAL_STATION_ID='+15555550100', FAX_HEADER='Faxbot proof')
        job, attempt = uuid.uuid4().hex, uuid.uuid4().hex
        fields = ami.originate_fields_for(values, job, DID, '/faxdata/outbound/proof.tiff', attempt_id=attempt,
                                          peer=call)
        before = docker.run('exec', tunnels['sender'], 'cat', '/sys/class/net/wg0/statistics/tx_bytes').stdout
        session = docker.run('exec', '--interactive', sides['sender'], 'bash', '-c', AMI_SESSION_FRAMES,
                             input_text=login + ''.join(f'{k}: {v}\r\n' for k, v in fields.items()) + '\r\n',
                             check=False, timeout=300)
        after = docker.run('exec', tunnels['sender'], 'cat', '/sys/class/net/wg0/statistics/tx_bytes').stdout
        events = parse_ami(session.stdout)
        result = next((event for event in events if event.get('UserEvent') == 'FaxResult'), None)
        frames = next((event for event in events if event.get('UserEvent') == 'FaxFrames'), None)
        assert result is not None, json.dumps(events[-6:])
        captured = None
        deadline = time.monotonic() + 60
        while captured is None and time.monotonic() < deadline:
            probe = docker.read(capture, '/tmp/capture.json')
            captured = json.loads(probe) if probe.strip() else None
            if captured is None:
                time.sleep(1)
        receiver_log = docker.read(sides['receiver'], '/tmp/asterisk.log')
        report = {'channel': fields['Channel'], 'route': route_event.get('Route'), 'status': result.get('Status'),
                  'pages': result.get('Pages'), 'mode': result.get('Mode'), 'iaf': (frames or {}).get('Iaf'),
                  'tunnel_tx_bytes': int(after or 0) - int(before or 0),
                  'handover_peer': (captured or {}).get('body', {}).get('peer'),
                  'invite_from_tunnel': f'<--- Received SIP request' in receiver_log and TUNNEL['sender'] in receiver_log}
        assert fields['Channel'] == f'PJSIP/{DID}@peer-{RECEIVER_PEER}-endpoint', fields
        assert result['Status'] == 'SUCCESS' and result['Pages'] == '2' and result['Mode'] == 'T38', report
        assert engine_frames.parse_event(frames)['iaf'] == 'peer', report
        assert report['tunnel_tx_bytes'] > 10_000, report
        assert captured is not None and captured['body']['peer'] == SENDER_PEER, report
        assert captured['body']['call']['peer'] == SENDER_PEER and captured['body']['faxpages'] == 2
        assert report['invite_from_tunnel'], report

        # The tunnel goes down: the route check sees the ordinary network, so no peer fax call is placed.
        docker.run('exec', tunnels['sender'], 'ip', 'link', 'del', 'wg0')
        down, down_event = route_check(TUNNEL['receiver'])
        assert down is None and not down_event.get('Route', '').endswith('/wireguard'), down_event
        refused = peer_call.decide(partner, tunnel_lookup=lambda address: down)
        assert not refused.applies and refused.reason == 'no_tunnel'
        report['route_without_tunnel'] = down_event.get('Route')
        print(json.dumps({'peer_fax_call': report}, indent=2))
    finally:
        docker.close()
        docker.run('rmi', wireguard, check=False)


def test_a_second_trunk_carries_the_fax_and_the_receiver_hands_over_which_trunk_it_came_in_on(tmp_path):
    """Several trunks over loopback: Faxbot's file has two trunks on each side; the fax goes out over the second
    trunk's endpoint (carrier B, the other container), and the receiver, which identifies the caller by address on
    its own second trunk, hands the fax to Faxbot naming that trunk (FAXBOT_TRUNK from the endpoint's set_var)."""
    outcome = exchange(tmp_path, two_trunks=True)
    result, captured = outcome['result'], outcome['captured']
    print(json.dumps({'two_trunks': {
        'channel': outcome['fields']['Channel'], 'status': result.get('Status'), 'pages': result.get('Pages'),
        'mode': result.get('Mode'), 'handover_trunk': (captured or {}).get('body', {}).get('trunk'),
        'call_trunk': ((captured or {}).get('body', {}).get('call') or {}).get('trunk'),
        'sender_endpoints': [line.strip() for line in outcome['sender_endpoints'].splitlines()
                             if line.strip().startswith('Endpoint:')],
        'receiver_endpoints': [line.strip() for line in outcome['receiver_endpoints'].splitlines()
                               if line.strip().startswith('Endpoint:')],
        'image': outcome['image']}}, indent=2))
    assert outcome['fields']['Channel'] == f'PJSIP/{DID}@trunk-{SECOND}-endpoint'
    for listing in (outcome['sender_endpoints'], outcome['receiver_endpoints']):
        assert 'trunk-endpoint' in listing and f'trunk-{SECOND}-endpoint' in listing
    assert result['Status'] == 'SUCCESS' and result['Pages'] == '2', result
    assert captured is not None, 'Receiver did not report the fax'
    assert captured['body']['trunk'] == SECOND and captured['body']['call']['trunk'] == SECOND
    assert captured['body']['faxstatus'] == 'SUCCESS' and captured['body']['faxpages'] == 2
    # The dialplan read each call's own endpoint for its T.38 checks: no unknown channel item on either side.
    for log in (outcome['sender_log'], outcome['receiver_log']):
        assert 'Unknown or unavailable item requested' not in log
