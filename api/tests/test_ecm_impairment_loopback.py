"""Brief 98 item 1, proven on the wire: the error-correction frame size and T.38 protection Faxbot learns from
impaired calls, used on the next ones, between two Faxbot Asterisk containers (the built-in engine at both ends)
over T.38, with loss added by tc netem on both containers' interfaces inside the colima VM.

Runs only with FAXBOT_NATIVE_PROOF=1 and FAXBOT_IMPAIRMENT_PROOF=1 (it places about twenty loopback calls).
FAXBOT_NATIVE_IMAGE names the Asterisk image with patch 0008 (built from asterisk/ otherwise).

The impairment is netem's two-state (Gilbert-Elliott) loss on every packet except SIP: from the good state a
packet starts a burst with probability ``P``, inside a burst every packet is lost and the burst ends with
probability ``R`` (mean burst 1 / R packets). UDPTL redundancy (three earlier packets in each, udptl.conf) repairs
bursts of up to three packets when a later packet arrives; longer bursts spoil error-correction frames (the
receiver asks for them again) or a control exchange (T.30 waits and tries again).

1. Learning: calls with Faxbot's usual settings (256-octet frames, each packet once). Their FaxFrames events
   (patch 0008: ``Ecm``, ``T38Rx``) become ``fax_call_ecm`` rows in a scratch database, and engine learning's own
   functions choose the frame size (``engine_ecm.learned``) and the protection (``engine_ecm.protection`` from
   ``t38_loss``), exactly as they would on an installation.
2. Comparison: the same document with each of the four combinations (frame 256 or 64, protection off or on), the
   same number of calls each, the netem seed differing per call. For each: the call's seconds on the line (answer to
   hang-up), the requests to send frames again and the frames resent (the sender's own count), the packets still
   missing after redundancy, and whether the fax arrived whole.

Printed as JSON (``ECM_IMPAIRMENT_PROOF``). The assertions check the mechanism: the learned settings reach the
call, the sender's DCS asks for 64-octet frames when told, protection is echoed by the session and duplicate packets
are dropped by the receiver. Whether the learned settings beat the usual ones is the measurement, reported as it
came out (a null result is a result: the research's own rule says a fixed 256-octet baseline that matches or beats
the adaptive policy fails it).
"""
from __future__ import annotations

import json
import os

import random
import statistics
import time
import uuid

import pytest

from app import ami, engine_ecm, engine_frames, sip_trunk
from app.schema import create_database_engine, upgrade_schema
from api.tests.test_t38_loopback import (AMI_PASSWORD, AMI_SESSION, AMI_USER, CAPTURE, DID, ROOT, SECRET, Docker,
                                         parse_ami, trunk_values, wait_booted)

pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_NATIVE_PROOF') != '1' or os.environ.get('FAXBOT_IMPAIRMENT_PROOF') != '1',
                       reason='Set FAXBOT_NATIVE_PROOF=1 and FAXBOT_IMPAIRMENT_PROOF=1 to run the impairment proof.'),
]

# Bursts start on 1% of packets and last 2.5 packets on average (R = 40%).
LOSS = os.environ.get('FAXBOT_IMPAIRMENT_LOSS', 'gemodel 1% 40% 100% 0%')
CALLS = int(os.environ.get('FAXBOT_IMPAIRMENT_CALLS', '4'))
AMI_SESSION_BOTH = AMI_SESSION.replace("*'UserEvent: FaxResult'*) seen=1 ;;", "*'UserEvent: FaxFrames'*) seen=1 ;;")


def document(path, *, pages=3, rows=45, seed=11):
    """Pages of scattered dots in a band of ``rows`` rows: each about 16 KB in MH, like a dense text page."""
    from PIL import Image
    rng = random.Random(seed)
    frames = []
    for _ in range(pages):
        frame = Image.new('1', (1728, 1100), 1)
        pixels = frame.load()
        for y in range(100, 100 + rows):
            for x in range(0, 1728, 2):
                if rng.random() < 0.35:
                    pixels[x, y] = 0
        frames.append(frame)
    frames[0].save(path, save_all=True, append_images=frames[1:], compression='group4', dpi=(204, 196))
    return path


