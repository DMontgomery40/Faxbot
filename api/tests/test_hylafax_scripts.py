"""The SSL Fax engine's receive and hand-over scripts, with stand-in tools and an explicit PATH (no container).

hylafax/bin/received runs as uucp when a fax arrives and writes a ticket;
hylafax/bin/handover runs as root and brings the fax into Faxbot's data folder
and hands it over. Linux CI runs these with the same small set of tools.
"""
import base64
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ('sh', 'sed', 'awk', 'base64', 'tr', 'head', 'cut', 'cat', 'grep', 'cp', 'mv', 'rm', 'mkdir', 'chmod',
         'date')

pytestmark = pytest.mark.skipif(not all(shutil.which(tool) for tool in TOOLS),
                                reason='POSIX tools are needed to run the engine scripts.')


def _stub(folder, name, body):
    path = folder / name
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)


@pytest.fixture
def engine(tmp_path):
    spool, data, tools = tmp_path / 'spool', tmp_path / 'faxdata', tmp_path / 'tools'
    for folder in (spool / 'recvq', spool / 'log', spool / 'etc', spool / 'faxbot-received', data, tools):
        folder.mkdir(parents=True)
    (spool / 'recvq' / 'fax000000007.tif').write_bytes(b'II*\x00jbig image')
    (spool / 'log' / 'c000000007').write_text(
        'Oct 05 01:00:00.00: [ 1]: REMOTE TSA "ssl://(passcode hidden)@x:1"\n'
        'Oct 05 01:00:01.00: [ 1]: SSL Fax connection was successful.\n')
    (spool / 'etc' / 'faxbot.conf').write_text(
        'url=http://api:8080\nsecret=synthetic-secret-value\nengine=0123456789abcdef\n')
    _stub(tools, 'faxinfo', "printf '%s\\n' 'x:' '    Sender: +1 555 555 0199' '     Pages: 2' "
                            "'SignalRate: SSL Fax' 'DataFormat: JBIG' 'TimeToRecv: 0:00:12'\n")
    _stub(tools, 'tiffcp', 'cp "$3" "$4"\n')
    # The stand-in Faxbot gives the answer in CAPTURE/answer and records each request.
    _stub(tools, 'curl', 'cat > "$CAPTURE/header.$$"; for a; do last=$a; done\n'
                         'while [ $# -gt 0 ]; do [ "$1" = --data-binary ] && printf %s "$2" > "$CAPTURE/body"; '
                         'shift; done\nprintf %s "$last" > "$CAPTURE/url"; cat "$CAPTURE/answer"\n')
    for tool in TOOLS:
        (tools / tool).symlink_to(shutil.which(tool))
    environment = {'PATH': str(tools), 'CAPTURE': str(tmp_path), 'FAXBOT_DATA': str(data),
                   'FAXBOT_HYLAFAX_SPOOL': str(spool)}
    return spool, data, environment


def run(script, environment, *args, cwd=None):
    return subprocess.run(['sh', str(ROOT / 'hylafax' / 'bin' / script), *args], cwd=cwd, env=environment,
                          capture_output=True, text=True, timeout=30)


def test_a_received_fax_becomes_a_ticket_and_is_handed_over_once_faxbot_answers(engine, tmp_path):
    spool, data, environment = engine
    received = run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '000000007', '',
                   '+15555550199', '179117219142.15555550100', '', cwd=spool)
    assert received.returncode == 0, received.stderr
    ticket = (spool / 'faxbot-received' / '000000007.ticket').read_text()
    assert 'token=179117219142' in ticket and 'sslfax=true' in ticket and 'offered=true' in ticket
    (tmp_path / 'answer').write_text('503')
    assert run('handover', environment).returncode == 0
    # Faxbot was starting: ticket and image stay for the next round; the G4 copy is already in place.
    assert (spool / 'faxbot-received' / '000000007.ticket').exists()
    stored = data / 'inbound' / 'engine-0123456789abcdef-000000007.tiff'
    assert stored.exists()
    (tmp_path / 'answer').write_text('200')
    assert run('handover', environment).returncode == 0
    assert not (spool / 'faxbot-received' / '000000007.ticket').exists()
    assert not (spool / 'recvq' / 'fax000000007.tif').exists()
    body = json.loads((tmp_path / 'body').read_text())
    assert (tmp_path / 'url').read_text() == 'http://api:8080/_internal/asterisk/inbound'
    assert body['tiff_path'] == str(stored) and body['uniqueid'] == 'engine.179117219142'
    assert body['to_number'] == '+15555550100' and body['from_number'] == '+15555550199'
    assert body['faxstatus'] == 'SUCCESS' and body['faxpages'] == 2
    assert body['call']['did'] == '+15555550100' and body['call']['t38'] is None
    assert body['engine'] == {'engine': 'hylafax', 'engine_ref': '0123456789abcdef:000000007', 'sslfax': True,
                              'sslfax_offered': True, 'transfer_seconds': 12,
                              'signal_rate_b64': base64.b64encode(b'SSL Fax').decode(),
                              'data_format_b64': base64.b64encode(b'JBIG').decode()}
    # The secret went in a header from standard input, never on the command line.
    headers = ''.join(path.read_text() for path in tmp_path.glob('header.*'))
    assert 'X-Internal-Secret: synthetic-secret-value' in headers


def test_without_asterisks_call_name_the_communication_id_keeps_the_fax_unique(engine, tmp_path):
    spool, data, environment = engine
    assert run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '000000007', 'Remote hung up',
               cwd=spool).returncode == 0
    (tmp_path / 'answer').write_text('200')
    assert run('handover', environment).returncode == 0
    body = json.loads((tmp_path / 'body').read_text())
    assert body['uniqueid'] == 'hylafax.0123456789abcdef.000000007'
    assert body['faxstatus'] == 'FAILED' and body['to_number'] is None


def test_the_receive_script_refuses_files_outside_the_receive_queue(engine):
    spool, _, environment = engine
    result = run('received', environment, '../etc/faxbot.conf', 'ttyIAX1', '7', '', cwd=spool)
    assert result.returncode == 1 and not list((spool / 'faxbot-received').iterdir())
