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
import shutil
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

    def create(self, name, image, *command, env=None, volumes=(), alias=None, restart=True, entrypoint=None,
               caps=()):
        container = f'{self.prefix}-{name}'
        args = ['create', '--name', container, '--network', self.network, '--ip', ADDRESS[name],
                '--label', 'com.faxbot.scope=sslfax-proof']
        for cap in caps:
            args += ['--cap-add', cap]
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


def proof_carrier_image(docker):
    """The carrier stand-in for case j: Faxbot's Asterisk plus asterisk/tests/proof-empty-preambles.patch, which
    (with FAXBOT_PROOF_EMPTY_PREAMBLES set) opens its T.38 stream with two empty V.21 preambles as Telnyx did."""
    name = os.environ.get('FAXBOT_PROOF_CARRIER_IMAGE')
    if name:
        return name
    name = f'{PREFIX}-carrier-preambles:proof'
    with tempfile.TemporaryDirectory() as folder:
        context = Path(folder) / 'asterisk'
        shutil.copytree(ROOT / 'asterisk', context)
        shutil.copy(ROOT / 'asterisk' / 'tests' / 'proof-empty-preambles.patch',
                    context / 'patches' / '9001-proof-empty-preambles.patch')
        docker.run('build', '--quiet', '--tag', name, str(context), timeout=3600)
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

CARRIER_RELAY = """exten => _[+0-9].,1,NoOp(carrier: call for ${EXTEN} to the peer)
 same => n,GotoIf($["@GATEWAY@" != "yes"]?dial)"""
CARRIER_RECEIVES = """exten => _[+0-9].,1,NoOp(carrier: answers as a T.38 fax machine)
 same => n,Answer()
 same => n,ReceiveFAX(/tmp/carrier-received.tif,f)
 same => n,Hangup()
 same => n,GotoIf($["@GATEWAY@" != "yes"]?dial)"""

NAT_STANDIN = """table inet faxbot_nat {
  chain out {
    type filter hook output priority 0; policy accept;
    udp sport 4096-4127 ct state established counter accept
    udp sport 4096-4127 counter drop
  }
}
"""

