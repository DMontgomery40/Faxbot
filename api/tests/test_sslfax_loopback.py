"""SSL Fax loopback proof, driven through Faxbot's own API (``make sslfax-proof``).

Runs only with FAXBOT_SSLFAX_PROOF=1; never in CI. On a private Docker network
with no internet access it starts:

* Faxbot: the API (api/Dockerfile), its Asterisk (asterisk/) and its SSL Fax
  engine (hylafax/), sharing one data volume as in docker-compose.yml;
* a carrier: a second Asterisk from the same image, with a trunk Faxbot
  renders, that hands every call to
* a peer: a second engine from the same image (HylaFAX+ on one IAX line),
  standing in for a fax machine on the internet.

The test configures the trunk with Faxbot's settings, presses Apply through
the API (which restarts Asterisk with the engine's lines), sends a three-page
PDF with ``POST /fax`` and waits for Faxbot's own result. It compares every
received page with the page Faxbot sent, reads both engines' session logs and
checks that the SSL Fax passcode is in no log.

The network uses 198.51.100.0/24 (TEST-NET-2), so the two engines reach each
other the way two installations on the internet do: the engine refuses
private, loopback, link-local and reserved addresses that a far end
advertises (see hylafax/patches), and a private Docker network would make
every SSL Fax attempt a refusal. One case advertises a private address on
purpose and shows that refusal.

Cases (M1): (a) SSL Fax over an audio call; (b) the same with T.38 on both
Asterisks, so the call crosses two T.38 gateways; (c) the peer advertises a
listener nothing answers, and the fax falls back to an ordinary one; (c2) the
peer advertises a private address and Faxbot's engine refuses it.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import uuid

import pytest

from app import hylafax_engine, sip_trunk
from app.config_values import ConfigurationValues

pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_SSLFAX_PROOF') != '1',
                       reason='Set FAXBOT_SSLFAX_PROOF=1 to run the SSL Fax loopback proof.'),
]

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = os.environ.get('FAXBOT_DOCKER_CONTEXT', 'colima-faxbot-refresh')
PREFIX = os.environ.get('FAXBOT_PROOF_PREFIX', 'faxbot-sslfax-proof')
SUBNET = '198.51.100.0/24'
ADDRESS = {'api': '198.51.100.10', 'asterisk': '198.51.100.11', 'hylafax': '198.51.100.12',
           'carrier': '198.51.100.20', 'peer': '198.51.100.21'}
# Inside the proof subnet, nothing answers here: a listener that cannot be reached.
NOBODY = '198.51.100.250'
FAXBOT_NUMBER, PEER_NUMBER = '+15555550100', '+15555550199'
LISTENER_PORT = 10443
PAGES = 3


class Docker:
    def __init__(self, label):
        self.base = ['docker', '--context', CONTEXT]
        self.prefix = f'{PREFIX}-{label}-{uuid.uuid4().hex[:6]}'
        self.containers, self.volumes, self.network = [], [], None

    def run(self, *args, check=True, input_text=None, timeout=600):
        result = subprocess.run(self.base + list(args), input=input_text, capture_output=True, text=True,
                                timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f'docker {args[0]} failed: {result.stderr[-2000:]}')
        return result

    def create(self, name, image, *command, env=None, volumes=(), alias=None, restart=True, entrypoint=None):
        container = f'{self.prefix}-{name}'
        args = ['create', '--name', container, '--network', self.network, '--ip', ADDRESS[name],
                '--label', 'com.faxbot.scope=sslfax-proof']
        if alias:
            args += ['--network-alias', alias]
        if restart:
            args += ['--restart', 'unless-stopped']
        if entrypoint:
            args += ['--entrypoint', entrypoint]
        for key, value in (env or {}).items():
            args += ['--env', f'{key}={value}']
        for volume, path in volumes:
            args += ['--volume', f'{volume}:{path}']
        self.run(*args, image, *command)
        self.containers.append(container)
        return container

    def volume(self, name):
        volume = f'{self.prefix}-{name}'
        self.run('volume', 'create', '--label', 'com.faxbot.scope=sslfax-proof', volume)
        self.volumes.append(volume)
        return volume

    def put(self, container, path, text):
        with tempfile.NamedTemporaryFile('w', delete=False) as handle:
            handle.write(text)
        try:
            self.run('cp', handle.name, f'{container}:{path}')
        finally:
            os.unlink(handle.name)

    def sh(self, container, script, check=False, timeout=120):
        return self.run('exec', container, 'sh', '-c', script, check=check, timeout=timeout)

    def read(self, container, path):
        result = self.run('exec', container, 'cat', path, check=False)
        return result.stdout if result.returncode == 0 else ''

    def read_bytes(self, container, path):
        result = subprocess.run(self.base + ['exec', container, 'cat', path], capture_output=True, timeout=60)
        return result.stdout if result.returncode == 0 else b''

    def asterisk(self, container, command):
        return self.run('exec', container, 'asterisk', '-rx', command, check=False).stdout

    def close(self):
        for container in reversed(self.containers):
            self.run('rm', '--force', '--volumes', container, check=False)
        for volume in self.volumes:
            self.run('volume', 'rm', '--force', volume, check=False)
        if self.network:
            self.run('network', 'rm', self.network, check=False)


def image(docker, variable, tag, *build):
    name = os.environ.get(variable)
    if name:
        return name
    name = f'{PREFIX}-{tag}:proof'
    docker.run('build', '--quiet', '--tag', name, *build, timeout=3600)
    return name


def proof_pdf():
    """Three pages with large distinct shapes and text, so every page differs."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=letter)
    for number in range(1, PAGES + 1):
        page.setFont('Helvetica-Bold', 40)
        page.drawString(72, 690, f'SSL FAX PROOF PAGE {number} OF {PAGES}')
        page.setFont('Helvetica', 18)
        for line in range(12):
            page.drawString(72, 620 - line * 24, f'Line {line + 1} of page {number}: the quick brown fox 0123456789')
        page.rect(72, 72, 80 + number * 90, 120 + number * 30, fill=1)
        page.showPage()
    page.save()
    return buffer.getvalue()


