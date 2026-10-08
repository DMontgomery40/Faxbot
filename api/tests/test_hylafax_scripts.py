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
import os
import shutil
import signal
import subprocess
import time

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
    # Numbers as the call carried them (a plus sign only when it had one); Faxbot reads them for its country.
    assert body['to_number'] == '15555550100' and body['from_number'] == '+15555550199'
    assert body['faxstatus'] == 'SUCCESS' and body['faxpages'] == 2
    assert body['call']['did'] == '15555550100' and body['call']['t38'] is None
    negotiation = json.loads(base64.b64decode(body['engine'].pop('negotiation_b64')))
    assert body['engine'] == {'engine': 'hylafax', 'engine_ref': '0123456789abcdef:000000007-1791180000',
                              'sslfax': True, 'sslfax_offered': True, 'transfer_seconds': 12,
                              'signal_rate_b64': base64.b64encode(b'SSL Fax').decode(),
                              'data_format_b64': base64.b64encode(b'JBIG').decode(),
                              # The far end's internet fax address from its TSA: host and port only.
                              'remote_address_b64': base64.b64encode(b'x:1').decode()}
    # Over SSL Fax there is no speed; the two log lines name no compression or resolution either.
    assert negotiation == {'rate_first': None, 'rate_lowest': None, 'rate_last': None, 'trainings': None,
                           'compression': None, 'resolution': None, 'ecm': None, 'session': None,
                           'page_length': None, 'page_width': None, 'fine': None, 'remote_ecm': None,
                           'scan_ms': None, 'remote_codings': None, 'boundary_ms': None, 'boundaries': None}
    # The secret went in a header from standard input, never on the command line.
    headers = ''.join(path.read_text() for path in tmp_path.glob('header.*'))
    assert 'X-Internal-Secret: synthetic-secret-value' in headers


@pytest.mark.parametrize('faxinfo, log, stated', [
    ("'    Sender: +1 555 555 0199' '   SubAddr: 20 01' '     Pages: 2'", '', '2001'),
    # Without faxinfo's SubAddr, the session log's line; "<unspecified>" is no subaddress.
    ("'    Sender: +1 555 555 0199' '     Pages: 2'",
     'RECV FAX (000000007): recvq/fax000000007.tif from 5550199, subaddress <2002>, 2 pages in 0:00:12\n', '2002'),
    ("'    Sender: +1 555 555 0199' '     Pages: 2'",
     'RECV FAX (000000007): recvq/fax000000007.tif from 5550199, subaddress <unspecified>, 2 pages\n', None),
])
def test_the_subaddress_the_sender_stated_reaches_faxbot(engine, tmp_path, faxinfo, log, stated):
    """Faxbot's number rules can route by it (it is never proof of who sent the fax)."""
    spool, state, data, environment = engine
    _stub(tmp_path / 'tools', 'faxinfo', f"printf '%s\\n' 'x:' {faxinfo}\n")
    with (spool / 'log' / 'c000000007').open('a') as session:
        session.write(log)
    assert run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '000000007', '',
               '+15555550199', '5.15555550100', cwd=spool).returncode == 0
    (tmp_path / 'answer').write_text('200')
    assert run('handover', environment).returncode == 0
    assert json.loads((tmp_path / 'body').read_text())['subaddress'] == stated


