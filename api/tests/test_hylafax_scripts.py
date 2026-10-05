"""The SSL Fax engine's receive and hand-over scripts, with stand-in tools and an explicit PATH (no container).

hylafax/bin/received runs as uucp when a fax arrives: it copies the image and
writes a ticket into the engine's volume. hylafax/bin/handover (also uucp)
puts the fax in the engine's out folder and hands it over. hylafax/bin/notify
keeps each job's result in the engine's volume and hylafax/bin/deliver sends
kept reports until Faxbot takes them. Linux CI runs these with the same small
set of tools.
"""
import base64
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ('sh', 'sed', 'awk', 'base64', 'tr', 'head', 'cut', 'cat', 'grep', 'cp', 'mv', 'rm', 'mkdir', 'chmod',
         'mktemp', 'dirname', 'tail')

pytestmark = pytest.mark.skipif(not all(shutil.which(tool) for tool in TOOLS),
                                reason='POSIX tools are needed to run the engine scripts.')


def _stub(folder, name, body):
    path = folder / name
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)


@pytest.fixture
def engine(tmp_path):
    spool, state, data, tools = tmp_path / 'spool', tmp_path / 'state', tmp_path / 'faxdata', tmp_path / 'tools'
    for folder in (spool / 'recvq', spool / 'log', spool / 'etc', spool / 'doneq', state / 'received',
                   state / 'results', data / 'hylafax-out' / 'inbound', tools):
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
    _stub(tools, 'date', 'echo 1791180000\n')
    # The stand-in Faxbot gives the next line of CAPTURE/answers, else CAPTURE/answer, and records each request.
    _stub(tools, 'curl', 'cat > "$CAPTURE/header.$$"; for a; do last=$a; done\n'
                         'while [ $# -gt 0 ]; do if [ "$1" = --data-binary ]; then case $2 in '
                         '@*) cat "${2#@}" > "$CAPTURE/body" ;; *) printf %s "$2" > "$CAPTURE/body" ;; esac; fi; '
                         'shift; done\nprintf %s "$last" > "$CAPTURE/url"; printf \'%s\\n\' "$last" >> "$CAPTURE/urls"\n'
                         'if [ -s "$CAPTURE/answers" ]; then head -1 "$CAPTURE/answers"; '
                         'tail -n +2 "$CAPTURE/answers" > "$CAPTURE/answers.next"; '
                         'mv "$CAPTURE/answers.next" "$CAPTURE/answers"; else cat "$CAPTURE/answer"; fi\n')
    _stub(tools, 'flock', 'exit 0\n')
    for tool in TOOLS:
        (tools / tool).symlink_to(shutil.which(tool))
    environment = {'PATH': str(tools), 'CAPTURE': str(tmp_path), 'FAXBOT_DATA': str(data),
                   'FAXBOT_HYLAFAX_SPOOL': str(spool), 'FAXBOT_ENGINE_STATE': str(state)}
    return spool, state, data, environment


def run(script, environment, *args, cwd=None):
    return subprocess.run(['sh', str(ROOT / 'hylafax' / 'bin' / script), *args], cwd=cwd, env=environment,
                          capture_output=True, text=True, timeout=30)


def test_a_received_fax_is_kept_in_the_engine_volume_and_handed_over_once_faxbot_answers(engine, tmp_path):
    spool, state, data, environment = engine
    received = run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '000000007', '',
                   '+15555550199', '179117219142.15555550100', '', cwd=spool)
    assert received.returncode == 0, received.stderr
    # The image copy and the ticket live in the engine's volume, named with the arrival time.
    ticket = (state / 'received' / '000000007-1791180000.ticket').read_text()
    assert (state / 'received' / '000000007-1791180000.tif').read_bytes() == b'II*\x00jbig image'
    assert 'token=179117219142' in ticket and 'sslfax=true' in ticket and 'offered=true' in ticket
    (tmp_path / 'answer').write_text('503')
    assert run('handover', environment).returncode == 0
    # Faxbot was starting: ticket and image stay for the next round; the G4 copy is already in place.
    assert (state / 'received' / '000000007-1791180000.ticket').exists()
    # In the engine's out folder (the only one Faxbot reads from the engine), never Faxbot's own inbound folder.
    stored = data / 'hylafax-out' / 'inbound' / 'engine-0123456789abcdef-000000007-1791180000.tiff'
    assert stored.exists() and not (data / 'inbound').exists()
    (tmp_path / 'answer').write_text('200')
    assert run('handover', environment).returncode == 0
    assert not list((state / 'received').iterdir())
    assert not (spool / 'recvq' / 'fax000000007.tif').exists()
    body = json.loads((tmp_path / 'body').read_text())
    assert (tmp_path / 'url').read_text() == 'http://api:8080/_internal/hylafax/inbound'
    assert body['tiff_path'] == str(stored) and body['uniqueid'] == 'engine.179117219142'
    assert body['to_number'] == '+15555550100' and body['from_number'] == '+15555550199'
    assert body['faxstatus'] == 'SUCCESS' and body['faxpages'] == 2
    assert body['call']['did'] == '+15555550100' and body['call']['t38'] is None
    assert body['engine'] == {'engine': 'hylafax', 'engine_ref': '0123456789abcdef:000000007-1791180000',
                              'sslfax': True, 'sslfax_offered': True, 'transfer_seconds': 12,
                              'signal_rate_b64': base64.b64encode(b'SSL Fax').decode(),
                              'data_format_b64': base64.b64encode(b'JBIG').decode()}
    # The secret went in a header from standard input, never on the command line.
    headers = ''.join(path.read_text() for path in tmp_path.glob('header.*'))
    assert 'X-Internal-Secret: synthetic-secret-value' in headers


