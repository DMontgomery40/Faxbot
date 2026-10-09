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
               caps=(), service=None):
        container = f'{self.prefix}-{name}'
        args = ['create', '--name', container, '--network', self.network, '--ip', ADDRESS[name],
                '--label', 'com.faxbot.scope=sslfax-proof']
        # The service's own privileges from docker-compose.yml (cap_drop ALL, its few capabilities,
        # no-new-privileges), so the proof runs the containers exactly as an installation does.
        args += compose_security(service) if service else []
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
        """Write a file as root (mode 0600), as Faxbot writes its settings. A plain "docker cp" keeps this
        computer's user ID, which root in a container without a file override (docker-compose.yml) cannot
        read; a tar stream names root. ``text`` may be bytes (a fax image)."""
        import tarfile
        data = text if isinstance(text, bytes) else text.encode()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            entry = tarfile.TarInfo(os.path.basename(path))
            entry.size, entry.mode, entry.uid, entry.gid, entry.mtime = len(data), 0o600, 0, 0, int(time.time())
            tar.addfile(entry, io.BytesIO(data))
        result = subprocess.run(self.base + ['cp', '-', f'{container}:{os.path.dirname(path)}'],
                                input=archive.getvalue(), capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f'docker cp failed: {result.stderr.decode(errors="replace")[-2000:]}')

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


def compose_security(service):
    """docker create flags for a service's cap_drop, cap_add and security_opt in docker-compose.yml."""
    import yaml
    spec = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services'][service]
    flags = []
    for key, flag in (('cap_drop', '--cap-drop'), ('cap_add', '--cap-add'), ('security_opt', '--security-opt')):
        for value in spec.get(key) or []:
            flags += [flag, value]
    return flags


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