CARRIER_DIALPLAN = r'''[general]
static=yes
writeprotect=no
autofallthrough=yes

; The carrier hands every call from Faxbot to the peer's fax line.
[faxbot-inbound]
exten => _[+0-9].,1,NoOp(carrier: call for ${EXTEN} to the peer)
 same => n,GotoIf($["@GATEWAY@" != "yes"]?dial)
 same => n,Set(FAXOPT(gateway)=yes)
 same => n(dial),Dial(IAX2/faxbot-line1,60)
 same => n,Hangup()
exten => s,1,Goto(faxbot-inbound,5555550199,1)

; The peer's own fax line places calls to Faxbot (the inbound proof).
[faxbot-engine-out]
exten => _X.,1,Set(CALLERID(all)=<+15555550199>)
 same => n,GotoIf($["@GATEWAY@" != "yes"]?dial)
 same => n,Set(FAXOPT(gateway)=yes)
 same => n(dial),Dial(PJSIP/+${EXTEN}@trunk-endpoint,60)
 same => n,Hangup()
'''

LOGGER = '[general]\ndateformat=%F %T\n\n[logfiles]\nconsole => notice,warning,error,verbose\n'

AMI_LISTENER = r'''
exec 3<>/dev/tcp/127.0.0.1/5038
printf 'Action: Login\r\nUsername: %s\r\nSecret: %s\r\nEvents: user\r\n\r\n' \
  "$ASTERISK_AMI_USERNAME" "$ASTERISK_AMI_PASSWORD" >&3
deadline=$((SECONDS + 900))
while [ "$SECONDS" -lt "$deadline" ]; do
  if IFS= read -r -t 5 line <&3; then printf '%s\n' "$line" >> /tmp/ami-events.log; fi
done
'''


def parse_ami(text):
    events, block = [], {}
    for line in text.splitlines():
        line = line.rstrip('\r')
        if not line:
            if block:
                events.append(block)
            block = {}
        elif ':' in line:
            key, value = line.split(':', 1)
            block[key.strip()] = value.strip()
    if block:
        events.append(block)
    return events


def wait_for(probe, seconds, what):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(1)
    raise AssertionError(f'Timed out waiting for {what}')


def api(docker, method, path, *, key, body=None, files=None):
    """One request to Faxbot's API from inside its own container (python stdlib, no extra tools)."""
    script = r'''
import json, sys, urllib.request, uuid
method, path, key = sys.argv[1], sys.argv[2], sys.argv[3]
spec = json.loads(sys.stdin.read() or '{}')
headers = {'X-API-Key': key}
data = None
if spec.get('files'):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in spec.get('form', {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, (filename, path_, kind) in spec['files'].items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                     f'Content-Type: {kind}\r\n\r\n'.encode() + open(path_, 'rb').read() + b'\r\n')
    data = b''.join(parts) + f'--{boundary}--\r\n'.encode()
    headers['Content-Type'] = f'multipart/form-data; boundary={boundary}'
elif 'json' in spec:
    data = json.dumps(spec['json']).encode()
    headers['Content-Type'] = 'application/json'
request = urllib.request.Request('http://127.0.0.1:8080' + path, data=data, method=method, headers=headers)
try:
    with urllib.request.urlopen(request, timeout=120) as response:
        print(json.dumps({'status': response.status, 'body': response.read().decode()}))
except urllib.error.HTTPError as error:
    print(json.dumps({'status': error.code, 'body': error.read().decode()}))
except (urllib.error.URLError, OSError) as error:
    print(json.dumps({'status': 0, 'body': str(error)}))
'''
    spec = {}
    if body is not None:
        spec['json'] = body
    if files:
        spec['files'], spec['form'] = files
    result = docker.run('exec', '--interactive', f'{docker.prefix}-api', 'python', '-c', script, method, path, key,
                        input_text=json.dumps(spec), timeout=180)
    reply = json.loads(result.stdout.strip().splitlines()[-1])
    try:
        reply['json'] = json.loads(reply['body'])
    except ValueError:
        reply['json'] = None
    return reply