def impair(docker, container, image, seed):
    """netem on the container's interface (a sidecar in its network namespace; the Asterisk container itself gets
    no privilege): every packet but SIP goes through the loss model."""
    sidecar = f'{container}-netem'
    if sidecar not in docker.containers:
        docker.run('run', '--detach', '--name', sidecar, '--network', f'container:{container}', '--cap-add',
                   'NET_ADMIN', '--label', 'com.faxbot.scope=t38-proof', '--entrypoint', 'sleep', image, 'infinity')
        docker.containers.append(sidecar)
    script = ('tc qdisc del dev eth0 root 2>/dev/null; '
              'tc qdisc add dev eth0 root handle 1: prio bands 3 priomap 2 2 2 2 2 2 2 2 2 2 2 2 2 2 2 2 && '
              f'tc qdisc add dev eth0 parent 1:3 handle 30: netem loss {LOSS} seed {seed} && '
              'tc filter add dev eth0 parent 1: protocol ip prio 1 u32 match ip dport 5060 0xffff flowid 1:1 && '
              'tc filter add dev eth0 parent 1: protocol ip prio 1 u32 match ip sport 5060 0xffff flowid 1:1')
    result = docker.run('exec', sidecar, 'sh', '-c', script, check=False)
    assert result.returncode == 0, result.stderr


def call(docker, sender, values, tiff, variables):
    """One fax from the sender to the receiver with the production Originate fields and ``variables``; the
    sender's FaxResult and FaxFrames events."""
    job, attempt = uuid.uuid4().hex, uuid.uuid4().hex
    fields = ami.originate_fields_for(values, job, DID, tiff, attempt_id=attempt)
    extra = {'FAXBOT_COMPRESSION': 'mh', **variables}
    fields['Variable'] += ''.join(f',{name}={value}' for name, value in extra.items() if value)
    actions = (f'Action: Login\r\nActionID: proof-login\r\nUsername: {AMI_USER}\r\n'
               f'Secret: {AMI_PASSWORD}\r\nEvents: call,user\r\n\r\n'
               + ''.join(f'{key}: {value}\r\n' for key, value in fields.items()) + '\r\n')
    session = docker.run('exec', '--interactive', sender, 'bash', '-c', AMI_SESSION_BOTH, input_text=actions,
                         check=False, timeout=400)
    events = parse_ami(session.stdout)
    result = next((event for event in events if event.get('UserEvent') == 'FaxResult'), None)
    frames = next((event for event in events if event.get('UserEvent') == 'FaxFrames'), None)
    return result, frames


def measured(result, frames):
    seconds = None
    if result and result.get('Answered') and result.get('Ended'):
        seconds = int(result['Ended']) - int(result['Answered'])
    ecm = engine_ecm.parse_ecm((frames or {}).get('Ecm')) or {}
    rx = engine_ecm.parse_t38_rx((frames or {}).get('T38Rx')) or {}
    dcs = engine_frames.decode_dcs((frames or {}).get('DcsLast')) if (frames or {}).get('DcsLast') else None
    return {'status': (result or {}).get('Status'), 'pages': int((result or {}).get('Pages') or 0), 'seconds': seconds,
            'frame': ecm.get('frame_octets'), 'small_asked': ecm.get('small_frames'), 'ppr': ecm.get('ppr'),
            'bad': ecm.get('bad_frames'), 'runs': ecm.get('bad_runs'), 'resent': ecm.get('resent'),
            'ctc': ecm.get('ctc'), 'edge_ms': ecm.get('edge_ms'), 'edges': ecm.get('edges'),
            'round_ms': ecm.get('round_ms'), 'rounds': ecm.get('rounds'), 't38_received': rx.get('t38_received'),
            't38_recovered': rx.get('t38_recovered'), 't38_missing': rx.get('t38_missing'),
            'protect_used': (frames or {}).get('T38Protect') or None, 'dcs_ecm': (dcs or {}).get('ecm')}


def summary(rows):
    def spread(name):
        found = [row[name] for row in rows if row[name] is not None]
        return {'median': statistics.median(found), 'min': min(found), 'max': max(found)} if found else None
    return {'calls': len(rows), 'delivered': sum(1 for row in rows if row['status'] == 'SUCCESS'),
            'seconds': spread('seconds'), 'resent': spread('resent'), 'ppr': spread('ppr'),
            't38_missing': spread('t38_missing'), 'ctc': spread('ctc')}


