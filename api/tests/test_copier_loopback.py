"""A copier registers to Faxbot as its own extension and faxes through it, both ways (N22, store and forward).

Runs only with FAXBOT_NATIVE_PROOF=1 and FAXBOT_NATIVE_IMAGE naming an existing Faxbot Asterisk image (the proof
never builds one). On one --internal network with no internet access:

- "faxbot": Faxbot's Asterisk, started by the image's own start.sh, with the copier extension exactly as Faxbot
  renders it (``copier_extensions.render``: MD5 credentials, private addresses only) and this checkout's dialplan
  and hand-over script copied in;
- "copier": a second Asterisk standing in for a Ricoh IP-Fax copier: it registers to Faxbot with the user name and
  password Faxbot issued, sends two pages to an outside number through Faxbot, and answers as a fax machine;
- "api": a stub that keeps the hand-over Faxbot's Asterisk makes.

Asserts the copier registered as its endpoint, that its fax ended at Faxbot with the dialled number and the copier's
ID (never bridged: Faxbot's trunk points at an address nothing answers), that the real Faxbot API queues that
hand-over as an outbound fax to that number from the copier's mailbox, and that a received fax Faxbot sends on to
the copier's extension arrives there with both pages. Nothing here reaches a carrier or a real copier.
"""
import os
from pathlib import Path
import time
import uuid

import pytest

from app import ami, copier_extensions, sip_trunk
from tests.test_t38_loopback import (AMI_PASSWORD, AMI_USER, CAPTURE, SECRET, UNREACHABLE, Docker, parse_ami,
                                     proof_pages, trunk_values, wait_booted)


pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_NATIVE_PROOF') != '1' or not os.environ.get('FAXBOT_NATIVE_IMAGE'),
                       reason='Set FAXBOT_NATIVE_PROOF=1 and FAXBOT_NATIVE_IMAGE to run the copier loopback proof.'),
]

ROOT = Path(__file__).resolve().parents[2]
DIALED = '+13035550199'
EXTENSION = '601'

COPIER_DIALPLAN = '''[copier-in]
exten => _[+0-9a-z].,1,Answer()
 same => n,ReceiveFAX(/tmp/copier-in.tiff,f)
 same => n,Hangup()
exten => s,1,Goto(copier-in,in,1)
'''

# Waits on the manager connection for one user event, as Faxbot's own connection would hear it.
SESSION = r'''
exec 3<>/dev/tcp/127.0.0.1/5038
cat >&3
deadline=$((SECONDS + 240))
seen=''
while [ "$SECONDS" -lt "$deadline" ]; do
  if IFS= read -r -t 5 line <&3; then
    printf '%s\n' "$line"
    case $line in
      *"$WANTED"*) seen=1 ;;
    esac
    if [ -n "$seen" ] && [ -z "${line%$'\r'}" ]; then exit 0; fi
  fi
done
exit 3
'''


def copier_conf(faxbot_address, password):
    """The copier's own SIP settings, as a Ricoh IM copier's IP-Fax SIP settings would be: register to Faxbot as
    the extension, digest authentication, and send ordinary fax numbers to it."""
    return '\n'.join([
        '[global]', 'type=global', 'user_agent=Synthetic-Copier', '',
        '[transport-udp]', 'type=transport', 'protocol=udp', 'bind=0.0.0.0', '',
        '[faxbot-auth]', 'type=auth', 'auth_type=userpass', f'username=copier-{EXTENSION}', f'password={password}',
        f'realm={copier_extensions.REALM}', '',
        '[faxbot]', 'type=aor', f'contact=sip:{faxbot_address}:5060', '',
        '[faxbot]', 'type=endpoint', 'transport=transport-udp', 'aors=faxbot', 'outbound_auth=faxbot-auth',
        'context=copier-in', 'disallow=all', 'allow=ulaw,alaw', 't38_udptl=yes', 't38_udptl_ec=redundancy',
        't38_udptl_maxdatagram=400', 'direct_media=no', f'from_user=copier-{EXTENSION}', '',
        '[faxbot-identify]', 'type=identify', 'endpoint=faxbot', f'match={faxbot_address}', '',
        '[faxbot-registration]', 'type=registration', 'outbound_auth=faxbot-auth',
        f'server_uri=sip:{faxbot_address}', f'client_uri=sip:copier-{EXTENSION}@{faxbot_address}',
        f'contact_user=copier-{EXTENSION}', 'retry_interval=5', 'expiration=120', 'transport=transport-udp', ''])


def session(docker, container, fields, wanted):
    actions = (f'Action: Login\r\nActionID: proof-login\r\nUsername: {AMI_USER}\r\n'
               f'Secret: {AMI_PASSWORD}\r\nEvents: call,user\r\n\r\n'
               + ''.join(f'{key}: {value}\r\n' for key, value in fields.items()) + '\r\n')
    result = docker.run('exec', '--interactive', '--env', f'WANTED={wanted}', container, 'bash', '-c', SESSION,
                        input_text=actions, check=False, timeout=300)
    return parse_ami(result.stdout)