def bring_up(tmp_path, label, made, *, faxbot_t38, carrier_gateway, peer_listener, peer_sslfax=True,
             carrier_t38=None):
    """Start the whole loopback; returns (docker, context dict). ``made`` collects it for cleanup at once."""
    docker = Docker(label)
    made.append(docker)
    images = {
        'native': image(docker, 'FAXBOT_NATIVE_IMAGE', 'asterisk', str(ROOT / 'asterisk')),
        'engine': image(docker, 'FAXBOT_ENGINE_IMAGE', 'engine', str(ROOT / 'hylafax')),
        'api': image(docker, 'FAXBOT_API_IMAGE', 'api', '-f', str(ROOT / 'api' / 'Dockerfile'), str(ROOT)),
    }
    docker.network = docker.prefix
    docker.run('network', 'create', '--internal', '--subnet', SUBNET, '--label', 'com.faxbot.scope=sslfax-proof',
               docker.network)
    faxdata = docker.volume('faxdata')
    # The engine's own folders, mounted as docker-compose.yml mounts them.
    settings_volume, out_volume, state_volume = (docker.volume('hylafax-settings'), docker.volume('hylafax-out'),
                                                 docker.volume('hylafax'))
    bootstrap = 'proof-' + secrets.token_urlsafe(24)
    ami_user, ami_password = 'proof_ami', 'Proof-' + secrets.token_hex(16)
    inbound_secret = secrets.token_urlsafe(32)
    ami_env = {'ASTERISK_AMI_USERNAME': ami_user, 'ASTERISK_AMI_PASSWORD': ami_password}

    # Faxbot: Asterisk and the API share the data folder; the engine gets only its own folders
    # (settings read-only, the out folder, its volume), exactly as in docker-compose.yml.
    asterisk = docker.create('asterisk', images['native'], env=ami_env,
                             volumes=[(faxdata, '/faxdata'), (settings_volume, '/faxdata/hylafax')], alias='asterisk')
    engine = docker.create('hylafax', images['engine'], alias='hylafax',
                           volumes=[(settings_volume, '/faxdata/hylafax:ro'), (out_volume, '/faxdata/hylafax-out'),
                                    (state_volume, '/var/lib/faxbot-engine')])
    api_env = {
        'FAX_DATA_DIR': '/faxdata', 'DATABASE_URL': 'sqlite:////faxdata/faxbot.db', 'API_KEY': bootstrap,
        'REQUIRE_API_KEY': 'true', 'FAX_BACKEND': 'sip', 'FAX_DEFAULT_COUNTRY': 'US',
        'SIP_TRUNK_PRESET': 'custom', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': ADDRESS['carrier'],
        'SIP_TRUNK_CALLER_ID': FAXBOT_NUMBER, 'SIP_TRUNK_DIDS': FAXBOT_NUMBER,
        'SIP_T38_ENABLED': 'true' if faxbot_t38 else 'false', 'SIP_FAX_PREFERENCE_HEADER': 'true',
        'ASTERISK_AMI_HOST': 'asterisk', 'ASTERISK_INBOUND_SECRET': inbound_secret, 'SIP_PUBLIC_ADDRESS_CHECK_MINUTES': '0',
        'INBOUND_ENABLED': 'true',
        **ami_env,
    }
    api_container = docker.create('api', images['api'], env=api_env, alias='api',
                                  volumes=[(faxdata, '/faxdata'), (settings_volume, '/faxdata/hylafax'),
                                           (out_volume, '/faxdata/hylafax-out:ro')])

    # The carrier: a trunk back to Faxbot rendered by Faxbot itself, and the peer's line.
    carrier_values = ConfigurationValues.from_environment({
        'FAX_DATA_DIR': str(tmp_path / 'carrier'), 'SIP_TRUNK_PRESET': 'custom', 'SIP_TRUNK_AUTH': 'ip',
        'SIP_TRUNK_HOST': ADDRESS['asterisk'], 'SIP_TRUNK_CALLER_ID': PEER_NUMBER, 'SIP_TRUNK_DIDS': PEER_NUMBER,
        # The carrier's T.38: with its gateway on, or off so it refuses every T.38 request (case e, inbound).
        'SIP_T38_ENABLED': 'true' if (carrier_gateway if carrier_t38 is None else carrier_t38) else 'false',
        'ASTERISK_INBOUND_SECRET': inbound_secret})
    peer_secrets = hylafax_engine.engine_secrets(carrier_values, lines=1)
    carrier = docker.create('carrier', images['native'], env={'ASTERISK_AMI_USERNAME': ami_user,
                                                             'ASTERISK_AMI_PASSWORD': ami_password})
    docker.run('start', carrier)
    docker.sh(carrier, 'mkdir -p /faxdata/asterisk', check=True)
    docker.put(carrier, '/faxdata/asterisk/pjsip.conf', sip_trunk.render_pjsip(carrier_values))
    docker.put(carrier, '/faxdata/asterisk/iax.conf', hylafax_engine.render_iax(carrier_values, peer_secrets, lines=1))
    docker.put(carrier, '/etc/asterisk/extensions.conf',
               CARRIER_DIALPLAN.replace('@GATEWAY@', 'yes' if carrier_gateway else 'no'))
    # Proof only: SIP messages on the console (container log), to see whether T.38 was offered.
    docker.put(carrier, '/etc/asterisk/logger.conf', LOGGER)
    docker.put(asterisk, '/etc/asterisk/logger.conf', LOGGER)
    docker.run('restart', carrier)

    # The peer: one fax line on the carrier; its listener (or none) as the case needs.
    # The peer's listener is published (as docker-compose.sslfax.yml would) when the case gives it one.
    peer = docker.create('peer', images['engine'], env={'FAXBOT_SSLFAX_PUBLISHED_PORT': str(LISTENER_PORT)})
    peer_conf = hylafax_engine.render_engine_conf(
        carrier_values, peer_secrets, inbound_secret=inbound_secret, lines=1, listener=peer_listener,
        sslfax=peer_sslfax, asterisk_host=ADDRESS['carrier'], api_url='http://198.51.100.250:8080')
    docker.run('start', peer)
    docker.sh(peer, 'mkdir -p /faxdata/hylafax', check=True)
    docker.put(peer, '/faxdata/hylafax/engine.conf', peer_conf)

    for container in (asterisk, engine, api_container):
        docker.run('start', container)
    context = {'docker': docker, 'key': bootstrap, 'asterisk': asterisk, 'engine': engine, 'api': api_container,
               'carrier': carrier, 'peer': peer, 'ami_env': ami_env}
    wait_for(lambda: api(docker, 'GET', '/health', key=bootstrap)['status'] == 200, 120, 'the Faxbot API')

    def ami_connected():
        ready = api(docker, 'GET', '/health/ready', key=bootstrap)
        checks = (ready.get('json') or {}).get('checks') or {}
        return (checks.get('outbound') or {}).get('ami_connected')
    wait_for(ami_connected, 120, "Faxbot's manager connection to Asterisk")
    applied = api(docker, 'POST', '/admin/sip/apply', key=bootstrap)
    assert applied['status'] == 200, applied
    context['apply'] = applied['json']
    # Apply restarts Asterisk with the engine's lines; the engine starts once its settings exist.
    wait_for(lambda: docker.read(engine, '/faxdata/hylafax-out/engine.status').find('"running"') >= 0, 180,
             'the engine to start')
    wait_for(lambda: docker.asterisk(asterisk, 'iax2 show peers').count(' OK ') >= 2, 120,
             "the engine's two lines on Faxbot's Asterisk")
    wait_for(lambda: docker.read(peer, '/faxdata/hylafax-out/engine.status').find('"running"') >= 0, 120,
             'the peer engine to start')
    wait_for(lambda: ' OK ' in docker.asterisk(carrier, 'iax2 show peers'), 120, "the peer's line on the carrier")
    wait_for(ami_connected, 120, "Faxbot's manager connection after the restart")

    def settled():
        # Asterisk runs exactly the applied settings and no restart is waiting for an idle moment
        # (a restart in the middle of a proof call would lose that call).
        status = api(docker, 'GET', '/admin/sip/status', key=bootstrap).get('json') or {}
        return status.get('in_use') and not status.get('engine_restarting')
    wait_for(settled, 180, 'Asterisk to run the applied settings with no restart pending')

    def uptime():
        found = re.search(r'System uptime: ([0-9]+)', docker.asterisk(asterisk, 'core show uptime seconds'))
        return int(found.group(1)) if found else -1

    def steady():
        # Two readings a few seconds apart that only grow: Asterisk did not restart in between.
        first = uptime()
        time.sleep(4)
        return first >= 0 and uptime() > first and settled()
    wait_for(steady, 180, "Faxbot's Asterisk to stay up")
    wait_for(lambda: docker.asterisk(asterisk, 'iax2 show peers').count(' OK ') >= 2, 120,
             "the engine's two lines after Asterisk settled")
    docker.run('exec', '--detach', asterisk, 'bash', '-c', AMI_LISTENER)
    for container in (asterisk, carrier):
        docker.asterisk(container, 'pjsip set logger on')
    return docker, context