def test_learned_frame_size_and_t38_protection_against_the_usual_settings_under_bursty_loss(tmp_path):
    docker = Docker()
    image = os.environ.get('FAXBOT_NATIVE_IMAGE') or 'faxbot-native:t38-proof'
    if not os.environ.get('FAXBOT_NATIVE_IMAGE'):
        docker.run('build', '--quiet', '--tag', image, str(ROOT / 'asterisk'), timeout=3600)
    database = create_database_engine('sqlite:///' + str(tmp_path / 'learning.db'))
    upgrade_schema(database)
    store = engine_ecm.EcmStore(database)
    evidence = {'impairment': LOSS, 'calls_per_setting': CALLS, 'image': image}
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
        docker.run('exec', '--detach', '--env', f'ASTERISK_INBOUND_SECRET={SECRET}', '--env',
                   f'FAXBOT_API_URL=http://{addresses["api"]}:8080', receiver, 'sh', '-c',
                   '/start.sh > /tmp/asterisk.log 2>&1')
        docker.run('exec', '--detach', '--env', f'ASTERISK_AMI_USERNAME={AMI_USER}', '--env',
                   f'ASTERISK_AMI_PASSWORD={AMI_PASSWORD}', sender, 'sh', '-c', '/start.sh > /tmp/asterisk.log 2>&1')
        wait_booted(docker, receiver)
        wait_booted(docker, sender)
        tiff = document(tmp_path / 'dense.tiff')
        docker.run('cp', str(tiff), f'{sender}:/faxdata/outbound/dense.tiff')
        values = trunk_values(addresses['receiver'], FAX_LOCAL_STATION_ID='+15555550100', FAX_HEADER='Faxbot proof')
        seed = 1000

        def run(name, variables):
            nonlocal seed
            rows = []
            for _ in range(CALLS):
                seed += 1
                impair(docker, sender, image, seed)
                impair(docker, receiver, image, seed + 500)
                result, frames = call(docker, sender, values, '/faxdata/outbound/dense.tiff', variables)
                row = measured(result, frames)
                rows.append(row)
                kept = engine_ecm.frames_row(frames or {})
                if kept is not None and name == 'usual':
                    kept['number'] = DID
                    store.record(kept)
                time.sleep(3)
            evidence[name] = {'calls': rows, 'summary': summary(rows)}
            return rows

        run('usual', {})
        # What engine learning chooses from those calls (the same functions an installation runs).
        since = time.time() - 3600
        from datetime import datetime
        moment = datetime.utcfromtimestamp(since)
        learned = engine_ecm.learned(database, DID, since=moment)
        rows = store.rows(direction=None, since=moment)
        loss, residual, measured_calls, packets = engine_ecm.t38_loss(rows)
        protect = engine_ecm.protection(loss, measured_calls)
        shape = learned.shape
        evidence['learned'] = {
            'frame_octets': learned.frame_octets, 'frame_why': learned.frame_why, 'protect': protect,
            'loss_percent': None if loss is None else round(loss * 100, 2),
            'residual_percent': None if residual is None else round(residual * 100, 3), 'packets': packets,
            'turnaround_seconds': learned.block_seconds,
            'shape': None if shape is None else {'calls': shape.calls, 'runs': shape.runs, 'bad': shape.bad,
                                                 'bursts_per_octet': shape.bursts_per_octet,
                                                 'burst_octets': shape.burst_octets}}
        chosen_protect = protect or engine_ecm.PROTECT_LOW
        settings = {'frame64': {'FAXBOT_ECM_FRAME': '64'},
                    'protected': {'FAXBOT_T38_PROTECT': chosen_protect},
                    'frame64_protected': {'FAXBOT_ECM_FRAME': '64', 'FAXBOT_T38_PROTECT': chosen_protect}}
        for name, variables in settings.items():
            run(name, variables)
        learned_name = {(256, None): 'usual', (64, None): 'frame64', (256, True): 'protected',
                        (64, True): 'frame64_protected'}[(learned.frame_octets, True if protect else None)]
        evidence['learned_setting'] = learned_name
        receiver_log = docker.read(receiver, '/tmp/asterisk.log')
        evidence['receiver_errors'] = [line for line in receiver_log.splitlines()
                                       if 'ERROR' in line and 'udptl' in line.lower()][-5:]
    finally:
        docker.close()
    print('\nECM_IMPAIRMENT_PROOF ' + json.dumps(evidence, indent=2, default=str))
    # The mechanism: settings reach the call, and the sender's DCS asks for what it was told.
    for row in evidence['frame64']['calls'] + evidence['frame64_protected']['calls']:
        if row['frame']:
            assert row['frame'] == 64 and row['small_asked'] == 1, row
    for row in evidence['usual']['calls'] + evidence['protected']['calls']:
        if row['frame']:
            assert row['frame'] == 256 and row['small_asked'] == 0, row
    for row in evidence['protected']['calls'] + evidence['frame64_protected']['calls']:
        if row['status']:
            assert row['protect_used'] == chosen_protect, row
    for row in evidence['usual']['calls']:
        assert row['protect_used'] is None, row
    assert evidence['usual']['summary']['calls'] == CALLS and evidence['learned']['packets'] > 0
    assert not evidence['receiver_errors'], evidence['receiver_errors']