def _wait(check, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(1)
    raise AssertionError(message)


def copier_exchange(tmp_path):
    docker = Docker()
    image = os.environ['FAXBOT_NATIVE_IMAGE']
    copier_id = uuid.uuid4().hex
    password = 'Synthetic-' + uuid.uuid4().hex
    copier = copier_extensions.Copier(copier_id, 'Front desk Ricoh', EXTENSION, f'copier-{EXTENSION}',
                                      copier_extensions.digest(f'copier-{EXTENSION}', password), None,
                                      ('+13035550161',), 'copier', 'active')
    try:
        docker.network = docker.prefix
        docker.run('network', 'create', '--internal', '--label', 'com.faxbot.scope=t38-proof', docker.network)
        capture = docker.start('api', 'python:3.11-slim', 'python', '-c', CAPTURE)
        faxbot = docker.start('faxbot', image, 'infinity', entrypoint='sleep')
        machine = docker.start('copier', image, 'infinity', entrypoint='sleep')
        addresses = {name: docker.address(container)
                     for name, container in (('api', capture), ('faxbot', faxbot), ('copier', machine))}
        assert all(addresses.values())
        # This checkout's dialplan and hand-over script, in both containers (the image may predate them).
        for container in (faxbot, machine):
            docker.run('cp', f'{ROOT}/asterisk/etc/asterisk/extensions.conf', f'{container}:/etc/asterisk/extensions.conf')
            docker.run('exec', container, 'mkdir', '-p', '/faxdata/asterisk', '/faxdata/outbound', '/faxdata/inbound')
        docker.run('cp', f'{ROOT}/asterisk/etc/asterisk/extensions-copier.conf',
                   f'{faxbot}:/etc/asterisk/extensions-copier.conf')
        docker.run('cp', f'{ROOT}/asterisk/bin/faxbot-inbound-notify', f'{faxbot}:/usr/local/bin/faxbot-inbound-notify')
        # Faxbot: its trunk points at an address nothing answers, then the copier exactly as Faxbot renders it.
        rendered = tmp_path / 'faxbot-pjsip.conf'
        rendered.write_text(sip_trunk.render_pjsip(trunk_values(UNREACHABLE)) + '\n\n'
                            + copier_extensions.render([copier]))
        docker.run('cp', str(rendered), f'{faxbot}:/faxdata/asterisk/pjsip.conf')
        # The copier: its own SIP settings and a context that answers as a fax machine.
        own = tmp_path / 'copier-pjsip.conf'
        own.write_text(copier_conf(addresses['faxbot'], password))
        docker.run('cp', str(own), f'{machine}:/faxdata/asterisk/pjsip.conf')
        plan = tmp_path / 'copier-extensions.conf'
        plan.write_text(COPIER_DIALPLAN)
        docker.run('cp', str(plan), f'{machine}:/etc/asterisk/extensions-copier.conf')
        environment = ['--env', f'ASTERISK_AMI_USERNAME={AMI_USER}', '--env', f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}']
        docker.run('exec', '--detach', *environment, '--env', f'ASTERISK_INBOUND_SECRET={SECRET}',
                   '--env', f'FAXBOT_API_URL=http://{addresses["api"]}:8080', faxbot,
                   'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        docker.run('exec', '--detach', *environment, machine, 'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        wait_booted(docker, faxbot)
        wait_booted(docker, machine)
        endpoint = docker.asterisk(faxbot, f'pjsip show endpoint copier-{EXTENSION}')
        try:
            contacts = _wait(lambda: (lambda text: text if f'copier-{EXTENSION}/sip:' in text else None)(
                docker.asterisk(faxbot, 'pjsip show contacts')), 60, 'The copier did not register to Faxbot')
        except AssertionError:
            raise AssertionError('The copier did not register to Faxbot.\n'
                                 + docker.asterisk(machine, 'pjsip show registrations') + '\n'
                                 + docker.asterisk(faxbot, 'pjsip show aors') + '\n'
                                 + docker.read(faxbot, '/tmp/asterisk.log')[-3000:] + '\n'
                                 + docker.read(machine, '/tmp/asterisk.log')[-3000:]) from None

        # The copier sends two pages to an outside number; Faxbot's engine receives them.
        pages = proof_pages()
        sent = tmp_path / 'copier-out.tiff'
        pages[0].save(sent, save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
        docker.run('cp', str(sent), f'{machine}:/tmp/copier-out.tiff')
        session(docker, machine, {'Action': 'Originate', 'ActionID': 'copier-send', 'Channel': f'PJSIP/{DIALED}@faxbot',
                                  'Application': 'SendFAX', 'Data': '/tmp/copier-out.tiff,f', 'Async': 'true',
                                  'Timeout': '30000'}, 'Event: OriginateResponse')
        captured = _wait(lambda: docker.read(capture, '/tmp/capture.json'), 120,
                         "Faxbot's Asterisk did not hand the copier's fax over")
        import json
        handover = json.loads(captured)
        received = tmp_path / 'handed.tiff'
        docker.run('cp', f"{faxbot}:{handover['body']['tiff_path']}", str(received))

        # Faxbot sends a received fax on to the copier's extension, exactly as the forwarder asks the engine to.
        forwarded = tmp_path / 'forward.tiff'
        pages[0].save(forwarded, save_all=True, append_images=pages[1:], compression='group4', dpi=(204, 196))
        docker.run('cp', str(forwarded), f'{faxbot}:/faxdata/inbound/forward.tiff')
        fields = {}

        async def keep(action):
            fields.update(action)
        client = ami.AMIClient.__new__(ami.AMIClient)
        client._send_action = keep
        import asyncio
        forward_id = uuid.uuid4().hex
        asyncio.run(client.originate_copier(forward_id, f'copier-{EXTENSION}', '/faxdata/inbound/forward.tiff'))
        events = session(docker, faxbot, fields, 'UserEvent: FaxCopierSent')
        if not any(event.get('UserEvent') == 'FaxCopierSent' for event in events):
            raise AssertionError('No FaxCopierSent.\n' + repr(fields) + '\n' + repr(events[-12:]) + '\n'
                                 + docker.read(faxbot, '/tmp/asterisk.log')[-2500:] + '\n'
                                 + docker.read(machine, '/tmp/asterisk.log')[-2500:])
        delivered = tmp_path / 'copier-in.tiff'
        docker.run('cp', f'{machine}:/tmp/copier-in.tiff', str(delivered), check=False)
        return {'endpoint': endpoint, 'contacts': contacts, 'handover': handover, 'received': received,
                'events': events, 'delivered': delivered, 'forward_id': forward_id, 'copier': copier,
                'password': password, 'log': docker.read(faxbot, '/tmp/asterisk.log')}
    finally:
        docker.close()


def _pages(path):
    from PIL import Image
    with Image.open(path) as image:
        return getattr(image, 'n_frames', 1)


def test_a_copier_registers_sends_through_faxbot_and_receives_what_is_forwarded(tmp_path, monkeypatch):
    outcome = copier_exchange(tmp_path)
    assert 'Contact:' in outcome['contacts'] and f'copier-{EXTENSION}' in outcome['contacts']
    assert 'contact_acl' in outcome['endpoint'] and 'faxbot-copier' in outcome['endpoint']
    body = outcome['handover']['body']
    assert outcome['handover']['path'] == '/_internal/asterisk/inbound' and outcome['handover']['secret'] == SECRET
    assert body['copier'] == outcome['copier'].id and body['dialed'] == DIALED and body['faxstatus'] == 'SUCCESS'
    assert _pages(outcome['received']) == 2
    sent_event = next(event for event in outcome['events'] if event.get('UserEvent') == 'FaxCopierSent')
    assert sent_event['Job'] == outcome['forward_id'] and sent_event['Status'] == 'SUCCESS'
    assert sent_event['Pages'] == '2' and _pages(outcome['delivered']) == 2

    # The real Faxbot API queues that hand-over as an outbound fax to the dialled number from the copier's mailbox.
    import sqlalchemy as sa
    from fastapi.testclient import TestClient
    from api.app.routing import envelope as envelopes
    from api.tests.test_access_management_http import B, ORIGIN, _environment
    from api.tests.test_work_http import mailbox
    from app.main import app
    (tmp_path / 'api').mkdir()
    _environment(monkeypatch, tmp_path / 'api')
    for name, value in {'FAX_BACKEND': 'sip', 'FAX_OUTBOUND_BACKEND': 'sip', 'FAX_INBOUND_BACKEND': 'sip',
                        'INBOUND_ENABLED': 'true', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                        'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1', 'SIP_TRUNK_CALLER_ID': '+13035550160',
                        'SIP_TRUNK_DIDS': '+13035550160,+13035550161', 'AMI_PASSWORD': 'synthetic-ami-pass',
                        'ASTERISK_INBOUND_SECRET': 'sekret'}.items():
        monkeypatch.setenv(name, value)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as client:
        box = mailbox(client, 'Front desk', '+13035550161')
        created = client.post('/admin/sip/copiers/extensions', headers=B, json={
            'name': 'Front desk Ricoh', 'extension': EXTENSION, 'mailbox_id': box['id'],
            'numbers': ['+13035550161']}).json()
        inbound = Path(os.environ['FAX_DATA_DIR']) / 'inbound'
        inbound.mkdir(parents=True, exist_ok=True)
        image = inbound / Path(body['tiff_path']).name
        image.write_bytes(outcome['received'].read_bytes())
        handed = client.post('/_internal/asterisk/inbound', headers={'X-Internal-Secret': 'sekret'},
                             json={**body, 'tiff_path': str(image), 'copier': created['id']})
        assert handed.status_code == 200, handed.text
        job = handed.json()['queued']
        engine = app.state.configuration_runtime.manager.store.engine
        with engine.connect() as connection:
            row = connection.execute(sa.text('SELECT to_number, pages FROM fax_jobs WHERE id = :id'),
                                     {'id': job}).one()
        assert tuple(row) == (DIALED, 2)
        assert envelopes.load(engine, job).facts.mailbox_id == box['id']