@pytest.mark.parametrize('line, address', [
    ('REMOTE TSA "ssl://synthetic-passcode@fax.partner.example:10443"', b'fax.partner.example:10443'),
    ('REMOTE TSA "ssl://192.0.2.7:10443"', b'192.0.2.7:10443'),
    ('REMOTE CSA "ssl://(passcode hidden)@x:1"', None),
])
def test_a_received_calls_internet_fax_address_reaches_faxbot_without_its_passcode(engine, tmp_path, line, address):
    """For partner discovery, as sent calls report it: the TSA's host and port, never the passcode."""
    spool, state, data, environment = engine
    (spool / 'log' / 'c000000007').write_text(f'Oct 05 01:00:00.00: [ 1]: {line}\n')
    assert run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '000000007', '',
               '+15555550199', '5.15555550100', cwd=spool).returncode == 0
    ticket = next((state / 'received').glob('*.ticket')).read_text()
    assert 'synthetic-passcode' not in ticket
    (tmp_path / 'answer').write_text('200')
    assert run('handover', environment).returncode == 0
    body = (tmp_path / 'body').read_text()
    assert 'synthetic-passcode' not in body
    expected = base64.b64encode(address).decode() if address else None
    assert json.loads(body)['engine']['remote_address_b64'] == expected


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


def test_a_new_container_that_repeats_a_communication_id_hands_over_two_faxes(engine, tmp_path):
    """A new engine container starts its communication IDs again (its spool is not kept). The arrival time
    keeps the second fax's identity and engine reference apart from the first, so it is never taken for a
    replay of the first."""
    spool, state, data, environment = engine
    (tmp_path / 'answer').write_text('200')
    bodies = []
    for arrival in ('1791180000', '1791183600'):
        _stub(tmp_path / 'tools', 'date', f'echo {arrival}\n')
        (spool / 'recvq' / 'fax000000007.tif').write_bytes(b'II*\x00jbig image')
        assert run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '000000007', '',
                   '+15555550199', cwd=spool).returncode == 0
        assert run('handover', environment).returncode == 0
        bodies.append(json.loads((tmp_path / 'body').read_text()))
    assert [body['uniqueid'] for body in bodies] == ['hylafax.0123456789abcdef.000000007-1791180000',
                                                     'hylafax.0123456789abcdef.000000007-1791183600']
    assert [body['engine']['engine_ref'] for body in bodies] == ['0123456789abcdef:000000007-1791180000',
                                                                 '0123456789abcdef:000000007-1791183600']
    assert bodies[0]['tiff_path'] != bodies[1]['tiff_path']


def test_a_new_container_that_repeats_a_communication_id_reports_each_call_that_left_no_fax(engine, tmp_path):
    spool, state, data, environment = engine
    (spool / 'log' / 'c000000007').unlink()  # the fixture's sent-fax log
    for arrival in ('1791180000', '1791183600'):
        _stub(tmp_path / 'tools', 'date', f'echo {arrival}\n')
        # A new container: a fresh spool with no list of reported sessions, and the same communication ID.
        (spool / 'etc' / 'faxbot-sessions-seen').unlink(missing_ok=True)
        (spool / 'etc' / 'faxbot-sessions-done').unlink(missing_ok=True)
        (spool / 'log' / 'c000000003').write_text(FAILED_RECEIVE.replace("'17911994223.17208565062'", "''"))
        assert run('sessions', environment).returncode == 0
    reports = [json.loads(path.read_text()) for path in sorted((state / 'results').glob('*.report'))]
    assert [report['key'] for report in reports] == ['000000003-1791180000', '000000003-1791183600']
    assert all(report['token'] == '' for report in reports)


def test_a_fax_received_without_a_communication_id_is_still_kept_and_handed_over(engine, tmp_path):
    """When Asterisk rang every free line for one call (it now tries them in turn), two lines could begin a
    session at the same moment and HylaFAX left the answering line's communication ID empty (loopback,
    6 October 2026), and the fax was refused here. It is kept under its receive-queue number instead, and
    handed over like any other."""
    spool, state, data, environment = engine
    # An ordinary fax (not SSL Fax by its image): whether SSL Fax ran on the call is unknown, never "no".
    _stub(tmp_path / 'tools', 'faxinfo', "printf '%s\\n' 'x:' '    Sender: +1 555 555 0199' '     Pages: 2' "
                                         "'SignalRate: 14400 bit/s' 'DataFormat: 2-D MMR' 'TimeToRecv: 0:00:12'\n")
    received = run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX2', '', '', '+15555550199',
                   '179125888.15555550100', '', cwd=spool)
    assert received.returncode == 0, received.stderr
    ticket = (state / 'received' / '0-000000007-1791180000.ticket').read_text()
    assert 'commid=0\n' in ticket and 'token=179125888' in ticket
    (tmp_path / 'answer').write_text('200')
    assert run('handover', environment).returncode == 0
    body = json.loads((tmp_path / 'body').read_text())
    assert body['uniqueid'] == 'engine.179125888' and body['faxpages'] == 2
    assert body['engine']['engine_ref'] == '0123456789abcdef:0-000000007-1791180000'
    assert body['engine']['sslfax'] is None
    assert not list((state / 'received').iterdir())


