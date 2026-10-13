"""A fax server (the SR140 stand-in) sends through Faxbot's Asterisk as its gateway (fax_servers.py, store and forward).

Runs only with FAXBOT_NATIVE_PROOF=1 and FAXBOT_NATIVE_IMAGE naming an existing Faxbot Asterisk image (the proof never
builds one), on the Docker context in FAXBOT_DOCKER_CONTEXT. On one --internal network with no internet access:

- "faxbot": Faxbot's Asterisk, started by this checkout's start.sh, with its trunk pointing at an address nothing
  answers and one fax server exactly as Faxbot renders it (``fax_servers.render``: the stand-in's address only, MD5
  digest, its own transport on port 5080), and this checkout's dialplan and ``faxbot-fax-server`` script;
- "sr140": a second Asterisk running tools/sr140-emulator exactly as committed (run.sh, etc/, the test TIFF), which
  calls Faxbot the way an SR140 with a Primary Gateway does;
- "api": a stub that answers Faxbot's admit question (send for one number, refuse for a premium-rate one, receive for
  Faxbot's own number) and keeps every request.

Asserts that the authenticated call was admitted with the stand-in's real address and the dialled number, answered,
received over T.38 with both pages and handed over with its Call-ID; that a refused number was never answered; that a
wrong password and no password never reached the admit question at all; and that Faxbot's own number went the
ordinary receiving way. Nothing here reaches a carrier or a real RightFax server.

    FAXBOT_NATIVE_PROOF=1 FAXBOT_NATIVE_IMAGE=faxbot-accept-asterisk:freeze-84522bec \\
      make test-local VENV=... PYTEST_ARGS='-s tests/test_fax_server_loopback.py'
"""
import base64
import json
import os
from pathlib import Path
import time
import uuid

import pytest

from app import copier_extensions, fax_servers, sip_trunk
from tests.test_t38_loopback import AMI_PASSWORD, AMI_USER, DID, SECRET, UNREACHABLE, Docker, trunk_values, wait_booted

pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_NATIVE_PROOF') != '1' or not os.environ.get('FAXBOT_NATIVE_IMAGE'),
                       reason='Set FAXBOT_NATIVE_PROOF=1 and FAXBOT_NATIVE_IMAGE to run the fax server loopback proof.'),
]

ROOT = Path(__file__).resolve().parents[2]
STANDIN = ROOT / 'tools' / 'sr140-emulator'
OUTSIDE, PREMIUM, WRONG, NONE = '13035550199', '19005550100', '13035550188', '13035550177'
STUB_IMAGE = os.environ.get('FAXBOT_NATIVE_STUB_IMAGE', 'python:3.11-slim')

# The API stand-in: answers admit by the dialled number and keeps every request, one JSON object a line.
STUB = r'''
import http.server, json
ANSWERS = {"13035550199": "send:+13035550199", "19005550100": "refuse:403", "DID": "receive:+DID"}
class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        with open("/tmp/requests.jsonl", "a") as handle:
            handle.write(json.dumps({"path": self.path, "secret": self.headers.get("X-Internal-Secret"),
                                     "body": body}) + "\n")
        if self.path.endswith("/fax-server/admit"):
            answer, kind = ANSWERS.get(body.get("dialed") or "", "refuse:403"), "text/plain"
        else:
            answer, kind = '{"status": "ok"}', "application/json"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(answer.encode())
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
'''.replace('"DID"', '"' + DID.lstrip('+') + '"').replace('+DID', DID)


def standin(docker, container, command):
    """``asterisk -rx`` on the stand-in, whose configuration lives in /tmp/sr140."""
    return docker.run('exec', container, 'asterisk', '-C', '/tmp/sr140/etc/asterisk.conf', '-rx', command,
                      check=False).stdout