# Asterisk's events, collected in the API container: the manager port accepts only the API's address
# (docker-compose.yml, start.sh), so a listener inside Asterisk's container is refused.
AMI_LISTENER = r'''
exec 3<>/dev/tcp/asterisk/5038
printf 'Action: Login\r\nUsername: %s\r\nSecret: %s\r\nEvents: user\r\n\r\n' \
  "$ASTERISK_AMI_USERNAME" "$ASTERISK_AMI_PASSWORD" >&3
deadline=$((SECONDS + 900))
while [ "$SECONDS" -lt "$deadline" ]; do
  if IFS= read -r -t 5 line <&3; then printf '%s\n' "$line" >> /tmp/ami-events.log; elif [ $? -le 128 ]; then break; fi
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
             carrier_empty_preambles=False, peer_ecm=True, api_extra=None):
    """Start the whole loopback; returns (docker, context dict). ``made`` collects it for cleanup at once.

    ``api_extra`` adds settings to Faxbot's own (case o: a second trunk number for the reply number).
    """
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
    # The manager port accepts only the API's address, as docker-compose.yml sets it.
    asterisk = docker.create('asterisk', images['native'], env={**ami_env, 'FAXBOT_API_ADDRESS': ADDRESS['api']},
                             volumes=[(faxdata, '/faxdata'), (settings_volume, '/faxdata/hylafax')], alias='asterisk',
                             service='asterisk')
    engine = docker.create('hylafax', images['engine'], alias='hylafax', service='hylafax',
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
        **(api_extra or {}),
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
    carrier = docker.create('carrier', carrier_image, service='asterisk',
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
    peer = docker.create('peer', images['engine'], env={'FAXBOT_SSLFAX_PUBLISHED_PORT': str(LISTENER_PORT)},
                         service='hylafax')
    peer_conf = hylafax_engine.render_engine_conf(
        carrier_values, peer_secrets, report_secret=peer_secrets['report_secret'], lines=1, listener=peer_listener,
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
    # Every daemon runs exactly once, with the command line its installed version reads (a second modem for a
    # line, live on 6 October 2026, still looked healthy to Asterisk), and each line holds only its own port.
    engine_daemons = lambda lines: sorted(  # noqa: E731
        [f'iaxmodem ttyIAX{n}' for n in range(1, lines + 1)] + [f'faxgetty -D ttyIAX{n}' for n in range(1, lines + 1)]
        + ['faxq', 'hfaxd -i 4559', 'busybox syslogd -n -s 0 -f /etc/faxbot-syslog.conf'])
    expected = {
        asterisk: ['asterisk -f -C /etc/asterisk/asterisk.conf'],
        carrier: ['asterisk -f -C /etc/asterisk/asterisk.conf'],
        engine: engine_daemons(2), peer: engine_daemons(1),
        api_container: ['/usr/local/bin/python3.11 /usr/local/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080'],
    }
    for container, commands in expected.items():
        names = {command.split()[1 if 'python' in command.split()[0] else 0].rsplit('/', 1)[-1]
                 for command in commands}
        # A second daemon stays; hfaxd's own child for one client (the engine's status check runs faxstat every
        # few seconds) is gone within moments, so the list must settle to exactly one of each.
        try:
            wait_for(lambda: daemons(docker, container, names) == commands, 20, f'one of each daemon in {container}')
        except AssertionError:
            assert daemons(docker, container, names) == commands, (container, daemons(docker, container, names))
    for container, lines in ((engine, 2), (peer, 1)):
        assert udp_ports(docker, container) == [4569 + n for n in range(1, lines + 1)], container
    docker.run('exec', '--detach', api_container, 'bash', '-c', AMI_LISTENER)
    for container in (asterisk, carrier):
        docker.asterisk(container, 'pjsip set logger on')
    return docker, context


PROCESSES = ('for p in /proc/[0-9]*; do [ -r "$p/cmdline" ] || continue; '
             'tr "\\000" "\\037" < "$p/cmdline"; echo; done')


def daemons(docker, container, names):
    """The command lines (sorted) of the processes in ``container`` running one of the programs ``names``
    (by program name; a Python program by its script name), whatever started them."""
    found = []
    for line in docker.run('exec', '-u', 'root', container, 'sh', '-c', PROCESSES, check=False).stdout.splitlines():
        argv = [part for part in line.split('\x1f') if part]
        if not argv:
            continue
        program = argv[1] if argv[0].rsplit('/', 1)[-1].startswith('python') and len(argv) > 1 else argv[0]
        if program.rsplit('/', 1)[-1] in names:
            found.append(' '.join(argv))
    return sorted(found)


def udp_ports(docker, container):
    """UDP ports open on all addresses in ``container``, one entry per socket (a port held twice shows twice)."""
    table = docker.run('exec', '-u', 'root', container, 'cat', '/proc/net/udp', check=False).stdout.splitlines()[1:]
    return sorted(int(line.split()[1].split(':')[1], 16) for line in table if line.split()[1].startswith('00000000:'))


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


def send_and_collect(tmp_path, context, document=None):
    docker, key = context['docker'], context['key']
    pdf = document or proof_pdf()
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
    events = parse_ami(docker.read(context['api'], '/tmp/ami-events.log'))
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
    calls = [event for event in parse_ami(docker.read(context['api'], '/tmp/ami-events.log'))
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
    assert proof['job_error'] == 'The call connected but the fax engine heard no fax machine on the line.'
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


# The same line, when a gateway engaged: how many signals the far end left open the gateway ended (0002, 0003).
T38_ENDED = re.compile(r'T\.38 gateway engaged \(it ended (\d+) empty V\.21 signals and (\d+) open fast modem '
                       r'signals from the far end\)')


def t38_ended(log):
    return [tuple(int(count) for count in match.groups()) for match in T38_ENDED.finditer(log)]


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
                  't38_lines': t38_lines(outcome['asterisk_log']),
                  't38_ended': t38_ended(outcome['asterisk_log'])})
    print('\nSSLFAX_PROOF_J ' + json.dumps(proof, indent=2, default=str))
    assert proof['carrier_empty_preambles'] >= 1, proof
    assert_delivered(outcome, proof)
    # The far end decides how often this comes, so the gateway logs only the first one in the call; 0001's
    # line at the end of the call counts every one it ended.
    assert proof['faxbot_ended_preambles'] == 1, proof
    [(preambles, trainings)] = proof['t38_ended']
    assert preambles >= 1 and trainings == 0, proof


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
    # Asterisk's own log file (its user's, mode 0600; root in the container has no file override).
    log = docker.run('exec', '-u', 'asterisk', asterisk, 'cat', '/var/log/asterisk/calls', check=False).stdout
    keep = ('IAX2/faxbot', 'chan_iax2', 'Dial(', 'ReceiveFAX', 'is ringing', 'answered', 'busy', 'No one',
            'congest', 'Everyone', 'faxbot-engine', 'Registered IAX2', 'Unregistered')
    return '\n'.join(line[:260] for line in log.splitlines() if any(word in line for word in keep))[-6000:]


def answered_lines(docker, asterisk):
    """The engine line that answered each received call so far, in order (Asterisk's dial steps)."""
    # Asterisk's own log file (its user's, mode 0600; root in the container has no file override).
    log = docker.run('exec', '-u', 'asterisk', asterisk, 'cat', '/var/log/asterisk/calls', check=False).stdout
    return [int(found.group(1)) for found in re.finditer(r'IAX2/faxbot-line([0-9]+)-[0-9]+ answered', log)]


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
    seen = len(answered_lines(docker, context['asterisk']))
    proof['first'] = receive_through_the_engine(context, 1)
    proof['first_lines'], seen = answered_lines(docker, context['asterisk'])[seen:], len(
        answered_lines(docker, context['asterisk']))
    # A modem lock left in the container by a restart, naming a process ID that is alive after it (live,
    # 6 October 2026: line 1 then waited on it for good). The engine clears it and both lines take calls.
    docker.run('exec', '-u', 'uucp', context['engine'], 'sh', '-c',
               'rm -f /run/lock/LCK..ttyIAX1; printf "%10d\\n" 1 > /run/lock/LCK..ttyIAX1')
    started = int(time.time())
    docker.run('restart', context['engine'])
    engine_running_again(context, started)
    proof['lock_after_restart'] = docker.run('exec', '-u', 'root', context['engine'], 'ls', '/run/lock',
                                             check=False).stdout.split()
    proof['faxstat_after_restart'] = docker.run('exec', '-u', 'root', context['engine'], 'faxstat', '-s',
                                                check=False).stdout
    proof['lines_after_engine_restart'] = engine_lines(docker, context['engine'])
    proof['second'] = receive_through_the_engine(context, 2)
    proof['second_lines'] = answered_lines(docker, context['asterisk'])[seen:]
    started = int(time.time())
    docker.run('restart', context['asterisk'], context['engine'])
    engine_running_again(context, started)
    proof['lines_after_fresh_start'] = engine_lines(docker, context['engine'])
    seen = len(answered_lines(docker, context['asterisk']))  # the log stays across Asterisk's restart
    proof['third'] = receive_through_the_engine(context, 3)
    proof['third_lines'] = answered_lines(docker, context['asterisk'])[seen:]
    print('\nSSLFAX_PROOF_K ' + json.dumps(proof, indent=2, default=str))
    expected = ([f'iaxmodem ttyIAX{number}' for number in range(1, ENGINE_LINES + 1)],
                [4569 + number for number in range(1, ENGINE_LINES + 1)])
    for moment in ('lines_at_start', 'lines_after_engine_restart', 'lines_after_fresh_start'):
        assert tuple(proof[moment]) == expected, proof
    assert 'LCK..ttyIAX1' not in proof['lock_after_restart'], proof
    assert proof['faxstat_after_restart'].count(': Running and idle') == ENGINE_LINES, proof
    # HylaFAX's own server messages now reach the container log (a syslog relay in the engine).
    engine_log = docker.run('logs', context['engine'], check=False)
    assert 'FaxGetty[' in engine_log.stdout + engine_log.stderr, proof
    assert 'runuser' not in engine_log.stdout + engine_log.stderr, proof
    for name in ('first', 'second', 'third'):
        assert proof[name]['fax']['pages'] == 2, proof
        assert proof[name]['call'] and proof[name]['call'][0]['call_id'].startswith('engine.'), proof
        # The lines are tried in turn, first free line first: line 1 takes each call, including the first
        # call after the stale lock on line 1.
        assert proof[f'{name}_lines'] == [1], proof


def test_l_a_fax_call_no_free_line_answers_restarts_the_engine_by_itself(tmp_path, loopback):
    """A free engine line that rings and does not answer: the built-in engine takes that fax within 20 s,
    Faxbot asks the engine to start again, the engine restarts by itself once no fax is going through, the
    trunk page says what happened, and the next received call reaches line 1 through the engine."""
    from datetime import datetime
    context = loopback('l', faxbot_t38=True, carrier_gateway=False, carrier_t38=False, peer_listener='',
                       peer_sslfax=False)
    docker = context['docker']
    docker.asterisk(context['asterisk'], 'core set verbose 3')
    # Both lines stay registered and idle but no longer answer (HylaFAX's own setting, changed only in the
    # running faxgetty; the engine's start writes it back).
    for number in range(1, ENGINE_LINES + 1):
        docker.run('exec', '-u', 'uucp', context['engine'], 'faxconfig', '-m', f'ttyIAX{number}',
                   'RingsBeforeAnswer', '0')
    seen = len(answered_lines(docker, context['asterisk']))
    proof = {'missed': receive_through_the_engine(context, 1)}
    proof['missed_lines'] = answered_lines(docker, context['asterisk'])[seen:]
    fax_id = proof['missed']['fax']['id']
    call = database(context, call=f"SELECT call_id, started_at, answered_at FROM sip_call_records "
                                  f"WHERE job_id = '{fax_id}'")['call']
    proof['missed_call'] = call
    events = parse_ami(docker.read(context['api'], '/tmp/ami-events.log'))
    proof['missed_events'] = [event for event in events if event.get('UserEvent') == 'FaxEngineMissed']
    request = wait_for(lambda: json.loads(docker.read(context['api'], '/faxdata/hylafax/engine-restart') or 'null'),
                       60, 'the restart request')
    proof['request'] = request
    engine_running_again(context, request['asked'])
    proof['status'] = json.loads(docker.read(context['engine'], '/faxdata/hylafax-out/engine.status'))
    trunk = api(docker, 'GET', '/admin/sip/status', key=context['key'])['json'] or {}
    proof['engine_state'], proof['engine_text'] = trunk.get('engine_state'), trunk.get('engine_text')
    engine_log = docker.run('logs', context['engine'], check=False)
    proof['engine_log'] = (engine_log.stdout + engine_log.stderr)[-1500:]
    seen = len(answered_lines(docker, context['asterisk']))
    proof['next'] = receive_through_the_engine(context, 2)
    proof['next_lines'] = answered_lines(docker, context['asterisk'])[seen:]
    print('\nSSLFAX_PROOF_L ' + json.dumps(proof, indent=2, default=str))
    # The missed fax arrived through the built-in engine, answered within the 20 s the lines may ring.
    assert proof['missed']['fax']['pages'] == 2 and proof['missed_lines'] == [], proof
    assert call and not call[0]['call_id'].startswith('engine.'), proof
    rang = (datetime.fromisoformat(str(call[0]['answered_at'])) - datetime.fromisoformat(str(call[0]['started_at'])))
    assert rang.total_seconds() <= 23, proof
    assert len(proof['missed_events']) == 1, proof
    assert request['reason'] == 'missed_call', proof
    # The engine started again after the request, and the trunk page says what happened.
    assert proof['status']['state'] == 'running' and proof['status']['started'] >= request['asked'], proof
    assert proof['engine_state'] == 'running', proof
    text = proof['engine_text'] or ''
    assert text.startswith("Faxbot's fax engine did not answer the "), proof
    assert text.endswith('so that fax was received the ordinary way; Faxbot restarted the fax engine.'), proof
    # The restart wrote the engine's own settings back: the next call reaches line 1 through the engine.
    assert proof['next']['fax']['pages'] == 2, proof
    assert proof['next']['call'] and proof['next']['call'][0]['call_id'].startswith('engine.'), proof
    assert proof['next_lines'] == [1], proof


def test_n_asterisk_as_its_own_user_sends_and_receives_with_its_built_in_engine(tmp_path, loopback):
    """Asterisk runs as its own user (uid 5060), as docker-compose.yml runs it. With the fax engine
    stopped, Faxbot's built-in engine does the work: SendFAX reads the pages the API wrote (mode 0640, the
    data folder's group), ReceiveFAX writes the received fax into Faxbot's inbound folder, and the hand-over
    script reads the inbound secret the API wrote (mode 0640) and gets the fax into Received."""
    context = loopback('n', faxbot_t38=False, carrier_gateway=False, peer_listener='', peer_sslfax=False)
    docker, asterisk = context['docker'], context['asterisk']
    docker.run('stop', context['engine'])
    # Asterisk checks the engine's lines; once none answers, Faxbot uses its built-in engine both ways.
    wait_for(lambda: ' OK ' not in docker.asterisk(asterisk, 'iax2 show peers'), 180,
             "Faxbot's Asterisk to see the engine's lines gone")
    files = docker.run('exec', asterisk, 'stat', '-c', '%a %U:%G %n', '/faxdata', '/faxdata/asterisk',
                       '/faxdata/asterisk/inbound.secret', '/faxdata/inbound', check=False).stdout
    owner = docker.run('exec', asterisk, 'ps', '-o', 'user=,args=', '-C', 'asterisk', check=False).stdout

    outcome = send_and_collect(tmp_path, context)
    proof = evidence(outcome)
    sent_tiff = docker.run('exec', asterisk, 'stat', '-c', '%a %U:%G', f"/faxdata/{outcome['job']['id']}.tiff",
                           check=False).stdout.strip()
    sent_record = records(context, outcome['job']['id'])

    docker.put(context['peer'], '/tmp/inbound.ps', PEER_DOCUMENT)
    queued = docker.run('exec', context['peer'], 'sendfax', '-n', '-d', FAXBOT_NUMBER.lstrip('+'), '/tmp/inbound.ps',
                        check=False)
    assert queued.returncode == 0, queued.stderr

    def arrived():
        return database(context, fax="SELECT id, from_number, to_number, pages FROM inbound_faxes")['fax'] or None
    faxes = wait_for(arrived, 240, 'the fax in Received')
    # As Asterisk's user: its inbound folder is its own (root here has no file override).
    received = docker.run('exec', '-u', 'asterisk', asterisk, 'sh', '-c', 'stat -c "%a %U:%G" /faxdata/inbound/*.tiff',
                          check=False).stdout.split('\n')[0]
    log = docker.run('logs', asterisk, check=False)
    proof.update({'files': files.splitlines(), 'asterisk_process': ' '.join(owner.split()), 'sent_tiff': sent_tiff,
                  'sent_record': sent_record['call'], 'received': faxes, 'received_tiff': received,
                  'handover_failures': [line for line in (log.stdout + log.stderr).splitlines()
                                        if 'was not handed to Faxbot' in line]})
    print('\nSSLFAX_PROOF_N ' + json.dumps(proof, indent=2, default=str))
    assert proof['asterisk_process'] == 'asterisk asterisk -f -C /etc/asterisk/asterisk.conf', proof
    assert '2755 root:asterisk /faxdata' in proof['files'], proof
    assert '640 root:asterisk /faxdata/asterisk/inbound.secret' in proof['files'], proof
    # Sent by the built-in engine (no engine call), from the API's own 0640 image, every page intact.
    assert proof['job_status'].upper() == 'SUCCESS' and proof['engine_call'] is None, proof
    assert proof['sent_pages'] == proof['received_pages'] == PAGES, proof
    assert all(offset is not None for offset in proof['header_offsets']), proof
    assert sent_tiff == '640 root:asterisk' and sent_record['call']['fax_status'] == 'SUCCESS', proof
    # Received by the built-in engine, written by Asterisk as its own user, and handed over with the secret.
    fax = faxes[0]
    assert fax['from_number'] == PEER_NUMBER and fax['to_number'] == FAXBOT_NUMBER and fax['pages'] == 2, proof
    assert received.endswith('asterisk:asterisk') and proof['handover_failures'] == [], proof



REPLY_NUMBER = '+15555550177'


def test_o_the_ssl_fax_engine_sends_the_reply_number_as_its_station_id(tmp_path, loopback):
    """Numbers -> Sender identity: a reply number (a second number on the trunk, routed to a mailbox) is the
    station ID the SSL Fax engine sends (JPARM TSI with UseJobTSI), not the line's own number."""
    context = loopback('o', faxbot_t38=False, carrier_gateway=False,
                       peer_listener=f'{ADDRESS["peer"]}:{LISTENER_PORT}',
                       api_extra={'SIP_TRUNK_DIDS': f'{FAXBOT_NUMBER},{REPLY_NUMBER}'})
    docker, key = context['docker'], context['key']
    version = api(docker, 'GET', '/auth/me', key=key)['json']['policy_version']
    box = api(docker, 'POST', '/access/mailboxes', key=key,
              body={'label': 'Replies', 'enabled': True, 'expected_policy_version': version})
    assert box['status'] == 200, box
    version = api(docker, 'GET', '/auth/me', key=key)['json']['policy_version']
    rule = api(docker, 'POST', '/access/inbound-rules', key=key,
               body={'to_number': REPLY_NUMBER, 'mailbox_id': box['json']['mailbox']['id'],
                     'expected_policy_version': version})
    assert rule['status'] == 200, rule
    saved = api(docker, 'PUT', '/numbers/reply', key=key, body={'number': REPLY_NUMBER})
    assert saved['status'] == 200 and saved['json']['number'] == REPLY_NUMBER, saved
    outcome = send_and_collect(tmp_path, context)
    proof = evidence(outcome)
    print('\nSSLFAX_PROOF_O ' + json.dumps({'received_info': proof['received_info']}, indent=2))
    assert_delivered(outcome, proof)
    assert proof['received_info'].get('Sender') == REPLY_NUMBER, proof['received_info']


def shaded_pdf():
    """Three pages of lightly shaded table rows: Ghostscript draws the shading as dots, which MH codes smaller than
    MMR (about 264,000 against 349,000 bits a page), so Faxbot asks the engine for MH."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=letter, invariant=1)
    for number in range(1, PAGES + 1):
        page.setFont('Helvetica-Bold', 28)
        page.drawString(72, 720, f'SHADED PROOF PAGE {number} OF {PAGES}')
        for row in range(6):
            top = 680 - row * 30
            page.setFillGray(0.9)
            page.rect(72, top - 6, 468, 20, fill=1, stroke=0)
            page.setFillGray(0)
            page.setFont('Helvetica', 12)
            page.drawString(80, top, f'Row {row + 1} of page {number}')
        page.showPage()
    page.save()
    return buffer.getvalue()


def test_p_the_coding_measured_on_the_pages_reaches_the_engines_call(tmp_path, loopback):
    """Measured fax coding (api/app/pages/coding.py): shaded pages measure smallest in MH, so Faxbot asks the SSL
    Fax engine for MH as the job's data format (JPARM DATAFORMAT "G31D", the job's desireddf 0), and the engine's
    job controls (hylafax/bin/jobcontrol) make faxsend honour it. The compression setting stops at MMR, so JBIG is
    not usable; the peer's machine takes MR and MMR, yet the call agrees MH, and every page arrives intact.
    Shading is kept as it is (FAX_FRIENDLY_DOCUMENTS never), so the measurement is of the pages as drawn.
    Without the job controls (8 October 2026) faxsend's software conversion ignored the job's data format."""
    context = loopback('p', faxbot_t38=False, carrier_gateway=False, peer_listener='', peer_sslfax=False,
                       api_extra={'FAX_FRIENDLY_DOCUMENTS': 'never', 'SIP_FAX_COMPRESSION': 'mmr'})
    docker, key = context['docker'], context['key']
    outcome = send_and_collect(tmp_path, context, document=shaded_pdf())
    proof = evidence(outcome)
    job_id = outcome['job']['id']
    found = records(context, job_id)
    coded = database(context, coding=f"SELECT requested, measured, compared, pages, bits, reason "
                                      f"FROM fax_coding_choices WHERE job_id = '{job_id}'")['coding']
    negotiated = database(context, engine=f"SELECT compression, ecm FROM fax_engine_calls WHERE job_id = '{job_id}'")
    detail = api(docker, 'GET', f'/admin/fax-jobs/{job_id}', key=key)['json'] or {}
    desired = re.findall(r'^desireddf:(\d+)$', outcome['done_qfile'], re.MULTILINE)
    proof.update({'coding': coded, 'negotiated': negotiated['engine'], 'engine_record': found['engine'],
                  'desireddf': desired, 'sent_detail': detail.get('coding'),
                  'dcs': [line for line in outcome['faxbot_log'].splitlines() if 'MH' in line or 'MMR' in line][:8]})
    print('\nSSLFAX_PROOF_P ' + json.dumps(proof, indent=2, default=str))
    assert_delivered(outcome, proof)
    assert coded and coded[0]['requested'] == 'MH' and coded[0]['measured'] == 1 and coded[0]['pages'] == PAGES, proof
    # The job's data format reached the engine (sendq(5) desireddf: 0 is 1-D MH) and the call agreed MH.
    assert desired and set(desired) == {'0'}, proof
    assert negotiated['engine'] and negotiated['engine'][0]['compression'] == 'MH', proof
    assert found['engine']['engine'] == 'hylafax', proof
    # The Sent detail says it: asked for MH, the call took MH.
    assert detail['coding']['requested'] == 'MH' and detail['coding']['negotiated'] == 'MH', proof
    assert detail['coding']['sentence'].startswith('Sent with MH: '), proof


def test_q_jbig_is_measured_and_chosen_once_the_receiving_machine_is_on_record(tmp_path, loopback):
    """JBIG end to end (M7). The API image has jbigkit, so the shaded pages are measured in JBIG too, with the
    engine's own T.85 options. First fax: the peer's machine is not on record, so JBIG is not priced (MH, the
    smallest of the others, is) and the SSL Fax engine is asked for nothing: it negotiates JBIG itself, and its
    session log ("REMOTE format support") puts the machine's codings on record. Second fax, the same pages:
    JBIG is chosen by its measured size, asked of the engine, and the call uses it. Every page of both arrives
    with identical pixels below the header line."""
    context = loopback('q', faxbot_t38=False, carrier_gateway=False, peer_listener='', peer_sslfax=False,
                       api_extra={'FAX_FRIENDLY_DOCUMENTS': 'never'})
    docker, key = context['docker'], context['key']
    # Pages as drawn on both faxes: once the first call puts the peer's unlimited page length on record, dense
    # pages would pack the second fax onto one long page (it did, on 8 October 2026, intact and in JBIG).
    kept = api(docker, 'PUT', '/routing/destinations/%2B' + PEER_NUMBER.lstrip('+') + '/pages', key=key,
               body={'packing': 'never'})
    assert kept['status'] == 200, kept
    proofs = {}
    for name in ('first', 'second'):
        folder = tmp_path / name
        folder.mkdir()
        outcome = send_and_collect(folder, context, document=shaded_pdf())
        proof = evidence(outcome)
        job_id = outcome['job']['id']
        coded = database(context, coding=f"SELECT requested, measured, compared, bits, reason "
                                          f"FROM fax_coding_choices WHERE job_id = '{job_id}'")['coding']
        negotiated = database(context, engine=f"SELECT compression FROM fax_engine_calls WHERE job_id = '{job_id}'")
        known = database(context, known="SELECT codings, engine FROM page_capability_observations "
                                        "ORDER BY observed_at DESC")['known']
        detail = api(docker, 'GET', f'/admin/fax-jobs/{job_id}', key=key)['json'] or {}
        proof.update({'coding': coded, 'negotiated': negotiated['engine'], 'receiver_on_record': known,
                      'desireddf': re.findall(r'^desireddf:(\d+)$', outcome['done_qfile'], re.MULTILINE),
                      'sent_detail': detail.get('coding'),
                      'received_info_raw': outcome['received_info'][:400]})
        proofs[name] = proof
        print(f'\nSSLFAX_PROOF_Q_{name.upper()} ' + json.dumps(proof, indent=2, default=str))
        assert_delivered(outcome, proof)
    print('\nSSLFAX_PROOF_Q ' + json.dumps(proofs, indent=2, default=str))
    first, second = proofs['first'], proofs['second']
    # Measured in JBIG on the API image, smaller than every other coding.
    bits = json.loads(first['coding'][0]['bits'])
    assert set(bits) == {'MH', 'MR', 'MMR', 'JBIG'} and bits['JBIG'] < min(bits['MH'], bits['MR'], bits['MMR']), proofs
    # First: not on record, so priced at MH; the engine chose JBIG itself; the machine is on record after it.
    assert (first['coding'][0]['requested'], first['coding'][0]['measured']) == ('MH', 1), proofs
    assert first['negotiated'] and first['negotiated'][0]['compression'] == 'JBIG', proofs
    assert first['receiver_on_record'] and first['receiver_on_record'][0]['codings'] == 'MH,MR,MMR,JBIG', proofs
    # Second: JBIG chosen by its measured size, and the call used it.
    assert (second['coding'][0]['requested'], second['coding'][0]['measured']) == ('JBIG', 1), proofs
    assert second['coding'][0]['reason'].startswith('JBIG: '), proofs
    assert second['negotiated'] and second['negotiated'][0]['compression'] == 'JBIG', proofs
    assert second['sent_detail']['requested'] == 'JBIG' and second['sent_detail']['negotiated'] == 'JBIG', proofs


# The polling password the proof uses: distinctive, so a coincidental digit string in a log cannot fail its check.
PASSWORD = '1357924'


def held_pages(count):
    """A document to hold for collection: ``count`` pages of large distinct shapes as a Group 4 fax TIFF (bytes),
    and the same pages as bitmaps."""
    from PIL import Image, ImageDraw
    frames = []
    for number in range(1, count + 1):
        page = Image.new('1', (1728, 2200), 1)
        draw = ImageDraw.Draw(page)
        draw.rectangle((200, 300, 200 + number * 300, 700), fill=0)
        draw.ellipse((900, 900, 1500, 1500 + number * 100), fill=0)
        for row in range(number):
            draw.rectangle((100, 1700 + row * 80, 1600, 1730 + row * 80), fill=0)
        frames.append(page)
    # One strip per page, as faxq prepares documents and as HylaFAX's sender reads them (its first strip only).
    from app.conversion import _fax_tiff_bytes
    return _fax_tiff_bytes(frames), frames


def hold_on_peer(context, hold_id, count, *, selective='', password=''):
    """Hold a document on the peer for Faxbot's number to collect, exactly as Faxbot's own engine holds one
    (hylafax/patches/0002-polled-transmit.patch, hylafax_engine.hold_document): a TIFF in pollq with its
    sidecar, owned so faxgetty (uucp) can read them. Returns the pages held."""
    docker, peer = context['docker'], context['peer']
    image, frames = held_pages(count)
    sidecar = hylafax_engine.held_sidecar(number=FAXBOT_NUMBER, selective=selective, password=password, job=hold_id,
                                         tsi=PEER_NUMBER, tagline='Held for collection|%c|Page %%P of %%T')
    folder = '/var/spool/hylafax/pollq'
    docker.put(peer, f'{folder}/faxhold-{hold_id}.tif', image)
    docker.put(peer, f'{folder}/faxhold-{hold_id}.poll', sidecar)
    docker.sh(peer, f'chown uucp:uucp {folder}/faxhold-{hold_id}.* && chmod 640 {folder}/faxhold-{hold_id}.*',
              check=True)
    return frames


def collect_once(context, *, selective=None, password=None):
    """Turn collecting on for the peer's number with these settings and collect once; (request id, result row)."""
    docker, key = context['docker'], context['key']
    route = '/routing/destinations/%2B' + PEER_NUMBER.lstrip('+') + '/polling'
    body = {'enabled': True, 'label': 'Peer site', 'selective': selective}
    if password is not None:
        body['password'] = password
    saved = api(docker, 'PUT', route, key=key, body=body)
    assert saved['status'] == 200, saved
    asked = api(docker, 'POST', route + '/collect', key=key)
    assert asked['status'] == 202, asked
    request_id = asked['json']['id']

    def finished():
        found = database(context, result=f"SELECT outcome, sentence, pages, inbound_fax_id FROM poll_results "
                                          f"WHERE request_id = '{request_id}'")['result']
        return found[0] if found else None
    return request_id, wait_for(finished, 300, 'the collection result')


def polled_evidence(context):
    """What the peer's side shows: its POLLED FAX log lines, the reports its polled script kept (Faxbot is
    unreachable from the peer, so they stay in its volume), and what is still held."""
    docker, peer = context['docker'], context['peer']
    log = session_logs(docker, peer)
    reports = docker.sh(peer, 'cat /var/lib/faxbot-engine/results/*polled.report 2>/dev/null').stdout
    held = docker.sh(peer, 'ls /var/spool/hylafax/pollq 2>/dev/null').stdout.split()
    return {
        'peer_polled_lines': [line.split(']: ', 1)[-1] for line in log.splitlines()
                              if re.search(r'POLLED FAX|REMOTE (DTC|SEP|CIG|PWD)|DIS offers|TRAINING|USE ', line)][:40],
        'peer_reports': [json.loads(line) for line in reports.splitlines() if line.strip().startswith('{')],
        'still_held': held,
    }


def received_in_faxbot(context, inbound_fax_id, workdir):
    """The pages of a fax in Faxbot's Received, from the image the engine's hand-over put in its out folder."""
    from PIL import Image, ImageSequence
    docker = context['docker']
    rows = database(context, fax=f"SELECT id, from_number, to_number, pages, tiff_path FROM inbound_faxes "
                                 f"WHERE id = '{inbound_fax_id}'")['fax']
    assert rows, inbound_fax_id
    path = rows[0]['tiff_path']
    local = workdir / f'received-{inbound_fax_id}.tif'
    local.write_bytes(docker.read_bytes(context['api'], path))
    return rows[0], [frame.convert('1').copy() for frame in ImageSequence.Iterator(Image.open(local))]


def test_s_a_held_fax_is_collected_by_polling_and_an_unknown_selective_address_is_refused(tmp_path, loopback):
    """Polled transmission (hylafax/patches/0002-polled-transmit.patch, the other half of M21). The peer runs
    the same patched engine image and holds two documents for Faxbot's number: one plain, one behind the
    selective polling address 77. Faxbot collects (poll-receive): the peer's DIS offers a document (bit 9),
    Faxbot's engine answers DTC, the peer turns the line around and sends the plain document, which lands in
    Faxbot's Received with identical pixels below the header line and is no longer held. A collection with a
    selective polling address the peer holds nothing for (42) is refused with DCN right after the DTC, recorded
    as refused, and leaves the protected document held. Case r (a peer that holds nothing) stays as it is."""
    context = loopback('s', faxbot_t38=False, carrier_gateway=False, peer_listener='', peer_sslfax=False)
    plain = hold_on_peer(context, 'a' * 32, 2)
    hold_on_peer(context, 'b' * 32, 1, selective='77', password=PASSWORD)
    proof = {}
    try:
        request, result = collect_once(context)
        proof['collected'] = result
        assert result['outcome'] == 'received', (result, polled_evidence(context))
        fax, pages = received_in_faxbot(context, result['inbound_fax_id'], tmp_path)
        proof['received_fax'] = {key: fax[key] for key in ('from_number', 'to_number', 'pages')}
        proof['header_offsets'] = [page_match(a, b) for a, b in zip(plain, pages)]
        proof['page_sizes'] = [list(page.size) for page in pages]
        time.sleep(3)
        proof['after_first'] = polled_evidence(context)
        refused_request, refused = collect_once(context, selective='42')
        proof['refused'] = refused
        time.sleep(3)
        proof['after_second'] = polled_evidence(context)
        proof['faxbot_poll_lines'] = [line.split(']: ', 1)[-1] for line in session_logs(context['docker'], context['engine']).splitlines()
                                      if re.search(r'POLL|DTC|SEP|REMOTE best|got DCN|E103', line)][:30]
    finally:
        print('\nSSLFAX_PROOF_S ' + json.dumps(proof, indent=2, default=str))
    assert fax['from_number'] == PEER_NUMBER and fax['pages'] == 2 and len(pages) == 2, proof
    assert all(offset is not None for offset in proof['header_offsets']), proof
    first = proof['after_first']
    assert any('REMOTE DTC' in line for line in first['peer_polled_lines']), proof
    assert any(line.startswith('POLLED FAX: pollq/faxhold-' + 'a' * 32) and 'sent to' in line
               for line in first['peer_polled_lines']), proof
    assert [report['outcome'] for report in first['peer_reports']] == ['sent'], proof
    assert first['peer_reports'][0]['job'] == 'a' * 32 and first['peer_reports'][0]['pages'] == 2, proof
    assert sorted(first['still_held']) == ['faxhold-' + 'b' * 32 + '.poll', 'faxhold-' + 'b' * 32 + '.tif'], proof
    assert refused['outcome'] == 'refused', proof
    second = proof['after_second']
    assert any('no document is held for that selective polling address' in line for line in second['peer_polled_lines']), proof
    assert [report['outcome'] for report in second['peer_reports']] == ['sent', 'refused'], proof
    assert sorted(second['still_held']) == sorted(first['still_held']), proof


def test_s2_a_held_fax_behind_a_password_goes_only_to_the_caller_that_gives_it(tmp_path, loopback):
    """Polled transmission with a polling password: the peer holds one document behind the selective polling
    address 77 and the password 2468. Faxbot asks with the wrong password and is refused with DCN right after
    its DTC (the document stays held); with the right password, sent with the call from its sealed setting, the
    document is collected and lands in Received with identical pixels below the header line."""
    context = loopback('s2', faxbot_t38=False, carrier_gateway=False, peer_listener='', peer_sslfax=False)
    protected = hold_on_peer(context, 'b' * 32, 1, selective='77', password=PASSWORD)
    proof = {}
    try:
        wrong_request, wrong = collect_once(context, selective='77', password='0000000')
        proof['wrong_password'] = wrong
        time.sleep(3)
        proof['after_wrong'] = polled_evidence(context)
        right_request, right = collect_once(context, selective='77', password=PASSWORD)
        proof['right_password'] = right
        if right['outcome'] == 'received':
            fax, pages = received_in_faxbot(context, right['inbound_fax_id'], tmp_path)
            proof['received_fax'] = {key: fax[key] for key in ('from_number', 'to_number', 'pages')}
            proof['header_offsets'] = [page_match(a, b) for a, b in zip(protected, pages)]
            proof['page_sizes'] = [list(page.size) for page in pages]
        time.sleep(3)
        proof['after_right'] = polled_evidence(context)
        proof['faxbot_poll_lines'] = [line.split(']: ', 1)[-1] for line in session_logs(context['docker'], context['engine']).splitlines()
                                      if re.search(r'POLL|DTC|SEP|got DCN|E103', line)][:30]
    finally:
        print('\nSSLFAX_PROOF_S2 ' + json.dumps(proof, indent=2, default=str))
    assert wrong['outcome'] == 'refused', proof
    first = proof['after_wrong']
    assert any('the polling password (PWD) does not match' in line for line in first['peer_polled_lines']), proof
    assert [report['outcome'] for report in first['peer_reports']] == ['refused'], proof
    assert sorted(first['still_held']) == ['faxhold-' + 'b' * 32 + '.poll', 'faxhold-' + 'b' * 32 + '.tif'], proof
    assert right['outcome'] == 'received' and proof['received_fax']['pages'] == 1, proof
    assert proof['header_offsets'] == [proof['header_offsets'][0]] and proof['header_offsets'][0] is not None, proof
    second = proof['after_right']
    assert [report['outcome'] for report in second['peer_reports']] == ['refused', 'sent'], proof
    assert second['peer_reports'][1]['job'] == 'b' * 32 and second['still_held'] == [], proof
    # The password itself is in no log the run writes: both engines' session logs (stock HylaFAX+ printed it in the
    # poller's; patch 0002 hides it) and every container's own log.
    docker = context['docker']
    for name in ('peer', 'engine'):
        assert PASSWORD not in session_logs(docker, context[name]), name
    for name in ('api', 'asterisk', 'engine', 'carrier', 'peer'):
        logs = docker.run('logs', context[name], check=False)
        assert PASSWORD not in logs.stdout + logs.stderr, name


def test_r_a_collection_from_a_fax_server_that_holds_nothing_calls_once_and_says_so(tmp_path, loopback):
    """Collecting by polling (M21) against a stock HylaFAX+ 7.0.11 peer, which cannot be polled: its DIS never
    sets bit 9 ("ready to transmit"), so Faxbot's engine places the call once, hears that the other machine holds
    no document (faxd/FaxSend.c++ sendPoll: "remote has no document to poll"), and the collection says so.
    Nothing reaches Received, the call is recorded like any other, and nothing is collected again by itself.
    Collecting is refused until it is turned on for the number."""
    context = loopback('r', faxbot_t38=False, carrier_gateway=False, peer_listener='', peer_sslfax=False)
    docker, key = context['docker'], context['key']
    route = '/routing/destinations/%2B' + PEER_NUMBER.lstrip('+') + '/polling'
    refused = api(docker, 'POST', route + '/collect', key=key)
    assert refused['status'] == 409, refused
    turned_on = api(docker, 'PUT', route, key=key, body={'enabled': True, 'label': 'Peer site', 'selective': None})
    assert turned_on['status'] == 200 and turned_on['json']['enabled'] is True, turned_on
    asked = api(docker, 'POST', route + '/collect', key=key)
    assert asked['status'] == 202, asked
    request_id = asked['json']['id']

    def finished():
        found = database(context, result=f"SELECT outcome, sentence FROM poll_results "
                                          f"WHERE request_id = '{request_id}'")['result']
        return found[0] if found else None
    result = wait_for(finished, 300, 'the collection result')
    time.sleep(5)
    rows = database(context,
                    calls=f"SELECT disposition, connected_seconds, called FROM sip_call_records "
                          f"WHERE job_id = '{request_id}' AND direction = 'outbound'",
                    requests="SELECT id FROM poll_requests",
                    received="SELECT id FROM inbound_faxes")
    shown = api(docker, 'GET', route, key=key)['json'] or {}
    log = session_logs(docker, context['engine'])
    proof = {'result': result, 'calls': rows['calls'], 'requests': rows['requests'], 'received': rows['received'],
             'shown': shown.get('requests'),
             'engine_poll_lines': [line.split(']: ', 1)[-1] for line in log.splitlines()
                                   if re.search(r'POLL|poll|DTC|REMOTE best|document', line)][:16]}
    print('\nSSLFAX_PROOF_R ' + json.dumps(proof, indent=2, default=str))
    assert result['outcome'] == 'nothing_waiting', proof
    assert result['sentence'] == 'The other fax server had no fax waiting for you.', proof
    assert len(rows['calls']) == 1 and rows['calls'][0]['disposition'] == 'answered', proof
    assert [row['id'] for row in rows['requests']] == [request_id] and rows['received'] == [], proof
    assert shown['requests'][0]['state'] == 'Nothing waiting', proof


# Lossless encoder tuning (hylafax/patches/0003, pages/tuning.py) ---------------------------------------------

def tuning_pdf():
    """A tinted form, a photograph and a shaded table: the pages lossless tuning shrinks most. Ghostscript draws
    the tints and the photograph's grey levels as dots (FAX_FRIENDLY_DOCUMENTS never keeps them)."""
    from PIL import Image
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=letter)
    page.setFont('Helvetica-Bold', 24)
    page.drawString(72, 730, 'TINTED FORM PROOF PAGE 1 OF 3')
    for box in range(8):
        top = 680 - box * 70
        page.setFillGray(0.8)
        page.rect(72, top - 40, 468, 50, fill=1, stroke=1)
        page.setFillGray(0)
        page.setFont('Helvetica', 12)
        page.drawString(80, top - 10, f'Field {box + 1}: synthetic value {box * 37 + 11}')
    page.showPage()
    photo = Image.new('L', (400, 500))
    photo.putdata([int(128 + 120 * ((x - 200) * (y - 250)) / (200 * 250)) for y in range(500) for x in range(400)])
    page.setFont('Helvetica-Bold', 24)
    page.drawString(72, 730, 'PHOTOGRAPH PROOF PAGE 2 OF 3')
    page.drawImage(ImageReader(photo), 106, 120, width=400, height=560)
    page.showPage()
    page.setFont('Helvetica-Bold', 24)
    page.drawString(72, 730, 'SHADED TABLE PROOF PAGE 3 OF 3')
    for row in range(16):
        top = 690 - row * 36
        page.setFillGray(0.9 if row % 2 else 0.75)
        page.rect(72, top - 8, 468, 26, fill=1, stroke=0)
        page.setFillGray(0)
        page.setFont('Helvetica', 12)
        page.drawString(80, top, f'Row {row + 1}   item {row * 13 + 7}   amount {row * 101 + 3}.00')
    page.showPage()
    page.save()
    return buffer.getvalue()


def received_bies(docker, peer, workdir):
    """The BIE header of each page of the peer's newest received fax, as it arrived (HylaFAX+ keeps a JBIG page
    undecoded in its TIFF): MX (BIH byte 16), options (byte 19) and L0 (bytes 12-15), T.82 6.2."""
    name = docker.sh(peer, 'ls -t /var/spool/hylafax/recvq/fax*.tif 2>/dev/null | head -1').stdout.strip()
    if not name:
        return []
    path = workdir / 'received-raw.tif'
    path.write_bytes(docker.read_bytes(peer, name))
    import struct
    data, found = path.read_bytes(), []
    # Pillow does not open JBIG TIFFs, so the directories are read here (TIFF 6.0 section 2).
    order = '<' if data[:2] == b'II' else '>'
    directory = struct.unpack(order + 'I', data[4:8])[0]
    while directory and len(found) < 50:
        count = struct.unpack(order + 'H', data[directory:directory + 2])[0]
        tags = {}
        for index in range(count):
            entry = data[directory + 2 + 12 * index:directory + 14 + 12 * index]
            tag, kind, _ = struct.unpack(order + 'HHI', entry[:8])
            tags[tag] = struct.unpack(order + ('H' if kind == 3 else 'I'), entry[8:10] if kind == 3 else entry[8:12])[0]
        if tags.get(259) == 9:  # TIFF Compression 9: JBIG (T.85); one strip a page
            bih = data[tags[273]:tags[273] + 20]
            found.append({'compression': 'JBIG', 'mx': bih[16], 'options': bih[19],
                          'l0': int.from_bytes(bih[12:16], 'big')})
        else:
            found.append({'compression': tags.get(259)})
        directory = struct.unpack(order + 'I', data[directory + 2 + 12 * count:directory + 6 + 12 * count])[0]
    return found


def row_check(sent, received, header=140):
    """Row by row below the header band (the sender's tag line is drawn over the top): how many rows both pages
    have, which of them differ and by how many pixels, and whether a row only one page has is blank."""
    width = min(sent.size[0], received.size[0])
    rows = min(sent.size[1], received.size[1])
    stride = (width + 7) // 8
    a, b = sent.crop((0, 0, width, sent.size[1])).tobytes(), received.crop((0, 0, width, received.size[1])).tobytes()
    differing = []
    for y in range(header, rows):
        left, right = a[y * stride:(y + 1) * stride], b[y * stride:(y + 1) * stride]
        if left != right:
            differing.append([y, sum(bin(x ^ z).count('1') for x, z in zip(left, right))])
    longer, extra = (a, sent.size[1]) if sent.size[1] > received.size[1] else (b, received.size[1])
    # Pillow's one-bit pages: a set bit is paper, so a blank row is all 0xff bytes.
    blank = all(byte == 0xff for byte in longer[rows * stride:extra * stride])
    return {'sent_rows': sent.size[1], 'received_rows': received.size[1], 'common_rows': rows,
            'differing_rows_below_header': len(differing), 'first_differences': differing[:5],
            'unmatched_rows_blank': blank}


def tuning_proof(tmp_path, context, name, *, exact=True):
    folder = tmp_path / name
    folder.mkdir()
    outcome = send_and_collect(folder, context, document=tuning_pdf())
    proof = evidence(outcome)
    job_id = outcome['job']['id']
    proof['lossless_lines'] = [line.split(']: ', 1)[-1] for line in outcome['faxbot_log'].splitlines()
                               if 'LOSSLESS TUNING' in line]
    proof['page_answers'] = re.findall(r'SEND recv (MCF|RTN|PPR)', outcome['faxbot_log'])
    proof['tuning_rows'] = database(context, rows=f"SELECT coding, pages, tuned_pages, tuned_bytes, plain_bytes, "
                                                  f"settings, sslfax, refused FROM coding_tuning_calls "
                                                  f"WHERE job_id = '{job_id}'")['rows']
    proof['records'] = records(context, job_id)
    detail = api(context['docker'], 'GET', f'/admin/fax-jobs/{job_id}', key=context['key'])['json'] or {}
    proof['sent_detail'] = detail.get('coding')
    proof['comments'] = re.findall(r'^comments:(.*)$', outcome['done_qfile'], re.MULTILINE)
    proof['received_bies'] = received_bies(context['docker'], context['peer'], folder)
    transfer = (proof['records'].get('engine') or {}).get('transfer_seconds')
    proof['seconds_per_page'] = round(transfer / PAGES, 2) if transfer else None
    proof['rows'] = [row_check(a, b) for a, b in zip(outcome['sent_pages'], outcome['received_pages'])]
    print(f'\nSSLFAX_PROOF_{name.upper()} ' + json.dumps(proof, indent=2, default=str))
    if exact:
        assert_delivered(outcome, proof)
    else:
        assert proof['job_status'].upper() == 'SUCCESS' and proof['received_pages'] == PAGES, proof
    return proof


def test_s_tuned_jbig_over_ssl_fax_keeps_every_pixel_and_is_sent_plain_when_you_turn_it_off(tmp_path, loopback):
    """Tuned JBIG between two patched engines over SSL Fax (TuneJBIG "sslfax", the default): the BIE the peer
    received carries the tuned options and MX, the engine's session log and Faxbot's records say what was sent,
    and every page arrives with identical pixels below the header line. Then the same pages with smaller pages off
    for the number: plain JBIG (options 0, MX 0), for the transfer time against stock settings."""
    context = loopback('s', faxbot_t38=False, carrier_gateway=False,
                       peer_listener=f'{ADDRESS["peer"]}:{LISTENER_PORT}',
                       api_extra={'FAX_FRIENDLY_DOCUMENTS': 'never'})
    docker, key = context['docker'], context['key']
    number = '/routing/destinations/%2B' + PEER_NUMBER.lstrip('+')
    assert api(docker, 'PUT', number + '/pages', key=key, body={'packing': 'never'})['status'] == 200
    tuned = tuning_proof(tmp_path, context, 's_tuned')
    off = api(docker, 'PUT', number + '/coding-tuning', key=key, body={'tune': False, 'tune_jbig': False})
    assert off['status'] == 200 and off['json']['jbig'] == 'never', off
    plain = tuning_proof(tmp_path, context, 's_plain')
    print('\nSSLFAX_PROOF_S ' + json.dumps({'tuned_seconds_per_page': tuned['seconds_per_page'],
                                            'plain_seconds_per_page': plain['seconds_per_page'],
                                            'tuned_bies': tuned['received_bies'],
                                            'plain_bies': plain['received_bies']}, indent=2, default=str))
    assert tuned['received_info'].get('SignalRate') == 'SSL Fax', tuned
    assert tuned['comments'] == ['faxbot-tuning mr=on jbig=sslfax'], tuned
    assert len(tuned['lossless_lines']) >= PAGES and all('JBIG tuned' in line for line in tuned['lossless_lines'])
    assert any(page.get('options') or page.get('mx') for page in tuned['received_bies']), tuned
    assert all(page.get('l0') == 128 for page in tuned['received_bies']), tuned
    rows = {row['coding']: row for row in tuned['tuning_rows']}
    assert rows['JBIG']['tuned_pages'] >= 1 and rows['JBIG']['tuned_bytes'] < rows['JBIG']['plain_bytes'], tuned
    assert rows['JBIG']['sslfax'] == 1 and rows['JBIG']['refused'] == 0, tuned
    assert tuned['sent_detail']['tuned'] == ['JBIG'], tuned
    assert plain['comments'] == ['faxbot-tuning mr=off jbig=never'], plain
    assert all(page.get('options') == 0 and page.get('mx') == 0 for page in plain['received_bies']), plain


def test_t_the_mr_schedule_without_error_correction_keeps_every_pixel(tmp_path, loopback):
    """MR without ECM (the peer's modem has error correction off): faxd codes the MR file again with the
    fewest-bytes reset schedule even though the session is MR too, and every page arrives intact. Then the same
    pages with smaller pages off for the number, for the transfer time against stock settings."""
    context = loopback('t', faxbot_t38=False, carrier_gateway=False, peer_listener='', peer_sslfax=False,
                       peer_ecm=False, api_extra={'FAX_FRIENDLY_DOCUMENTS': 'never', 'SIP_FAX_COMPRESSION': 'mr'})
    docker, key = context['docker'], context['key']
    number = '/routing/destinations/%2B' + PEER_NUMBER.lstrip('+')
    assert api(docker, 'PUT', number + '/pages', key=key, body={'packing': 'never'})['status'] == 200
    tuned = tuning_proof(tmp_path, context, 't_tuned', exact=False)
    off = api(docker, 'PUT', number + '/coding-tuning', key=key, body={'tune': False, 'tune_jbig': False})
    assert off['status'] == 200 and not off['json']['mr'], off
    plain = tuning_proof(tmp_path, context, 't_plain', exact=False)
    print('\nSSLFAX_PROOF_T ' + json.dumps({'tuned_seconds_per_page': tuned['seconds_per_page'],
                                            'plain_seconds_per_page': plain['seconds_per_page'],
                                            'tuned_lines': tuned['lossless_lines'],
                                            'tuned_coding': (tuned['records'].get('engine') or {}).get('data_format'),
                                            'plain_coding': (plain['records'].get('engine') or {}).get('data_format'),
                                            'tuned_rows': tuned['rows'], 'plain_rows': plain['rows']},
                                           indent=2, default=str))
    if str((tuned['records'].get('engine') or {}).get('data_format') or '').startswith('2-D MR'):
        assert tuned['lossless_lines'] and all('MR schedule' in line for line in tuned['lossless_lines']), tuned
        rows = {row['coding']: row for row in tuned['tuning_rows']}
        assert rows['MR']['tuned_bytes'] <= rows['MR']['plain_bytes'], tuned
    assert not plain['lossless_lines'], plain