# The engine's start and supervision (hylafax/entrypoint.sh) against stand-in daemons -------------------------

def _bash():
    """A bash of version 4 or later (the entrypoint's associative arrays), or None."""
    for candidate in (shutil.which('bash'), '/opt/homebrew/bin/bash', '/usr/local/bin/bash', '/bin/bash'):
        if candidate and Path(candidate).exists():
            version = subprocess.run([candidate, '-c', 'echo ${BASH_VERSINFO[0]}'], capture_output=True, text=True)
            if version.returncode == 0 and version.stdout.strip().isdigit() and int(version.stdout) >= 4:
                return candidate
    return None


# Each stand-in records how it was called and runs (or forks) the way the installed program does; a stand-in
# process lists itself in <root>/procs/<pid> with the command line the real one would show, so the stand-in
# pgrep finds exactly what the real pgrep would.
STAND_INS = {
    'common': r"""root=$FAXBOT_ENGINE_ROOT
printf '%s\n' "${0##*/} $*" >> "$root/calls"
become() {  # run until stopped, listed under the given command line
  printf '%s\n' "$1" > "$root/procs/$BASHPID"
  trap 'rm -f "$root/procs/$BASHPID"; exit 0' TERM INT
  while :; do sleep 0.2; done
}
""",
    # IAXmodem 1.2.0 (iaxmodem.c main): a single argument other than -F is one config, run in the foreground;
    # anything else makes a controller (daemonized unless the only argument is -F) that starts one modem for
    # every file in /etc/iaxmodem. A modem binds its port, else 4569, else a random one (libiax2).
    'iaxmodem': r"""cmd="iaxmodem $*"
modem() {
  local conf=$root/etc/iaxmodem/$1 port device candidate
  port=$(sed -n 's/^port[[:space:]]*//p' "$conf")
  device=$(sed -n 's/^device[[:space:]]*//p' "$conf")
  for candidate in "$port" 4569 $((40000 + BASHPID % 20000)); do
    if mkdir "$root/ports/$candidate" 2>/dev/null; then port=$candidate; break; fi
  done
  printf '%5d: 00000000:%04X 00000000:0000 07 00000000:00000000 00:00000000 00000000    10 0 %d 2\n' \
    "$BASHPID" "$port" "$BASHPID" >> "$root/proc/net/udp"
  : > "$root/dev/pts/$BASHPID"
  ln -sfn "$root/dev/pts/$BASHPID" "$root$device"
  echo "[stand-in] Modem started" >&2
  become "$cmd"
}
if [ $# -eq 1 ] && [ "$1" != "-F" ]; then modem "$1"; fi
controller() {
  local file
  for file in "$root"/etc/iaxmodem/*; do
    [ -f "$file" ] && ( modem "${file##*/}" ) &
  done
  become "$cmd"
}
if [ $# -eq 1 ]; then controller; fi
( controller ) &
exit 0
""",
    # faxgetty -D, faxq and hfaxd detach and keep running; faxgetty needs its modem's device, and (as
    # HylaFAX's UUCPLock does with kill(pid, 0)) waits for good on a modem lock whose process ID is alive.
    'faxgetty': r"""[ -e "$root/dev/$2" ] || exit 1
lock=$root/run/lock/LCK..$2
state='Running and idle'
if [ -f "$lock" ] && kill -0 "$(tr -cd '0-9' < "$lock")" 2>/dev/null; then state='Waiting for modem to come free'; fi
[ -f "$root/force.$2" ] && state=$(cat "$root/force.$2")
( printf '%s\n' "$state" > "$root/status.$2"; become "faxgetty $*" ) &
exit 0
""",
    'faxq': r"""( become "faxq" ) &
exit 0
""",
    'hfaxd': r"""( become "hfaxd $*" ) &
exit 0
""",
    # faxstat -s, with the CR LF line ends the real one prints.
    'faxstat': r"""for file in "$root"/status.*; do
  [ -f "$file" ] && printf 'Modem %s (15555550100): %s\r\n' "${file##*.}" "$(cat "$file")"
done
exit 0
""",
    # procps pgrep over the stand-ins' own list: -c count, -x whole match, -f whole command line, -a list.
    'pgrep': r"""count=0 exact=0 full=0 list=0
while [ $# -gt 1 ]; do
  case $1 in -c) count=1 ;; -x) exact=1 ;; -f) full=1 ;; -a) list=1 ;; esac
  shift
done
n=0
for entry in "$root"/procs/*; do
  [ -f "$entry" ] || continue
  pid=${entry##*/}
  kill -0 "$pid" 2>/dev/null || continue
  cmd=$(cat "$entry")
  subject=$cmd
  [ "$full" = 1 ] || subject=${cmd%% *}
  if [ "$exact" = 1 ]; then [ "$subject" = "$1" ] || continue; else case $subject in *"$1"*) ;; *) continue ;; esac; fi
  n=$((n + 1))
  [ "$count" = 1 ] || { if [ "$list" = 1 ]; then echo "$pid $cmd"; else echo "$pid"; fi; }
done
[ "$count" = 1 ] && echo "$n"
[ "$n" -gt 0 ]
""",
    # Privileged or networked steps the engine container does as root.
    'chown': 'exit 0\n',
    'install': 'eval "last=\\${$#}"; eval "first=\\${$(($# - 1))}"; cp "$first" "$last"\n',
    'runuser': 'while [ "$1" != -- ]; do shift; done; shift; exec "$@"\n',
    'getent': "echo '198.51.100.11   STREAM asterisk'\n",
    'openssl': r"""case $1 in
  passwd) echo '$6$standin$hash' ;;
  req) while [ $# -gt 0 ]; do
         case $1 in -keyout) printf -- '-----BEGIN PRIVATE KEY-----\n' > "$2" ;;
                    -out) printf -- '-----BEGIN CERTIFICATE-----\n' > "$2" ;; esac
         shift
       done ;;
esac
""",
}