def session_logs(docker, container):
    script = 'cd /var/spool/hylafax/log 2>/dev/null && for f in c*; do echo "=== $f"; cat "$f"; done'
    return docker.sh(container, script).stdout


def received_pages(docker, peer, workdir):
    """The peer's newest received fax, decoded to G4 inside the peer (JBIG needs libtiff), as page bitmaps."""
    from PIL import Image, ImageSequence
    name = docker.sh(peer, 'ls -t /var/spool/hylafax/recvq/fax*.tif 2>/dev/null | head -1').stdout.strip()
    if not name:
        return None, '', []
    info = docker.sh(peer, f'faxinfo {name}').stdout
    docker.sh(peer, f'tiffcp -c g4 {name} /tmp/received-g4.tif', check=True)
    path = workdir / 'received.tif'
    path.write_bytes(docker.read_bytes(peer, '/tmp/received-g4.tif'))
    pages = [frame.convert('1').copy() for frame in ImageSequence.Iterator(Image.open(path))]
    return path, info, pages


def page_match(sent, received):
    """Rows below the sender's header line must match exactly; returns the header offset or None."""
    width = min(sent.size[0], received.size[0])
    height = min(sent.size[1], received.size[1])
    if abs(sent.size[0] - received.size[0]) > 0 or height < 400:
        return None
    for offset in range(0, 121):
        top, bottom = 140, height - 140
        if bottom - offset <= top:
            break
        a = sent.crop((0, top, width, bottom - offset)).tobytes()
        b = received.crop((0, top + offset, width, bottom)).tobytes()
        if hashlib.sha256(a).digest() == hashlib.sha256(b).digest():
            return offset
    return None