def test_a_ticket_left_from_an_older_container_is_handed_over_from_the_volume(engine, tmp_path):
    spool, state, data, environment = engine
    assert run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '000000007', '',
               '+15555550199', '5.15555550100', cwd=spool).returncode == 0
    # A new container: the spool (and its copy of the image) is gone; the volume is not.
    (spool / 'recvq' / 'fax000000007.tif').unlink()
    (tmp_path / 'answer').write_text('200')
    assert run('handover', environment).returncode == 0
    assert not list((state / 'received').iterdir())
    assert json.loads((tmp_path / 'body').read_text())['uniqueid'] == 'engine.5'


def test_without_asterisks_call_name_the_communication_id_and_arrival_keep_the_fax_unique(engine, tmp_path):
    spool, state, data, environment = engine
    assert run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '000000007', 'Remote hung up',
               cwd=spool).returncode == 0
    (tmp_path / 'answer').write_text('200')
    assert run('handover', environment).returncode == 0
    body = json.loads((tmp_path / 'body').read_text())
    assert body['uniqueid'] == 'hylafax.0123456789abcdef.000000007-1791180000'
    assert body['faxstatus'] == 'FAILED' and body['to_number'] is None


def test_the_receive_script_refuses_files_outside_the_receive_queue(engine):
    spool, state, _, environment = engine
    result = run('received', environment, '../etc/faxbot.conf', 'ttyIAX1', '7', '', cwd=spool)
    assert result.returncode == 1 and not list((state / 'received').iterdir())


def test_a_received_image_or_ticket_that_is_a_link_is_not_handed_over(engine, tmp_path):
    spool, state, data, environment = engine
    secret = tmp_path / 'secret.tif'
    secret.write_bytes(b'not the fax')
    (state / 'received' / '1-1.tif').symlink_to(secret)
    (state / 'received' / '1-1.ticket').write_text('key=1-1\ncommid=1\n')
    (tmp_path / 'answer').write_text('200')
    assert run('handover', environment).returncode == 0
    assert not (tmp_path / 'url').exists() and not list((data / 'hylafax-out' / 'inbound').iterdir())


QFILE = ('jobtag:' + 'a' * 32 + '.' + 'b' * 32 + '\njobid:12\ncommid:000000007\nstate:8\nnpages:0\ntotpages:2\n'
         'ndials:1\ntotdials:1\ntottries:1\nstatus:No carrier detected {E002}\nstatuscode:E002\ncsi:\n'
         'signalrate:\ndataformat:\n')


def test_a_job_result_is_kept_in_the_engine_volume_until_faxbot_takes_it(engine, tmp_path):
    spool, state, data, environment = engine
    (spool / 'doneq' / 'q12').write_text(QFILE)
    # Faxbot is down for longer than notify waits: the result stays in the volume (no secret in it).
    (tmp_path / 'answer').write_text('503')
    assert run('notify', environment, 'doneq/q12', 'failed', '0:00:41', cwd=spool).returncode == 0
    kept = state / 'results' / '1791180000-job12-failed.report'
    report = json.loads(kept.read_text())
    assert report['tag'] == 'a' * 32 + '.' + 'b' * 32 and report['status_code'] == 'E002'
    assert 'synthetic-secret-value' not in kept.read_text()
    # The engine's next round, once Faxbot is back: sent once, then gone.
    (tmp_path / 'answer').write_text('200')
    assert run('deliver', environment).returncode == 0
    assert not kept.exists()
    assert json.loads((tmp_path / 'body').read_text()) == report
    assert (tmp_path / 'urls').read_text().splitlines() == ['http://api:8080/_internal/hylafax/result'] * 2
    headers = ''.join(path.read_text() for path in tmp_path.glob('header.*'))
    assert 'X-Internal-Secret: synthetic-secret-value' in headers


def test_the_engines_start_is_reported_and_a_malformed_report_is_set_aside(engine, tmp_path):
    spool, state, data, environment = engine
    (state / 'results' / '1791180000-started.report').write_text('{"engine_id":"0123456789abcdef","started":1}\n')
    (state / 'results' / '1791180001-job12-failed.report').write_text('{"tag":"x"}\n')
    (tmp_path / 'answer').write_text('400')
    assert run('deliver', environment).returncode == 0
    assert (tmp_path / 'urls').read_text().splitlines() == ['http://api:8080/_internal/hylafax/started',
                                                            'http://api:8080/_internal/hylafax/result']
    # Faxbot refused both as malformed: kept aside for a person, never sent again.
    assert sorted(path.name for path in (state / 'results' / 'refused').iterdir()) == [
        '1791180000-started.report', '1791180001-job12-failed.report']
    assert run('deliver', environment).returncode == 0
    assert len((tmp_path / 'urls').read_text().splitlines()) == 2


def test_one_report_faxbot_cannot_take_does_not_hold_up_the_others(engine, tmp_path):
    spool, state, data, environment = engine
    first, second = state / 'results' / '1791180000-job11-failed.report', state / 'results' / '1791180001-job12-done.report'
    first.write_text('{"tag":"one"}\n')
    second.write_text('{"tag":"two"}\n')
    # A server error on the oldest report: it stays, and the next one is still sent.
    (tmp_path / 'answers').write_text('500\n200\n')
    assert run('deliver', environment).returncode == 1
    assert first.exists() and not second.exists()
    # Faxbot unreachable: the round stops at once and every report waits.
    third = state / 'results' / '1791180002-job13-done.report'
    third.write_text('{"tag":"three"}\n')
    (tmp_path / 'answers').write_text('000\n200\n')
    assert run('deliver', environment).returncode == 1
    assert first.exists() and third.exists()
    assert len((tmp_path / 'urls').read_text().splitlines()) == 3