@pytest.fixture
def entrypoint(tmp_path):
    """The real hylafax/entrypoint.sh with a folder standing in for / and stand-in daemons."""
    bash = _bash()
    if bash is None:
        pytest.skip('bash 4 or later is needed to run the engine entrypoint.')
    root, tools, data = tmp_path / 'root', tmp_path / 'tools', tmp_path / 'faxdata'
    spool, state = root / 'var' / 'spool' / 'hylafax', root / 'var' / 'lib' / 'faxbot-engine'
    for folder in (root / 'procs', root / 'ports', root / 'proc' / 'net', root / 'dev' / 'pts', tools,
                   spool / 'etc', spool / 'sendq', spool / 'log', state, data / 'hylafax', data / 'hylafax-out'):
        folder.mkdir(parents=True)
    (root / 'proc' / 'net' / 'udp').write_text('   sl  local_address rem_address   st\n')
    for name, body in STAND_INS.items():
        if name != 'common':
            (tools / name).write_text(f'#!{bash}\n' + STAND_INS['common'] + body)
            (tools / name).chmod(0o755)
    secrets = {f'line{number}_secret': f'Line{number}' + 'a' * 30 for number in (1, 2)}
    (data / 'hylafax' / 'engine.conf').write_text(''.join(f'{key}={value}\n' for key, value in {
        'lines': '2', 'asterisk_host': 'asterisk', 'asterisk_port': '4569', 'submit_user': 'faxbot',
        'submit_password': 'Submit' + 'b' * 30, 'station_id': '+15555550100', 'fax_number': '15555550100',
        'codec': 'ulaw', 'sslfax': 'yes', 'sslfax_listener': '', 'api_url': 'http://api:8080',
        'report_secret': 'synthetic-report-secret-0123456789', **secrets}.items()))
    environment = {'PATH': f'{tools}:/usr/bin:/bin', 'FAXBOT_ENGINE_ROOT': str(root), 'FAXBOT_DATA': str(data),
                   'FAXBOT_HYLAFAX_SPOOL': str(spool), 'FAXBOT_ENGINE_STATE': str(state),
                   'FAXBOT_ENGINE_CHECK_SECONDS': '1', 'FAXBOT_ENGINE_UNREADY_SECONDS': '1',
                   'FAXBOT_ENGINE_READY_SECONDS': '3', 'HOME': str(tmp_path)}
    started = []

    def start():
        # The engine's own lines go to a file: the daemons it starts keep their output open after it exits.
        with open(tmp_path / 'engine.log', 'w') as log:
            process = subprocess.Popen([bash, str(ROOT / 'hylafax' / 'entrypoint.sh')], env=environment,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        started.append(process)
        return process
    yield start, root, data
    for process in started:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def _status(data):
    try:
        return json.loads((data / 'hylafax-out' / 'engine.status').read_text())
    except (OSError, ValueError):
        return {}


def _wait(probe, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if probe():
            return True
        time.sleep(0.2)
    return False


def _running(root):
    """The stand-in processes alive now, by the command line each would show."""
    found = []
    for entry in (root / 'procs').iterdir():
        try:
            os.kill(int(entry.name), 0)
        except (ProcessLookupError, ValueError):
            continue
        found.append(entry.read_text().strip())
    return sorted(found)


def test_the_engine_starts_each_line_as_exactly_one_modem_and_says_running_only_then(entrypoint):
    """Live, 6 October 2026: `iaxmodem -F <file>` made IAXmodem 1.2.0 start a modem for every file in
    /etc/iaxmodem, so two lines ran as four modems and calls rang where no faxgetty answered. The real
    entrypoint, against stand-ins that run and fork the way the installed programs do: one modem per line,
    started with its config name as the only argument, each on its own port, then running."""
    start, root, data = entrypoint
    process = start()
    assert _wait(lambda: _status(data).get('state') == 'running' or process.poll() is not None), _status(data)
    assert _status(data).get('state') == 'running', (root.parent / 'engine.log').read_text()
    calls = (root / 'calls').read_text().splitlines()
    assert sorted(call for call in calls if call.startswith('iaxmodem')) == ['iaxmodem ttyIAX1', 'iaxmodem ttyIAX2']
    assert _running(root) == ['faxgetty -D ttyIAX1', 'faxgetty -D ttyIAX2', 'faxq', 'hfaxd -i 4559',
                              'iaxmodem ttyIAX1', 'iaxmodem ttyIAX2']
    ports = sorted(int(line.split()[1].split(':')[1], 16)
                   for line in (root / 'proc' / 'net' / 'udp').read_text().splitlines()[1:])
    assert ports == [4570, 4571]


def test_a_line_that_loses_its_modem_is_reported_and_the_engine_starts_again(entrypoint):
    """A line whose modem is gone takes no calls: the status says so at once, and the engine exits to be
    started again (Docker's restart policy) as soon as no call is up."""
    start, root, data = entrypoint
    process = start()
    assert _wait(lambda: _status(data).get('state') == 'running')
    (modem,) = [entry for entry in (root / 'procs').iterdir() if entry.read_text().strip() == 'iaxmodem ttyIAX2']
    os.kill(int(modem.name), signal.SIGTERM)
    assert _wait(lambda: process.poll() is not None)
    assert process.returncode == 1
    status = _status(data)
    assert status['state'] == 'restarting', status
    assert status['reason'] == "Faxbot's fast fax service lost a fax line and is starting again."


def test_a_modem_lock_left_by_a_restart_never_keeps_a_line_out_of_service(entrypoint):
    """Live, 6 October 2026: the engine restarted itself during a call; faxgetty's lock on ttyIAX1 stayed in
    the container and named a process ID that the restart gave to a live process, so line 1 waited on it for
    good while the status said running. Every start clears the modem locks before the lines start."""
    start, root, data = entrypoint
    (root / 'run' / 'lock').mkdir(parents=True)
    (root / 'run' / 'lock' / 'LCK..ttyIAX1').write_text(f'{os.getpid():10d}\n')
    start()
    assert _wait(lambda: _status(data).get('state') == 'running'), _status(data)
    assert not (root / 'run' / 'lock' / 'LCK..ttyIAX1').exists()
    assert (root / 'status.ttyIAX1').read_text().strip() == 'Running and idle'


def test_a_line_that_never_gets_ready_is_never_reported_running(entrypoint):
    """A line whose faxgetty is not ready takes no calls: the engine does not say it is running, and starts
    again once the wait for its lines is over and no call is up."""
    start, root, data = entrypoint
    (root / 'force.ttyIAX1').write_text('Waiting for modem to come free\n')
    process = start()
    assert _wait(lambda: process.poll() is not None)
    assert process.returncode == 1 and _status(data)['state'] == 'failed', _status(data)
    assert (root / 'status.ttyIAX1').read_text().strip() == 'Waiting for modem to come free'


def test_the_engine_starts_again_when_faxbot_asks_and_not_for_a_request_it_already_followed(entrypoint):
    """Faxbot writes a restart request (a fax call no free line answered, or Restart the fast fax service): the
    engine starts again once no call is up. A request older than this start was already followed."""
    start, root, data = entrypoint
    (data / 'hylafax' / 'engine-restart').write_text('{"reason": "manual", "at": 1, "asked": 1}\n')
    process = start()
    assert _wait(lambda: _status(data).get('state') == 'running')
    time.sleep(2.5)  # two supervision rounds: the old request does not restart it
    assert process.poll() is None
    (data / 'hylafax' / 'engine-restart').write_text('{"reason": "missed_call", "at": 2, "asked": 2}\n')
    assert _wait(lambda: process.poll() is not None)
    assert process.returncode == 0
    assert _status(data)['state'] == 'restarting'
    assert _status(data)['reason'] == "Faxbot's fast fax service is starting again."


def test_a_line_that_stops_being_ready_is_reported_and_running_again_once_it_recovers(entrypoint):
    """After the start, a line that is not ready for a call (waiting on a modem lock) makes the status say so;
    while the other line is in a call the engine does not restart, and once the line is ready again the
    status says running. With no call up, the engine starts again."""
    start, root, data = entrypoint
    process = start()
    assert _wait(lambda: _status(data).get('state') == 'running')
    (root / 'status.ttyIAX2').write_text('Receiving facsimile\n')
    (root / 'status.ttyIAX1').write_text('Waiting for modem to come free\n')
    assert _wait(lambda: _status(data).get('state') == 'restarting'), _status(data)
    assert _status(data)['reason'] == "Faxbot's fast fax service lost a fax line and is starting again."
    assert process.poll() is None
    (root / 'status.ttyIAX1').write_text('Running and idle\n')
    assert _wait(lambda: _status(data).get('state') == 'running'), _status(data)
    (root / 'status.ttyIAX2').write_text('Running and idle\n')
    (root / 'status.ttyIAX1').write_text('Waiting for modem to come free\n')
    assert _wait(lambda: process.poll() is not None)
    assert process.returncode == 1


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


FAILED_RECEIVE = """Oct 05 11:23:42.60: [  108]: SESSION BEGIN 000000003 17208565062 (logging via thread)
Oct 05 11:23:42.60: [  108]: CallID: '3034265097' '17911994223.17208565062' ''
Oct 05 11:23:46.34: [  108]: ANSWER: FAX CONNECTION  DEVICE '/dev/ttyIAX2'
Oct 05 11:23:46.34: [  108]: RECV FAX: begin
Oct 05 11:23:53.21: [  108]: MODEM TIMEOUT: waiting for v.21 carrier
Oct 05 11:24:26.13: [  108]: RECV FAX: No sender protocol (T.30 T1 timeout) {E102}
Oct 05 11:24:26.13: [  108]: RECV FAX: end
Oct 05 11:24:26.15: [  108]: SESSION END
"""
RECEIVED = """Oct 05 11:25:02.30: [  108]: CallID: '3034265097' '17911995026.17208565062' ''
Oct 05 11:25:06.04: [  108]: RECV FAX: begin
Oct 05 11:26:00.93: [  108]: RECV FAX: /usr/local/lib/faxbot-engine/received 'recvq/fax000000002.tif' 'ttyIAX2'
Oct 05 11:26:00.93: [  108]: RECV FAX (000000004): recvq/fax000000002.tif from 3034265097, subaddress <unspecified>, 2 pages in 0:00:53
Oct 05 11:26:00.93: [  108]: RECV FAX: end
Oct 05 11:26:00.94: [  108]: SESSION END
"""


def test_a_received_call_that_left_no_fax_is_reported_once_with_the_engines_reason(engine, tmp_path):
    spool, state, data, environment = engine
    (spool / 'log' / 'c000000003').write_text(FAILED_RECEIVE)
    (spool / 'log' / 'c000000004').write_text(RECEIVED)
    (spool / 'log' / 'c000000005').write_text(FAILED_RECEIVE.replace('SESSION END\n', ''))  # still in progress
    (spool / 'log' / 'c000000007').unlink()  # the fixture's sent-fax log
    assert run('sessions', environment).returncode == 0
    reports = sorted((state / 'results').glob('*.report'))
    assert [path.name for path in reports] == ['1791180000-recv000000003-failed.report']
    report = json.loads(reports[0].read_text())
    assert report == {'engine_id': '0123456789abcdef', 'commid': '000000003', 'key': '000000003-1791180000',
                      'token': '17911994223', 'caller': '3034265097', 'called': '17208565062', 'trunk': '',
                      'reason_b64': base64.b64encode(b'No sender protocol (T.30 T1 timeout) {E102}').decode()}
    # Reported once; the session still in progress is reported when it ends.
    assert run('sessions', environment).returncode == 0
    assert len(list((state / 'results').glob('*.report'))) == 1
    (spool / 'log' / 'c000000005').write_text(FAILED_RECEIVE)
    assert run('sessions', environment).returncode == 0
    assert len(list((state / 'results').glob('*.report'))) == 2
    # bin/deliver sends it to the engine's route for received calls that left no fax.
    (tmp_path / 'answer').write_text('200')
    assert run('deliver', environment).returncode == 0
    assert set((tmp_path / 'urls').read_text().splitlines()) == {'http://api:8080/_internal/hylafax/received-failed'}




def test_a_received_call_the_engines_restart_cut_off_is_reported_never_skipped(engine, tmp_path):
    """A session the engine's last start cut off never gets SESSION END (live, 6 October 2026: c000000004);
    it is reported as a received call that left no fax, and a call still in progress is left alone."""
    spool, state, data, environment = engine
    (spool / 'log' / 'c000000007').unlink()  # the fixture's sent-fax log
    cut_off = "Oct 06 02:44:56.78: [  106]: SESSION BEGIN 000000004 17208565062 (logging via thread)\n" \
              "Oct 06 02:44:56.78: [  106]: CallID: '3034265097' '179125469614.17208565062' ''\n"
    (spool / 'log' / 'c000000004').write_text(cut_off)
    (spool / 'log' / 'c000000005').write_text(cut_off.replace('000000004', '000000005'))
    marker = spool / 'etc' / 'faxbot-engine-started'
    marker.write_text('')
    os.utime(spool / 'log' / 'c000000004', (1791180000 - 60, 1791180000 - 60))
    os.utime(marker, (1791180000, 1791180000))
    os.utime(spool / 'log' / 'c000000005', (1791180000 + 60, 1791180000 + 60))  # after the start: in progress
    assert run('sessions', environment).returncode == 0
    (report,) = [json.loads(path.read_text()) for path in (state / 'results').glob('*.report')]
    assert report['commid'] == '000000004' and report['token'] == '179125469614'
    assert base64.b64decode(report['reason_b64']) == b'Call cut off: the fast fax service restarted'


def test_sessions_read_only_the_sessions_above_the_last_finished_one(engine, tmp_path):
    """The logs are never pruned; every round once read every one. Communication IDs rise one by one, so a
    mark below which every session is finished keeps each round to the new and unfinished ones."""
    spool, state, data, environment = engine
    (spool / 'log' / 'c000000007').unlink()
    (spool / 'log' / 'c000000003').write_text(FAILED_RECEIVE)
    (spool / 'log' / 'c000000004').write_text(RECEIVED)
    (spool / 'log' / 'c000000005').write_text(FAILED_RECEIVE.replace('SESSION END\n', ''))  # still in progress
    (spool / 'log' / 'c000000006').write_text(FAILED_RECEIVE)
    # Before the first mark the script runs quietly (it runs every few seconds; loopback, 6 October 2026).
    (spool / 'etc' / 'faxbot-sessions-done').unlink(missing_ok=True)
    first = run('sessions', environment)
    assert first.returncode == 0 and first.stderr == '', first.stderr
    assert (spool / 'etc' / 'faxbot-sessions-done').read_text().strip() == '000000004'
    assert len(list((state / 'results').glob('*.report'))) == 2  # 3 and 6
    # Sessions at or below the mark are not read again, whatever their log says now.
    (spool / 'log' / 'c000000003').write_text('unreadable now')
    (spool / 'log' / 'c000000003').chmod(0)
    (spool / 'log' / 'c000000005').write_text(FAILED_RECEIVE)
    assert run('sessions', environment).returncode == 0
    assert (spool / 'etc' / 'faxbot-sessions-done').read_text().strip() == '000000006'
    assert len(list((state / 'results').glob('*.report'))) == 3


def test_a_job_reports_speed_and_compression_only_when_the_session_agreed_them(engine, tmp_path):
    spool, state, data, environment = engine
    requested = QFILE + 'signalrate:14400 bit/s\ndataformat:JBIG\n'
    (spool / 'doneq' / 'q12').write_text(requested.replace('signalrate:\ndataformat:\n', ''))
    (spool / 'log' / 'c000000007').write_text('Oct 05 11:18:30.08: [  858]: DIAL 3235486610915671\n'
                                             'Oct 05 11:19:12.78: [  858]: SEND FAILED: No carrier detected {E002}\n')
    (tmp_path / 'answer').write_text('200')
    assert run('notify', environment, 'doneq/q12', 'failed', '0:00:41', cwd=spool).returncode == 0
    body = json.loads((tmp_path / 'body').read_text())
    assert body['signal_rate_b64'] == '' and body['data_format_b64'] == ''
    # A session that trained reports what it agreed.
    (spool / 'log' / 'c000000007').write_text('Oct 05 11:22:33.61: [  598]: TRAINING succeeded\n')
    assert run('notify', environment, 'doneq/q12', 'failed', '0:00:41', cwd=spool).returncode == 0
    body = json.loads((tmp_path / 'body').read_text())
    assert base64.b64decode(body['signal_rate_b64']) == b'14400 bit/s'