def send_and_collect(tmp_path, context):
    docker, key = context['docker'], context['key']
    pdf = proof_pdf()
    local = tmp_path / 'proof.pdf'
    local.write_bytes(pdf)
    docker.run('cp', str(local), f'{context["api"]}:/tmp/proof.pdf')
    started = time.time()
    created = api(docker, 'POST', '/fax', key=key,
                  files=({'file': ('proof.pdf', '/tmp/proof.pdf', 'application/pdf')}, {'to': PEER_NUMBER}))
    assert created['status'] == 202, created
    job_id = created['json']['id']

    def finished():
        job = api(docker, 'GET', f'/fax/{job_id}', key=key)['json'] or {}
        return job if str(job.get('status', '')).lower() not in {'queued', 'in_progress', 'sending', ''} else None
    job = wait_for(finished, 600, 'the fax result')
    elapsed = round(time.time() - started, 1)
    sent = tmp_path / 'sent.tiff'
    sent.write_bytes(docker.read_bytes(context['asterisk'], f'/faxdata/{job_id}.tiff'))
    from PIL import Image, ImageSequence
    sent_pages = [frame.convert('1').copy() for frame in ImageSequence.Iterator(Image.open(sent))]
    time.sleep(3)
    _, info, pages = received_pages(docker, context['peer'], tmp_path)
    faxbot_log = session_logs(docker, context['engine'])
    peer_log = session_logs(docker, context['peer'])
    events = parse_ami(docker.read(context['asterisk'], '/tmp/ami-events.log'))
    engine_call = next((event for event in events if event.get('UserEvent') == 'FaxEngineCall'), None)
    done = docker.sh(context['engine'], 'cat /var/spool/hylafax/doneq/q* 2>/dev/null').stdout

    def log(container):
        result = docker.run('logs', container, check=False)
        return result.stdout + result.stderr
    return {
        'job': job, 'elapsed_seconds': elapsed, 'sent_pages': sent_pages, 'received_pages': pages,
        'received_info': info, 'faxbot_log': faxbot_log, 'peer_log': peer_log, 'engine_call': engine_call,
        'done_qfile': done, 'carrier_log': log(context['carrier']), 'asterisk_log': log(context['asterisk']),
    }


def sip_evidence(text):
    """What the SIP messages on one Asterisk show: T.38 offers and answers, and the fax preference header.

    Each logged message starts with "<--- Received" or "<--- Transmitting"; an
    offer is an INVITE with a T.38 media line on a real port, an answer a
    200 OK to an INVITE with one.
    """
    offers = answers = refusals = 0
    for block in text.split('<--- ')[1:]:
        image = re.search(r'^m=image (\d+) udptl t38', block, re.MULTILINE)
        if not image or not re.search(r'^CSeq: \d+ INVITE', block, re.MULTILINE):
            continue
        if image.group(1) == '0':
            refusals += 1
        elif re.search(r'^INVITE ', block, re.MULTILINE):
            offers += 1
        elif re.search(r'^SIP/2\.0 200 OK', block, re.MULTILINE):
            answers += 1
    return {
        't38_offers': offers, 't38_answers': answers, 't38_closed_with_port_zero': refusals,
        'accept_contact': sorted(set(re.findall(r'Accept-Contact: ([^\r\n]*)', text))),
    }


def evidence(outcome):
    sent, received = outcome['sent_pages'], outcome['received_pages']
    offsets = [page_match(a, b) for a, b in zip(sent, received)]
    log = outcome['faxbot_log']
    transfer = re.findall(r'SEND FAX .*docq/.* sent in ([0-9:]+)\)', log)
    return {
        'job_status': outcome['job'].get('status'), 'job_error': outcome['job'].get('error'),
        'elapsed_seconds': outcome['elapsed_seconds'],
        'sent_pages': len(sent), 'received_pages': len(received),
        'page_sizes': [list(page.size) for page in received],
        'header_offsets': offsets,
        'received_info': {line.split(':', 1)[0].strip(): line.split(':', 1)[1].strip()
                          for line in outcome['received_info'].splitlines()[1:] if ':' in line},
        'faxbot_remote_csa': re.findall(r'REMOTE CSA "([^"]*)"', log),
        'faxbot_ssl_lines': [line.split(']: ', 1)[-1] for line in log.splitlines() if 'SSL Fax' in line][:12],
        'faxbot_dis': [line.split(']: ', 1)[-1] for line in log.splitlines()
                       if re.search(r'REMOTE (best|supports|wants|has)|internet fax|DCS|DIS', line)][:16],
        'peer_ssl_lines': [line.split(']: ', 1)[-1] for line in outcome['peer_log'].splitlines()
                           if 'SSL Fax' in line or 'internet fax' in line][:12],
        'document_transfer': transfer[-1:] if transfer else [],
        'engine_call': {key: outcome['engine_call'].get(key) for key in ('JobID', 'AttemptID', 'Gateway', 'T38',
                                                                         'T38Session',
                                                                         'Answered', 'Ended', 'Cause', 'CallID64')}
        if outcome['engine_call'] else None,
        'faxbot_sip': sip_evidence(outcome['asterisk_log']),
        'carrier_sip': sip_evidence(outcome['carrier_log']),
    }


def passcodes(text):
    """Every passcode a session log could reveal: whatever follows ssl:// up to @ (hidden ones excepted)."""
    return {code for code in re.findall(r'ssl://([^@"\s]+)@', text) if code != '(passcode hidden)'}


@pytest.fixture
def loopback(tmp_path):
    made = []

    def start(label, **case):
        _, context = bring_up(tmp_path, label, made, **case)
        return context
    yield start
    if os.environ.get('FAXBOT_PROOF_KEEP') != '1':
        for docker in made:
            docker.close()


DB_QUERY = r'''
import json, sqlite3, sys
db = sqlite3.connect('/faxdata/faxbot.db')
db.row_factory = sqlite3.Row
out = {}
for name, query in json.loads(sys.stdin.read()).items():
    out[name] = [dict(row) for row in db.execute(query)]
print(json.dumps(out, default=str))
'''