LOGGER = ('[general]\ndateformat=%F %T\n\n[logfiles]\nconsole => notice,warning,error,verbose\n'
          # Each call's dial steps, read by case k when a received call does not reach the engine.
          'calls => notice,warning,verbose(3)\n')

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
             carrier_t38=None, carrier_drops_t38=False, carrier_nat_standin=False, carrier_receives=False,
             carrier_empty_preambles=False, peer_ecm=True):
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
        # The peer's error correction: off sends pages as plain V.29 data, as the live senders did.
        'SIP_FAX_ECM': 'true' if peer_ecm else 'false',
        'ASTERISK_INBOUND_SECRET': inbound_secret})
    peer_secrets = hylafax_engine.engine_secrets(carrier_values, lines=1)
    carrier_image = images['native']
    if carrier_nat_standin:
        # The carrier's image plus nftables, for the router stand-in below.
        carrier_image = os.environ.get('FAXBOT_PROOF_PREFIX', 'faxbot-sslfax-proof') + '-natstandin:latest'
        docker.run('build', '-t', carrier_image, '-', input_text=(
            f"FROM {images['native']}\nRUN apt-get update && apt-get install -y --no-install-recommends nftables "
            "&& rm -rf /var/lib/apt/lists/*\n"), timeout=900)
    carrier_env = {'ASTERISK_AMI_USERNAME': ami_user, 'ASTERISK_AMI_PASSWORD': ami_password}
    if carrier_empty_preambles:
        carrier_image = proof_carrier_image(docker)
        carrier_env['FAXBOT_PROOF_EMPTY_PREAMBLES'] = '1'
    carrier = docker.create('carrier', carrier_image,
                            caps=('NET_ADMIN',) if carrier_drops_t38 or carrier_nat_standin else (),
                            env=carrier_env)
    docker.run('start', carrier)
    docker.sh(carrier, 'mkdir -p /faxdata/asterisk', check=True)
    docker.put(carrier, '/faxdata/asterisk/pjsip.conf', sip_trunk.render_pjsip(carrier_values))
    docker.put(carrier, '/faxdata/asterisk/iax.conf', hylafax_engine.render_iax(carrier_values, peer_secrets, lines=1))
    dialplan = CARRIER_DIALPLAN
    if carrier_receives:
        # The carrier answers as a T.38 fax machine, as Telnyx did on 5 October: ReceiveFAX plays three
        # seconds of answer tone, then asks Faxbot for T.38 itself, and Faxbot answers the re-INVITE.
        assert CARRIER_RELAY in dialplan
        dialplan = dialplan.replace(CARRIER_RELAY, CARRIER_RECEIVES)
    docker.put(carrier, '/etc/asterisk/extensions.conf',
               dialplan.replace('@GATEWAY@', 'yes' if carrier_gateway else 'no'))
    # Proof only: SIP messages on the console (container log), to see whether T.38 was offered.
    docker.put(carrier, '/etc/asterisk/logger.conf', LOGGER)
    docker.put(asterisk, '/etc/asterisk/logger.conf', LOGGER)
    if carrier_drops_t38 or carrier_nat_standin:
        # Like the live call of 5 October: the carrier accepts T.38, but none of its T.38 data reaches
        # Faxbot. Its T.38 ports are one block (4096-4127) and its network drops every packet sent from them.
        docker.put(carrier, '/etc/asterisk/udptl.conf', '[general]\nudptlstart=4096\nudptlend=4127\n'
                   'udptlchecksums=no\nudptlfecentries=3\nudptlfecspan=3\n')
    docker.run('restart', carrier)
    if carrier_drops_t38:
        docker.sh(carrier, 'tc qdisc add dev eth0 root handle 1: prio && tc filter add dev eth0 parent 1: '
                           'protocol ip prio 1 u32 match ip protocol 17 0xff match ip sport 4096 0xffe0 action drop',
                  check=True)
    if carrier_nat_standin:
        # A router in front of Faxbot that keeps port numbers and forwards none (the owner's network): the
        # carrier's T.38 packets get through only on a flow Faxbot opened by sending from its side first.
        docker.put(carrier, '/tmp/nat-standin.nft', NAT_STANDIN)
        docker.sh(carrier, 'nft -f /tmp/nat-standin.nft', check=True)

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
    # The engine channel's event for this fax (the trunk channel sends its own, Side: trunk).
    engine_call = next((event for event in events if event.get('UserEvent') == 'FaxEngineCall'
                        and event.get('Side') == 'engine' and event.get('JobID') == job_id), None)
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
    offers = answers = refusals = received = sent = 0
    for block in text.split('<--- ')[1:]:
        image = re.search(r'^m=image (\d+) udptl t38', block, re.MULTILINE)
        if not image or not re.search(r'^CSeq: \d+ INVITE', block, re.MULTILINE):
            continue
        if image.group(1) == '0':
            refusals += 1
        elif re.search(r'^INVITE ', block, re.MULTILINE):
            offers += 1
            received += block.startswith('Received')
            sent += block.startswith('Transmitting')
        elif re.search(r'^SIP/2\.0 200 OK', block, re.MULTILINE):
            answers += 1
    return {
        't38_offers': offers, 't38_answers': answers, 't38_closed_with_port_zero': refusals,
        't38_offers_received': received, 't38_offers_sent': sent,
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
    if str(proof['job_status']).upper() != 'SUCCESS':
        # Both engines' own words, for the record, when the fax did not go through.
        print('\nSSLFAX_PROOF_E_FAXBOT_SESSION\n' + outcome['faxbot_log'][-4000:])
        print('\nSSLFAX_PROOF_E_PEER_SESSION\n' + outcome['peer_log'][-4000:])
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


def test_inbound_t38_a_fax_received_over_t38_reaches_received(tmp_path, loopback):
    """The peer faxes Faxbot's number over T.38 on both Asterisks, without error correction, so its training
    check and pages reach the engine as V.29 data through Faxbot's T.38 gateway, as on 6 October (call 2).
    The fax arrives whole, and asterisk/patches/0003 does not act on a call whose signals all end normally."""
    context = loopback('it', faxbot_t38=True, carrier_gateway=True, peer_listener='', peer_sslfax=False,
                       peer_ecm=False)
    docker = context['docker']
    docker.put(context['peer'], '/tmp/inbound.ps', PEER_DOCUMENT)
    sent = docker.run('exec', context['peer'], 'sendfax', '-n', '-d', FAXBOT_NUMBER.lstrip('+'), '/tmp/inbound.ps',
                      check=False)
    assert sent.returncode == 0, sent.stderr

    def arrived():
        rows = database(context, fax="SELECT id, from_number, to_number, pages FROM inbound_faxes")['fax']
        return rows or None
    try:
        faxes = wait_for(arrived, 300, 'the fax in Received')
    except AssertionError:
        print('\nSSLFAX_INBOUND_T38_TIMEOUT peer:\n' + session_logs(docker, context['peer'])[-4000:])
        print('\nSSLFAX_INBOUND_T38_TIMEOUT faxbot engine:\n' + session_logs(docker, context['engine'])[-4000:])
        raise
    time.sleep(3)
    rows = database(context, call="SELECT call_id, t38, pages, fax_status FROM sip_call_records "
                                  "WHERE direction = 'inbound'")
    log = session_logs(docker, context['engine'])
    asterisk_log = docker.run('logs', context['asterisk'], check=False).stdout
    proof = {'faxes': faxes, **rows,
             'engine_training': [line.split(']: ', 1)[-1] for line in log.splitlines()
                                 if 'TCF' in line or 'TRAINING' in line or 'training' in line][:8],
             'open_signals_ended': asterisk_log.count('fast modem signal ended with its data still open'),
             'empty_preambles_ended': asterisk_log.count("the far end's V.21 signal ended with no frame"),
             't38_lines': t38_lines(asterisk_log)}
    print('\nSSLFAX_PROOF_INBOUND_T38 ' + json.dumps(proof, indent=2, default=str))
    fax = faxes[0]
    assert fax['from_number'] == PEER_NUMBER and fax['to_number'] == FAXBOT_NUMBER and fax['pages'] == 2, proof
    assert proof['call'][0]['t38'] == 'yes', proof
    assert any('TRAINING succeeded' in line for line in proof['engine_training']), proof
    assert proof['open_signals_ended'] == 0, proof
    assert proof['t38_lines'] and all(line[4] == 'engaged' for line in proof['t38_lines']), proof


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
    # The interrupted call ends when the other machine gives up; then nothing dials this fax again.
    wait_for(lambda: 'faxbot-line' not in docker.asterisk(context['asterisk'], 'core show channels concise'), 240,
             'the interrupted call to end')
    time.sleep(30)
    events = database(context, kinds=f"SELECT kind FROM outbound_events WHERE job_id = '{job_id}' "
                                     f"ORDER BY created_at")
    attempts = database(context, n=f"SELECT COUNT(*) AS n FROM outbound_attempts WHERE job_id = '{job_id}'")
    calls = [event for event in parse_ami(docker.read(context['asterisk'], '/tmp/ami-events.log'))
             if event.get('UserEvent') == 'FaxEngineCall' and event.get('Side') == 'engine'
             and event.get('JobID') == job_id]
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


def test_g_a_t38_call_with_no_t38_data_back_moves_only_the_engine_to_audio(tmp_path, loopback):
    """The live failure of 5 October, reproduced: T.38 is agreed and nothing comes back. The engine heard no
    fax machine; the call says so in the engine's words, only the engine switches to audio fax, and its
    next fax goes through as audio on the same network."""
    context = loopback('g', faxbot_t38=True, carrier_gateway=True, peer_listener='', peer_sslfax=False,
                       carrier_drops_t38=True)
    docker, key = context['docker'], context['key']
    first = send_and_collect(tmp_path, context)
    found = records(context, first['job']['id'])
    mode = docker.read(context['api'], '/faxdata/hylafax/engine-t38')
    trunk = api(docker, 'GET', '/admin/sip/status', key=key)['json'] or {}
    settings = api(docker, 'GET', '/admin/settings', key=key)['json'] or {}
    proof = {'job_status': first['job'].get('status'), 'job_error': first['job'].get('error'),
             'call': found['call'], 'engine_t38': mode, 'engine_text': trunk.get('engine_text'),
             'installation_t38': settings.get('sip', {}).get('trunk', {}).get('t38_enabled'),
             'engine_call': first['engine_call'], 'faxbot_log': first['faxbot_log'][-1500:]}
    print('\nSSLFAX_PROOF_G1 ' + json.dumps(proof, indent=2, default=str))
    assert str(proof['job_status']).lower() == 'failed', proof
    assert proof['job_error'] == 'The call connected but the fast fax service heard no fax machine on the line.'
    assert found['call']['t38'] == 'yes' and found['call']['pages'] == 0, proof
    assert '"mode": "audio"' in mode and proof['installation_t38'] is True, proof
    assert proof['engine_call'] and proof['engine_call'].get('GwStatus'), proof
    # The next fax: the engine places it as audio and it goes through.
    tmp_second = tmp_path / 'second'
    tmp_second.mkdir()
    second = send_and_collect(tmp_second, context)
    found = records(context, second['job']['id'])
    print('\nSSLFAX_PROOF_G2 ' + json.dumps({'job_status': second['job'].get('status'), 'call': found['call']},
                                             indent=2, default=str))
    assert str(second['job'].get('status')).upper() == 'SUCCESS', second['job']
    assert found['call']['t38'] == 'no' and found['call']['pages'] == PAGES, found


T38_LINE = re.compile(r'Faxbot T\.38 on (PJSIP/\S+): 3 opening packets to ([0-9.]+:\d+); (\d+) packets sent, '
                      r'(\d+) received; T\.38 gateway (\S+)')


def t38_lines(log):
    """Asterisk's NOTICE line for each T.38 stream (asterisk/patches/0001): channel, opened to, sent, received,
    whether the gateway engaged."""
    return [match.groups() for match in T38_LINE.finditer(log)]


def nft_counters(text):
    """(packets let through, packets dropped) from the router stand-in's rules."""
    passed = re.search(r'ct state established counter packets (\d+)', text)
    dropped = re.search(r'udp sport 4096-4127 counter packets (\d+) bytes \d+ drop', text)
    return (int(passed.group(1)) if passed else None, int(dropped.group(1)) if dropped else None)


def test_h_t38_through_a_router_that_keeps_ports_works_once_faxbot_sends_first(tmp_path, loopback):
    """The fix for the live call of 5 October: behind a router that keeps ports and forwards none, the
    carrier's T.38 data gets in only after Faxbot sends from its T.38 port. Asterisk now sends three T.38
    no-signal packets as soon as a T.38 stream starts (asterisk/patches/0001-t38-send-first.patch), so the
    engine's T.38 fax goes through; with the unpatched image the same case fails like the live call."""
    context = loopback('h', faxbot_t38=True, carrier_gateway=True, peer_listener='', peer_sslfax=False,
                       carrier_nat_standin=True)
    outcome = send_and_collect(tmp_path, context)
    found = records(context, outcome['job']['id'])
    passed, dropped = nft_counters(context['docker'].sh(context['carrier'], 'nft list table inet faxbot_nat').stdout)
    proof = evidence(outcome)
    proof.update({'records': found, 'standin_passed': passed, 'standin_dropped': dropped,
                  't38_lines': t38_lines(outcome['asterisk_log'])})
    print('\nSSLFAX_PROOF_H ' + json.dumps(proof, indent=2, default=str))
    assert_delivered(outcome, proof)
    assert found['call']['t38'] == 'yes' and found['call']['pages'] == PAGES, proof
    # The stand-in let the carrier's T.38 data in only after Faxbot had sent first.
    assert passed and passed > 0, proof
    # One line for the stream says where the opening packets went, the counts, and that the gateway engaged.
    [(_, opened, sent, received, gateway)] = proof['t38_lines']
    assert opened.startswith(ADDRESS['carrier'] + ':') and int(sent) > 3 and int(received) > 0, proof
    assert gateway == 'engaged', proof


def test_i_t38_offered_by_the_carrier_behind_a_router_that_keeps_ports(tmp_path, loopback):
    """The direction of the live call: the far end answers as a T.38 fax machine and asks Faxbot for T.38
    three seconds later (ReceiveFAX, like Telnyx on 5 October); Faxbot answers, and its first packets let
    the far end's T.38 data in through the router stand-in."""
    context = loopback('i', faxbot_t38=True, carrier_gateway=False, carrier_t38=True, peer_listener='',
                       peer_sslfax=False, carrier_nat_standin=True, carrier_receives=True)
    docker, key = context['docker'], context['key']
    local = tmp_path / 'proof.pdf'
    local.write_bytes(proof_pdf())
    docker.run('cp', str(local), f'{context["api"]}:/tmp/proof.pdf')
    created = api(docker, 'POST', '/fax', key=key,
                  files=({'file': ('proof.pdf', '/tmp/proof.pdf', 'application/pdf')}, {'to': PEER_NUMBER}))
    assert created['status'] == 202, created
    job_id = created['json']['id']

    def finished():
        job = api(docker, 'GET', f'/fax/{job_id}', key=key)['json'] or {}
        return job if str(job.get('status', '')).lower() not in {'queued', 'in_progress', 'sending', ''} else None
    job = wait_for(finished, 600, 'the fax result')
    found = records(context, job_id)
    passed, dropped = nft_counters(docker.sh(context['carrier'], 'nft list table inet faxbot_nat').stdout)
    size = docker.sh(context['carrier'], 'stat -c %s /tmp/carrier-received.tif 2>/dev/null').stdout
    faxbot_log = docker.run('logs', context['asterisk'], check=False).stdout
    faxbot_sip = sip_evidence(faxbot_log)
    proof = {'job_status': job.get('status'), 'job_error': job.get('error'), 'call': found['call'],
             'carrier_image_bytes': size.strip(), 'standin_passed': passed, 'standin_dropped': dropped,
             'faxbot_sip': faxbot_sip, 't38_lines': t38_lines(faxbot_log)}
    print('\nSSLFAX_PROOF_I ' + json.dumps(proof, indent=2, default=str))
    assert str(job.get('status')).upper() == 'SUCCESS', proof
    assert found['call']['t38'] == 'yes' and found['call']['pages'] == PAGES, proof
    # The live direction: the carrier offered T.38 and Faxbot answered it.
    assert faxbot_sip['t38_offers_received'] >= 1 and faxbot_sip['t38_answers'] >= 1, proof
    assert passed and passed > 0 and int(size.strip() or 0) > 0, proof
    [(_, opened, sent, received, gateway)] = proof['t38_lines']
    assert int(sent) > 3 and int(received) > 0 and gateway == 'engaged', proof


def test_j_t38_that_opens_with_empty_v21_preambles_still_reaches_the_engine(tmp_path, loopback):
    """The live failure of 5 October that asterisk/patches/0002 fixed: the carrier's T.38 opens with two V.21
    preambles that end with no frame (00, 06, c00110, 00, 06, c00110, 00, then the far machine's DIS). With 0002
    the engine hears the DIS and the fax goes through; on an image without 0002 (FAXBOT_NATIVE_IMAGE) the
    gateway keeps sending V.21 flags to the engine and the fax fails like the live calls."""
    context = loopback('j', faxbot_t38=True, carrier_gateway=True, peer_listener='', peer_sslfax=False,
                       carrier_empty_preambles=True)
    outcome = send_and_collect(tmp_path, context)
    proof = evidence(outcome)
    proof.update({'carrier_empty_preambles': outcome['carrier_log'].count(
                      'Faxbot proof: sent two empty V.21 preambles'),
                  'faxbot_ended_preambles': outcome['asterisk_log'].count(
                      "the far end's V.21 signal ended with no frame"),
                  't38_lines': t38_lines(outcome['asterisk_log'])})
    print('\nSSLFAX_PROOF_J ' + json.dumps(proof, indent=2, default=str))
    assert proof['carrier_empty_preambles'] >= 1, proof
    assert_delivered(outcome, proof)
    assert proof['faxbot_ended_preambles'] >= 1, proof


ENGINE_LINES = 2


def engine_lines(docker, engine):
    """The engine's modems and the UDP ports open on all addresses: one modem per line, each on its port."""
    modems = docker.run('exec', engine, 'pgrep', '-a', '-x', 'iaxmodem', check=False).stdout.splitlines()
    udp = docker.read(engine, '/proc/net/udp').splitlines()[1:]
    ports = sorted({int(line.split()[1].split(':')[1], 16) for line in udp
                    if line.split()[1].startswith('00000000:')})
    return sorted(line.split(' ', 1)[1] for line in modems if line.strip()), ports


def dial_lines(docker, asterisk):
    """Asterisk's lines about the engine's fax lines and received calls (dial steps, IAX, fax)."""
    log = docker.run('exec', '-u', 'root', asterisk, 'cat', '/var/log/asterisk/calls', check=False).stdout
    keep = ('IAX2/faxbot', 'chan_iax2', 'Dial(', 'ReceiveFAX', 'is ringing', 'answered', 'busy', 'No one',
            'congest', 'Everyone', 'faxbot-engine', 'Registered IAX2', 'Unregistered')
    return '\n'.join(line[:260] for line in log.splitlines() if any(word in line for word in keep))[-6000:]


def receive_through_the_engine(context, number):
    """The peer faxes Faxbot; the fax must arrive in Received through the engine (its call record says so)."""
    docker = context['docker']
    before = len(database(context, fax='SELECT id FROM inbound_faxes')['fax'])
    docker.put(context['peer'], '/tmp/inbound.ps', PEER_DOCUMENT)
    sent = docker.run('exec', context['peer'], 'sendfax', '-n', '-d', FAXBOT_NUMBER.lstrip('+'), '/tmp/inbound.ps',
                      check=False)
    assert sent.returncode == 0, sent.stderr

    def arrived():
        rows = database(context, fax='SELECT id, pages FROM inbound_faxes ORDER BY created_at')['fax']
        return rows[before:] if len(rows) > before else None
    try:
        (fax,) = wait_for(arrived, 300, f'received fax {number}')
    except AssertionError:
        print(f'\nSSLFAX_RESTART_TIMEOUT {number} engine:\n' + session_logs(docker, context['engine'])[-3000:])
        engine_log = docker.run('logs', '--timestamps', context['engine'], check=False)
        print(f'\nSSLFAX_RESTART_TIMEOUT {number} engine container:\n' + (engine_log.stdout + engine_log.stderr)[-2500:])
        print(f'\nSSLFAX_RESTART_TIMEOUT {number} peer:\n' + session_logs(docker, context['peer'])[-2000:])
        print(f'\nSSLFAX_RESTART_TIMEOUT {number} modem logs:\n' + docker.run(
            'exec', '-u', 'root', context['engine'], 'tail', '-n', '12', '/var/log/iaxmodem/ttyIAX1.log',
            '/var/log/iaxmodem/ttyIAX2.log', check=False).stdout)
        print(f'\nSSLFAX_RESTART_TIMEOUT {number} asterisk:\n' + dial_lines(docker, context['asterisk']))
        raise
    time.sleep(3)
    call = database(context, call=f"SELECT call_id, fax_status FROM sip_call_records WHERE job_id = '{fax['id']}'")
    if not (call['call'] and call['call'][0]['call_id'].startswith('engine.')):
        # Not through the engine: what the engine and Asterisk said, for the record.
        engine_log = docker.run('logs', '--timestamps', context['engine'], check=False)
        print(f'\nSSLFAX_RESTART_BUILTIN {number} engine container:\n' + (engine_log.stdout + engine_log.stderr)[-2500:])
        print(f'\nSSLFAX_RESTART_BUILTIN {number} modem logs:\n' + docker.run(
            'exec', '-u', 'root', context['engine'], 'tail', '-n', '12', '/var/log/iaxmodem/ttyIAX1.log',
            '/var/log/iaxmodem/ttyIAX2.log', check=False).stdout)
        print(f'\nSSLFAX_RESTART_BUILTIN {number} asterisk:\n' + dial_lines(docker, context['asterisk']))
        print(f'\nSSLFAX_RESTART_BUILTIN {number} peers:\n' + docker.asterisk(context['asterisk'], 'iax2 show peers'))
    return {'fax': fax, 'call': call['call']}


def engine_running_again(context, since):
    docker = context['docker']

    def running():
        text = docker.read(context['engine'], '/faxdata/hylafax-out/engine.status')
        try:
            status = json.loads(text)
        except ValueError:
            return False
        return status.get('state') == 'running' and int(status.get('started') or 0) >= since
    wait_for(running, 180, 'the engine to run again')
    wait_for(lambda: docker.asterisk(context['asterisk'], 'iax2 show peers').count(' OK ') >= ENGINE_LINES, 120,
             "the engine's lines on Faxbot's Asterisk")
    docker.asterisk(context['asterisk'], 'core set verbose 3')  # each call's dial steps in the log


def test_k_after_an_engine_restart_and_a_fresh_start_received_calls_still_reach_the_engine(tmp_path, loopback):
    """Live, 6 October 2026: every fax line ran as two modems (`iaxmodem -F <file>` starts one for every line),
    so calls rang where nothing answered and went to Asterisk's own fax engine after 20 s. Now one modem per
    line on its own port, at start, after the engine restarts, and after the engine and Asterisk start afresh,
    and a received call reaches the engine each time."""
    context = loopback('k', faxbot_t38=True, carrier_gateway=False, carrier_t38=False, peer_listener='',
                       peer_sslfax=False)
    docker = context['docker']
    docker.asterisk(context['asterisk'], 'core set verbose 3')
    proof = {'lines_at_start': engine_lines(docker, context['engine'])}
    proof['first'] = receive_through_the_engine(context, 1)
    started = int(time.time())
    docker.run('restart', context['engine'])
    engine_running_again(context, started)
    proof['lines_after_engine_restart'] = engine_lines(docker, context['engine'])
    proof['second'] = receive_through_the_engine(context, 2)
    started = int(time.time())
    docker.run('restart', context['asterisk'], context['engine'])
    engine_running_again(context, started)
    proof['lines_after_fresh_start'] = engine_lines(docker, context['engine'])
    proof['third'] = receive_through_the_engine(context, 3)
    print('\nSSLFAX_PROOF_K ' + json.dumps(proof, indent=2, default=str))
    expected = ([f'iaxmodem ttyIAX{number}' for number in range(1, ENGINE_LINES + 1)],
                [4569 + number for number in range(1, ENGINE_LINES + 1)])
    for moment in ('lines_at_start', 'lines_after_engine_restart', 'lines_after_fresh_start'):
        assert tuple(proof[moment]) == expected, proof
    for name in ('first', 'second', 'third'):
        assert proof[name]['fax']['pages'] == 2, proof
        assert proof[name]['call'] and proof[name]['call'][0]['call_id'].startswith('engine.'), proof
