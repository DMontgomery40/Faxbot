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

from app import ami, sip_trunk
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


def wait_booted(docker, container):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if 'Asterisk 22' in docker.asterisk(container, 'core show version'):
            if docker.run('exec', container, 'asterisk', '-rx', 'core waitfullybooted', check=False).returncode == 0:
                return
        time.sleep(0.5)
    raise RuntimeError('Asterisk did not start')


def test_two_asterisk_containers_exchange_a_two_page_fax_over_t38(tmp_path):
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
        for container, peer in ((receiver, addresses['sender']), (sender, addresses['receiver'])):
            rendered = tmp_path / f'{container}.conf'
            rendered.write_text(sip_trunk.render_pjsip(trunk_values(peer)))
            docker.run('exec', container, 'mkdir', '-p', '/faxdata/asterisk', '/faxdata/outbound')
            docker.run('cp', str(rendered), f'{container}:/faxdata/asterisk/pjsip.conf')
        # Proof only: print the receiver's SIP messages so the INVITE headers and
        # the T.38 re-INVITE SDP can be checked from its container log.
        logger = tmp_path / 'logger.conf'
        logger.write_text('[general]\ndateformat=%F %T\n\n[logfiles]\nconsole => notice,warning,error,verbose\n')
        docker.run('cp', str(logger), f'{receiver}:/etc/asterisk/logger.conf')
        # The image's own start script, with Asterisk output kept in the container.
        docker.run('exec', '--detach', '--env', f'ASTERISK_INBOUND_SECRET={SECRET}',
                   '--env', f'FAXBOT_API_URL=http://{addresses["api"]}:8080', receiver,
                   'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        docker.run('exec', '--detach', '--env', f'ASTERISK_AMI_USERNAME={AMI_USER}',
                   '--env', f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}', sender,
                   'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        wait_booted(docker, receiver)
        wait_booted(docker, sender)
        docker.asterisk(receiver, 'pjsip set logger on')

        sent = tmp_path / 'proof.tiff'
        pages = proof_pages()
        pages[0].save(sent, save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
        docker.run('cp', str(sent), f'{sender}:/faxdata/outbound/proof.tiff')

        job, attempt = uuid.uuid4().hex, uuid.uuid4().hex
        values = trunk_values(addresses['receiver'], SIP_FAX_PREFERENCE_HEADER='true',
                              FAX_LOCAL_STATION_ID='+15555550100', FAX_HEADER='Faxbot proof')
        fields = ami.originate_fields_for(values, job, DID, '/faxdata/outbound/proof.tiff', attempt_id=attempt)
        actions = (f'Action: Login\r\nActionID: proof-login\r\nUsername: {AMI_USER}\r\n'
                   f'Secret: {AMI_PASSWORD}\r\nEvents: call,user\r\n\r\n'
                   + ''.join(f'{key}: {value}\r\n' for key, value in fields.items()) + '\r\n')
        submitted = time.time()
        session = docker.run('exec', '--interactive', sender, 'bash', '-c', AMI_SESSION,
                             input_text=actions, check=False, timeout=300)
        finished = time.time()
        events = parse_ami(session.stdout)
        result = next((event for event in events if event.get('UserEvent') == 'FaxResult'), None)
        originate = next((event for event in events if event.get('Event') == 'OriginateResponse'), {})
        assert result is not None, 'No fax result: ' + json.dumps(events[-6:])

        deadline = time.monotonic() + 60
        captured = None
        while time.monotonic() < deadline and captured is None:
            probe = docker.run('exec', capture, 'cat', '/tmp/capture.json', check=False)
            if probe.returncode == 0 and probe.stdout.strip():
                captured = json.loads(probe.stdout)
            else:
                time.sleep(1)
        assert captured is not None, 'Receiver did not report the fax'
        body = captured['body']
        received = tmp_path / 'received.tiff'
        docker.run('cp', f'{receiver}:{body["tiff_path"]}', str(received))

        log_text = docker.run('exec', receiver, 'cat', '/tmp/asterisk.log', check=False).stdout
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
            'image': json.loads(docker.run('image', 'inspect', image).stdout)[0]['Id'],
            'asterisk': docker.asterisk(sender, 'core show version').strip(),
            'spandsp': docker.run('exec', sender, 'dpkg-query', '-W', '-f', '${Version}', 'libspandsp2t64').stdout,
            'fax_capabilities': docker.asterisk(sender, 'fax show capabilities').strip().splitlines()[-2:],
            'originate_response': {key: originate.get(key) for key in ('Response', 'Reason')},
            'sender_result': {key: result.get(key) for key in ('Status', 'Error', 'Pages', 'Mode', 'Cause')},
            'sender_remote_station': base64.b64decode(result.get('Station64') or '').decode('utf-8', 'replace'),
            'connected_seconds': int(result['Ended']) - int(result['Answered']),
            'submit_to_result_seconds': round(finished - submitted, 1),
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
    finally:
        docker.close()