def database(context, **queries):
    """Rows from Faxbot's own database (read-only queries, run inside the API container)."""
    result = context['docker'].run('exec', '--interactive', context['api'], 'python', '-c', DB_QUERY,
                                   input_text=json.dumps(queries))
    return json.loads(result.stdout.strip().splitlines()[-1])


def records(context, job_id):
    rows = database(
        context,
        call=f"SELECT disposition, connected_seconds, t38, pages, fax_status, sip_call_id, called "
             f"FROM sip_call_records WHERE job_id = '{job_id}' AND direction = 'outbound'",
        engine=f"SELECT engine, reason, sslfax, sslfax_offered, transfer_seconds, session_seconds, signal_rate, "
               f"data_format, number FROM fax_engine_calls WHERE job_id = '{job_id}'",
        accepts=f"SELECT number, accepts, direction FROM sslfax_observations "
                f"WHERE source IN (SELECT engine_ref FROM fax_engine_calls WHERE job_id = '{job_id}')")
    return {name: (values[0] if values else None) for name, values in rows.items()}


def assert_recorded(context, outcome, proof, *, t38, sslfax):
    """The per-call record (Call-ID, connected seconds, T.38) and the engine record exist and agree."""
    found = records(context, outcome['job']['id'])
    proof['records'] = found
    call, engine = found['call'], found['engine']
    assert call and call['disposition'] == 'answered' and call['fax_status'] == 'SUCCESS', found
    assert call['sip_call_id'] and call['connected_seconds'] and call['pages'] == PAGES, found
    assert call['t38'] == ('yes' if t38 else 'no'), found
    assert engine and engine['engine'] == 'hylafax' and engine['sslfax'] == int(sslfax), found
    assert engine['transfer_seconds'] is not None and engine['number'] == PEER_NUMBER, found
    assert found['accepts'] and found['accepts']['number'] == PEER_NUMBER, found
    return found


def assert_delivered(outcome, proof):
    assert proof['job_status'].upper() == 'SUCCESS', proof
    assert proof['sent_pages'] == proof['received_pages'] == PAGES, proof
    assert all(offset is not None for offset in proof['header_offsets']), proof
    assert proof['engine_call'] and proof['engine_call']['JobID'] == outcome['job']['id'], proof
    assert proof['engine_call']['Answered'] and proof['engine_call']['CallID64'], proof
    # The passcode never reaches either engine's logs.
    assert not passcodes(outcome['faxbot_log']) and not passcodes(outcome['peer_log']), proof


def test_a_sslfax_over_an_audio_call(tmp_path, loopback):
    context = loopback('a', faxbot_t38=False, carrier_gateway=False,
                       peer_listener=f'{ADDRESS["peer"]}:{LISTENER_PORT}')
    outcome = send_and_collect(tmp_path, context)
    proof = evidence(outcome)
    print('\nSSLFAX_PROOF_A ' + json.dumps(proof, indent=2))
    assert_delivered(outcome, proof)
    assert proof['received_info'].get('SignalRate') == 'SSL Fax', proof
    assert any('SSL Fax connection was successful' in line for line in proof['faxbot_ssl_lines']), proof
    assert proof['faxbot_remote_csa'] == [f'ssl://(passcode hidden)@{ADDRESS["peer"]}:{LISTENER_PORT}'], proof
    assert proof['engine_call']['Gateway'] == 'no', proof
    # Audio end to end, and the RFC 6913 fax preference reaches the carrier exactly.
    assert proof['faxbot_sip']['t38_offers'] == proof['faxbot_sip']['t38_answers'] == 0, proof
    assert proof['carrier_sip']['accept_contact'] == ['*;+sip.fax="t38"'], proof
    assert_recorded(context, outcome, proof, t38=False, sslfax=True)


def test_b_sslfax_attempt_across_the_t38_gateways(tmp_path, loopback):
    context = loopback('b', faxbot_t38=True, carrier_gateway=True,
                       peer_listener=f'{ADDRESS["peer"]}:{LISTENER_PORT}')
    outcome = send_and_collect(tmp_path, context)
    proof = evidence(outcome)
    print('\nSSLFAX_PROOF_B ' + json.dumps(proof, indent=2))
    # Whatever the gateways do to SSL Fax, the fax itself must arrive intact.
    assert_delivered(outcome, proof)
    assert proof['engine_call']['Gateway'] == 'yes', proof
    # The trunk leg really ran T.38: the carrier offered it and Faxbot's Asterisk accepted.
    assert proof['faxbot_sip']['t38_offers'] >= 1 and proof['faxbot_sip']['t38_answers'] >= 1, proof
    # Finding (2026-10-04): SSL Fax negotiation survives two Asterisk T.38 gateways.
    assert proof['received_info'].get('SignalRate') == 'SSL Fax', proof
    # The gateway ran a T.38 fax session (its number); the trunk is back to audio by hang-up.
    assert int(proof['engine_call']['T38Session'] or 0) > 0, proof
    assert_recorded(context, outcome, proof, t38=True, sslfax=True)