def _wait(check, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(1)
    raise AssertionError(message)


def call(docker, container, number, endpoint):
    """Send the stand-in's test fax to ``number`` through ``endpoint``, as send.sh does; returns its result line."""
    before = docker.read(container, '/fax/results.log').count('\n')
    plan = '\n'.join([f'Channel: PJSIP/{number}@{endpoint}', 'CallerID: "SR140 stand-in" <15555550100>',
                      'MaxRetries: 0', 'WaitTime: 45', 'Context: sr140-send', 'Extension: s', 'Priority: 1',
                      'Setvar: FAXFILE=/fax/test-fax.tiff', f'Setvar: SR140_TO={number}', 'Archive: yes', ''])
    docker.run('exec', '--interactive', container, 'sh', '-c',
               'cat > /tmp/sr140/spool/next.call && mv /tmp/sr140/spool/next.call /tmp/sr140/spool/outgoing/',
               input_text=plan)
    lines = _wait(lambda: (lambda text: text.splitlines()[before:] or None)(docker.read(container, '/fax/results.log')),
                  180, f'The stand-in never finished its call to {number}')
    return lines[0]


def requests(docker, api):
    return [json.loads(line) for line in docker.read(api, '/tmp/requests.jsonl').splitlines() if line.strip()]


def exchange(tmp_path):
    docker = Docker()
    image = os.environ['FAXBOT_NATIVE_IMAGE']
    password = 'Synthetic-' + uuid.uuid4().hex
    try:
        docker.network = docker.prefix
        docker.run('network', 'create', '--internal', '--label', 'com.faxbot.scope=t38-proof', docker.network)
        api = docker.start('api', STUB_IMAGE, 'python', '-c', STUB)
        faxbot = docker.start('faxbot', image, 'infinity', entrypoint='sleep')
        sr140 = docker.start('sr140', image, 'infinity', entrypoint='sleep')
        addresses = {name: docker.address(container) for name, container in (('api', api), ('faxbot', faxbot),
                                                                              ('sr140', sr140))}
        assert all(addresses.values())
        server_id = uuid.uuid4().hex
        username = f'faxserver-{server_id[:8]}'
        server = fax_servers.FaxServer(server_id, 'SR140 stand-in', addresses['sr140'], username,
                                       copier_extensions.digest(username, password), None, ('US',), 60, 500, None,
                                       None, None, 'active')
        # Faxbot: this checkout's start.sh, dialplan and script; its trunk goes nowhere; the fax server as rendered.
        for name in ('extensions.conf', 'extensions-copier.conf', 'extensions-fax-server.conf'):
            docker.run('cp', f'{ROOT}/asterisk/etc/asterisk/{name}', f'{faxbot}:/etc/asterisk/{name}')
        docker.run('cp', f'{ROOT}/asterisk/start.sh', f'{faxbot}:/start.sh')
        docker.run('cp', f'{ROOT}/asterisk/bin/faxbot-fax-server', f'{faxbot}:/usr/local/bin/faxbot-fax-server')
        docker.run('exec', faxbot, 'chmod', '755', '/start.sh', '/usr/local/bin/faxbot-fax-server')
        docker.run('exec', faxbot, 'mkdir', '-p', '/faxdata/asterisk', '/faxdata/inbound')
        rendered = tmp_path / 'faxbot-pjsip.conf'
        rendered.write_text(sip_trunk.render_pjsip(trunk_values(UNREACHABLE)) + '\n\n' + fax_servers.render([server]))
        docker.run('cp', str(rendered), f'{faxbot}:/faxdata/asterisk/pjsip.conf')
        environment = ['--env', f'ASTERISK_AMI_USERNAME={AMI_USER}', '--env', f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}',
                       '--env', f'ASTERISK_INBOUND_SECRET={SECRET}', '--env',
                       f'FAXBOT_API_URL=http://{addresses["api"]}:8080', '--env',
                       f'FAXBOT_FAX_SERVER_ADDRESS={addresses["faxbot"]}', '--env', 'FAXBOT_FAX_SERVER_PORT=5080']
        docker.run('exec', '--detach', *environment, faxbot, 'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        # The stand-in: tools/sr140-emulator exactly as committed.
        docker.run('exec', sr140, 'mkdir', '-p', '/emulator', '/fax')
        docker.run('cp', f'{STANDIN}/etc', f'{sr140}:/emulator/etc')
        docker.run('cp', f'{STANDIN}/run.sh', f'{sr140}:/emulator/run.sh')
        docker.run('cp', f'{STANDIN}/fax/test-fax.tiff', f'{sr140}:/fax/test-fax.tiff')
        docker.run('exec', '--detach', '--env', f'GATEWAY={addresses["faxbot"]}', '--env', 'GATEWAY_PORT=5080',
                   '--env', f'SR140_USERNAME={username}', '--env', f'SR140_PASSWORD={password}', sr140,
                   'sh', '-c', 'sh /emulator/run.sh > /tmp/sr140.log 2>&1')
        wait_booted(docker, faxbot)
        _wait(lambda: 'Fully Booted' in standin(docker, sr140, 'core waitfullybooted') or
              'Asterisk 22' in standin(docker, sr140, 'core show version'), 60, 'The stand-in did not start')
        time.sleep(2)
        endpoint = docker.asterisk(faxbot, f'pjsip show endpoint faxserver-{server_id}')
        dialplan = docker.asterisk(faxbot, 'dialplan show faxbot-fax-server')
        results = {
            'outside': call(docker, sr140, OUTSIDE, 'faxbot-gateway'),
            'premium': call(docker, sr140, PREMIUM, 'faxbot-gateway'),
            'wrong': call(docker, sr140, WRONG, 'faxbot-gateway-wrong'),
            'none': call(docker, sr140, NONE, 'faxbot-gateway-none'),
            'own': call(docker, sr140, DID.lstrip('+'), 'faxbot-gateway'),
        }
        seen = _wait(lambda: (lambda found: found if any(item['path'].endswith('/asterisk/inbound') for item in found)
                              else None)(requests(docker, api)), 60, 'Faxbot never handed over the received fax')
        handover = next(item for item in seen if item['path'].endswith('/fax-server/handover'))
        received = tmp_path / 'handed.tiff'
        docker.run('cp', f"{faxbot}:{handover['body']['tiff_path']}", str(received))
        return {'server': server, 'addresses': addresses, 'endpoint': endpoint, 'dialplan': dialplan,
                'results': results, 'requests': seen, 'received': received,
                'log': docker.read(faxbot, '/tmp/asterisk.log'), 'standin_log': docker.read(sr140, '/tmp/sr140.log')}
    except BaseException:
        for container in docker.containers:
            for path in ('/tmp/asterisk.log', '/tmp/sr140.log'):
                text = docker.read(container, path)
                if text:
                    print(f'--- {container}:{path}\n{text[-4000:]}')
        raise
    finally:
        docker.close()


def _pages(path):
    from PIL import Image
    with Image.open(path) as image:
        return getattr(image, 'n_frames', 1)


def test_the_sr140_stand_in_sends_through_faxbot_and_everything_else_is_refused(tmp_path):
    outcome = exchange(tmp_path)
    print(json.dumps({key: value for key, value in outcome['results'].items()}, indent=2))
    assert 'faxbot-fax-server' in outcome['endpoint'] and 'faxbot-fax-server' in outcome['dialplan']
    results, seen, server = outcome['results'], outcome['requests'], outcome['server']
    admits = [item for item in seen if item['path'].endswith('/fax-server/admit')]
    assert {item['body']['dialed'] for item in admits} == {OUTSIDE, PREMIUM, DID.lstrip('+')}  # never WRONG or NONE
    assert all(item['secret'] == SECRET and item['body']['server'] == server.id
               and item['body']['address'] == outcome['addresses']['sr140'] for item in admits)
    first = next(item for item in admits if item['body']['dialed'] == OUTSIDE)
    assert base64.b64decode(first['body']['callid64']) and first['body']['uniqueid']
    # The authenticated call to an outside number: answered, both pages received (over T.38), handed over once.
    assert 'result=SUCCESS' in results['outside'] and 'pages=2' in results['outside'], results['outside']
    assert 'mode=T38' in results['outside'], results['outside']
    handovers = [item for item in seen if item['path'].endswith('/fax-server/handover')]
    assert len(handovers) == 1
    body = handovers[0]['body']
    assert (body['server'], body['faxstatus'], body['faxpages']) == (server.id, 'SUCCESS', 2)
    assert body['callid64'] == first['body']['callid64'] and body['tiff_path'].startswith('/faxdata/fax-server/')
    assert _pages(outcome['received']) == 2
    # Refused before answering, and the two calls that could not authenticate never reached the question.
    for case in ('premium', 'wrong', 'none'):
        assert 'result=NOT_ANSWERED' in results[case], (case, results[case])
    # Faxbot's own number: the ordinary receiving path, filed under that number.
    assert 'result=SUCCESS' in results['own'], results['own']
    inbound = [item for item in seen if item['path'].endswith('/asterisk/inbound')]
    assert len(inbound) == 1 and inbound[0]['body']['to_number'] == DID