def test_c_an_unreachable_listener_falls_back_to_an_ordinary_fax(tmp_path, loopback):
    context = loopback('c', faxbot_t38=False, carrier_gateway=False, peer_listener=f'{NOBODY}:{LISTENER_PORT}')
    outcome = send_and_collect(tmp_path, context)
    proof = evidence(outcome)
    print('\nSSLFAX_PROOF_C ' + json.dumps(proof, indent=2))
    assert_delivered(outcome, proof)
    assert proof['received_info'].get('SignalRate') != 'SSL Fax', proof
    assert any('Timeout waiting for SSL Fax connect' in line or 'SSL Fax connection failed' in line
               for line in proof['faxbot_ssl_lines']), proof
    found = assert_recorded(context, outcome, proof, t38=False, sslfax=False)
    # The other machine offered SSL Fax (it named its listener); it still counts as accepting it.
    assert found['engine']['sslfax_offered'] == 1 and found['accepts']['accepts'] == 1, found


def test_c2_a_private_listener_address_is_refused_and_the_fax_still_goes(tmp_path, loopback):
    context = loopback('c2', faxbot_t38=False, carrier_gateway=False, peer_listener=f'10.9.9.9:{LISTENER_PORT}')
    outcome = send_and_collect(tmp_path, context)
    proof = evidence(outcome)
    print('\nSSLFAX_PROOF_C2 ' + json.dumps(proof, indent=2))
    assert_delivered(outcome, proof)
    assert proof['received_info'].get('SignalRate') != 'SSL Fax', proof
    assert any('Refusing SSL Fax host' in line for line in proof['faxbot_ssl_lines']), proof
    assert_recorded(context, outcome, proof, t38=False, sslfax=False)


def test_d_t38_to_a_machine_without_sslfax(tmp_path, loopback):
    """T.38 on the trunk to an ordinary fax machine: no SSL Fax, T.38 end to end, and Faxbot learns that."""
    context = loopback('d', faxbot_t38=True, carrier_gateway=True, peer_listener='', peer_sslfax=False)
    outcome = send_and_collect(tmp_path, context)
    proof = evidence(outcome)
    print('\nSSLFAX_PROOF_D ' + json.dumps(proof, indent=2))
    assert_delivered(outcome, proof)
    assert proof['received_info'].get('SignalRate') != 'SSL Fax', proof
    assert proof['engine_call']['Gateway'] == 'yes' and int(proof['engine_call']['T38Session'] or 0) > 0, proof
    found = assert_recorded(context, outcome, proof, t38=True, sslfax=False)
    assert found['engine']['sslfax_offered'] == 0 and found['accepts']['accepts'] == 0, found


def test_e_a_refused_t38_request_goes_on_as_audio(tmp_path, loopback):
    """Faxbot asks for T.38 and the carrier refuses: the same call goes on as audio, never aborted."""
    context = loopback('e', faxbot_t38=True, carrier_gateway=False, carrier_t38=False,
                       peer_listener=f'{ADDRESS["peer"]}:{LISTENER_PORT}')
    outcome = send_and_collect(tmp_path, context)
    proof = evidence(outcome)
    print('\nSSLFAX_PROOF_E ' + json.dumps(proof, indent=2))
    assert_delivered(outcome, proof)
    assert proof['engine_call']['Gateway'] == 'yes', proof
    # Faxbot asked for T.38 and nobody accepted; the call went on as audio.
    assert proof['faxbot_sip']['t38_offers'] >= 1 and proof['faxbot_sip']['t38_answers'] == 0, proof
    # No T.38 fax session started (the trunk's own state is informational and may arrive empty).
    assert proof['engine_call']['T38Session'] == '0', proof
    assert_recorded(context, outcome, proof, t38=False, sslfax=True)


PEER_DOCUMENT = r"""%!PS-Adobe-3.0
%%Pages: 2
%%Page: 1 1
/Helvetica-Bold findfont 40 scalefont setfont 72 700 moveto (INBOUND PROOF PAGE 1) show
newpath 72 200 moveto 300 200 lineto 300 500 lineto 72 500 lineto closepath fill showpage
%%Page: 2 2
/Helvetica-Bold findfont 40 scalefont setfont 72 700 moveto (INBOUND PROOF PAGE 2) show
newpath 72 200 moveto 450 200 lineto 450 400 lineto 72 400 lineto closepath fill showpage
%%EOF
"""


def test_inbound_a_fax_through_the_engine_reaches_received(tmp_path, loopback):
    """The peer faxes Faxbot's number. Faxbot asks for T.38, the carrier refuses (as Telnyx did on
    2026-10-04), the call goes on as audio, the engine receives over SSL Fax (as the client of the
    peer's listener) and hands the fax to Faxbot's Received with its numbers and call record."""
    context = loopback('in', faxbot_t38=True, carrier_gateway=False, carrier_t38=False,
                       peer_listener=f'{ADDRESS["peer"]}:{LISTENER_PORT}')
    docker = context['docker']
    docker.put(context['peer'], '/tmp/inbound.ps', PEER_DOCUMENT)
    sent = docker.run('exec', context['peer'], 'sendfax', '-n', '-d', FAXBOT_NUMBER.lstrip('+'), '/tmp/inbound.ps',
                      check=False)
    assert sent.returncode == 0, sent.stderr

    def arrived():
        rows = database(context, fax="SELECT id, from_number, to_number, pages FROM inbound_faxes")['fax']
        return rows or None
    try:
        faxes = wait_for(arrived, 240, 'the fax in Received')
    except AssertionError:
        # What each side saw, for the record: the peer's sending log and Faxbot's engine log.
        print('\nSSLFAX_INBOUND_TIMEOUT peer:\n' + session_logs(docker, context['peer'])[-4000:])
        print('\nSSLFAX_INBOUND_TIMEOUT faxbot engine:\n' + session_logs(docker, context['engine'])[-4000:])
        print('\nSSLFAX_INBOUND_TIMEOUT engine container:\n' + docker.run('logs', context['engine'],
                                                                           check=False).stderr[-2000:])
        raise
    time.sleep(3)
    rows = database(
        context,
        call="SELECT call_id, did, caller, disposition, connected_seconds, t38, pages, job_id, sip_call_id "
             "FROM sip_call_records WHERE direction = 'inbound'",
        engine="SELECT engine, sslfax, sslfax_offered, transfer_seconds, number, call_key, job_id "
               "FROM fax_engine_calls WHERE direction = 'inbound'",
        accepts="SELECT number, accepts, direction FROM sslfax_observations WHERE direction = 'inbound'")
    log = session_logs(docker, context['engine'])
    engine_logs = docker.run('logs', context['engine'], check=False)
    proof = {'faxes': faxes, **rows,
             'engine_ssl_lines': [line.split(']: ', 1)[-1] for line in log.splitlines()
                                  if 'SSL Fax' in line or 'internet fax' in line][:10],
             'handover_log': [line for line in (engine_logs.stdout + engine_logs.stderr).splitlines()
                              if 'handover' in line][-4:],
             'faxbot_sip': sip_evidence(docker.run('logs', context['asterisk'], check=False).stdout)}
    print('\nSSLFAX_PROOF_INBOUND ' + json.dumps(proof, indent=2, default=str))
    fax = faxes[0]
    assert fax['from_number'] == PEER_NUMBER and fax['to_number'] == FAXBOT_NUMBER, proof
    assert fax['pages'] == 2, proof
    call = proof['call'][0]
    assert call['call_id'].startswith('engine.') and call['job_id'] == fax['id'], proof
    assert call['disposition'] == 'answered' and call['sip_call_id'] and call['connected_seconds'], proof
    assert call['t38'] == 'no', proof  # refused by the carrier: audio on the same call
    engine = proof['engine'][0]
    assert engine['engine'] == 'hylafax' and engine['sslfax'] == 1 and engine['number'] == PEER_NUMBER, proof
    assert proof['accepts'] and proof['accepts'][0]['accepts'] == 1, proof
    assert not passcodes(log), proof


def test_f_a_restart_mid_call_leaves_the_fax_uncertain_and_never_resends_it(tmp_path, loopback):
    """The engine restarts while its call is up: the fax waits for a person (uncertain), is never
    dialed again, and the engine cannot write Faxbot's data folder or Asterisk's files."""
    context = loopback('f', faxbot_t38=False, carrier_gateway=False, peer_listener='', peer_sslfax=False)
    docker, key = context['docker'], context['key']
    # The narrowed mounts: nothing of Faxbot's data folder is there, and the settings are read-only.
    mounts = docker.sh(context['engine'], 'ls /faxdata; touch /faxdata/hylafax/x 2>&1; ls /faxdata/asterisk 2>&1').stdout
    assert 'faxbot.db' not in mounts and 'Read-only file system' in mounts and 'No such file' in mounts, mounts
    local = tmp_path / 'proof.pdf'
    local.write_bytes(proof_pdf())
    docker.run('cp', str(local), f'{context["api"]}:/tmp/proof.pdf')
    created = api(docker, 'POST', '/fax', key=key,
                  files=({'file': ('proof.pdf', '/tmp/proof.pdf', 'application/pdf')}, {'to': PEER_NUMBER}))
    assert created['status'] == 202, created
    job_id = created['json']['id']
    # Restart once the engine's call is up and the pages are going.
    wait_for(lambda: 'faxbot-line' in docker.asterisk(context['asterisk'], 'core show channels concise'), 120,
             "the engine's call")
    time.sleep(8)
    docker.run('restart', '--time', '1', context['engine'])

    def state():
        rows = database(context, delivery=f"SELECT state FROM outbound_deliveries WHERE id = '{job_id}'")
        found = rows['delivery'][0]['state'] if rows['delivery'] else None
        return found if found not in (None, 'ready', 'preparing', 'submitting', 'in_progress') else None
    final = wait_for(state, 240, 'the fax to leave in progress')
    time.sleep(20)
    events = database(context, kinds=f"SELECT kind FROM outbound_events WHERE job_id = '{job_id}' "
                                     f"ORDER BY created_at")
    attempts = database(context, n=f"SELECT COUNT(*) AS n FROM outbound_attempts WHERE job_id = '{job_id}'")
    calls = [event for event in parse_ami(docker.read(context['asterisk'], '/tmp/ami-events.log'))
             if event.get('UserEvent') == 'FaxEngineCall' and event.get('JobID') == job_id]
    engine_log = docker.run('logs', context['engine'], check=False)
    proof = {'final_state': final, 'events': [row['kind'] for row in events['kinds']],
             'attempts': attempts['n'][0]['n'], 'engine_calls': len(calls),
             'engine_log': (engine_log.stdout + engine_log.stderr)[-3000:]}
    print('\nSSLFAX_PROOF_F ' + json.dumps(proof, indent=2))
    # Uncertain (waiting for a person), one attempt, one call to the peer, and the job moved aside.
    assert final == 'reconciliation_required', proof
    assert proof['attempts'] == 1 and 'submission_uncertain' in proof['events'], proof
    assert proof['engine_calls'] == 1, proof
    assert 'moved 1 unfinished job(s) aside' in proof['engine_log'], proof
