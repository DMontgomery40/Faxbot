"""The engine containers' processes, run the way docker-compose.yml runs them.

Runs only with FAXBOT_NATIVE_PROOF=1 (it starts containers with Docker). FAXBOT_NATIVE_IMAGE and
FAXBOT_ENGINE_IMAGE name already-built Asterisk and engine images; without them the images are built.

Asterisk parses the carrier's SIP, audio and T.38 packets, so it runs as its own unprivileged user
(uid and gid 5060, asterisk.conf runuser and rungroup), and the fax engine's daemons run as uucp. Each
container keeps only the capabilities its root start script needs. Every test here starts the built
image with the service's own cap_drop, cap_add and security_opt read from docker-compose.yml, so the
file and the images cannot drift apart, and checks what runs, as whom, with which privileges; one test
takes each capability away in turn and shows what breaks without it.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import time
import uuid

import pytest
import yaml

pytestmark = [
    pytest.mark.native,
    pytest.mark.skipif(os.environ.get('FAXBOT_NATIVE_PROOF') != '1',
                       reason='Set FAXBOT_NATIVE_PROOF=1 to start the engine images with Docker.'),
]

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = os.environ.get('FAXBOT_DOCKER_CONTEXT', 'colima-faxbot-refresh')
PREFIX = os.environ.get('FAXBOT_PROOF_PREFIX', 'faxbot-native-proof')
ASTERISK_ID = 5060
ASTERISK_COMMAND = 'asterisk -f -C /etc/asterisk/asterisk.conf'
# A synthetic engine.conf: one line, a loopback Asterisk host (none answers; the daemons start anyway).
ENGINE_CONF = '\n'.join([
    'lines=1', 'asterisk_host=localhost', 'asterisk_port=4569', 'submit_user=faxbot',
    'submit_password=Synthetic0123456789abcdefXYZ', 'station_id=+15555550100', 'fax_number=15555550100',
    'codec=ulaw', 'sslfax=yes', 'sslfax_listener=', 'api_url=http://127.0.0.1:8080',
    'report_secret=synthetic-report-secret-0123', 'line1_secret=Synthetic0123456789abcdefLINE', ''])


def docker(*args, check=True, timeout=120):
    result = subprocess.run(['docker', '--context', CONTEXT, *args], capture_output=True, text=True,
                            timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'docker {args[0]} failed: {result.stderr[-1500:]}')
    return result


def compose_security(service, *, without=()):
    """docker run flags for the service's cap_drop, cap_add and security_opt in docker-compose.yml."""
    spec = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services'][service]
    flags = []
    for cap in spec.get('cap_drop') or []:
        flags += ['--cap-drop', cap]
    for cap in spec.get('cap_add') or []:
        if cap not in without:
            flags += ['--cap-add', cap]
    for option in spec.get('security_opt') or []:
        flags += ['--security-opt', option]
    return flags


def built(variable, tag, folder):
    name = os.environ.get(variable)
    if name:
        return name
    name = f'{PREFIX}-{tag}:processes'
    docker('build', '--quiet', '--tag', name, str(ROOT / folder), timeout=3600)
    return name


@pytest.fixture(scope='module')
def asterisk_image():
    return built('FAXBOT_NATIVE_IMAGE', 'asterisk', 'asterisk')


@pytest.fixture(scope='module')
def engine_image():
    return built('FAXBOT_ENGINE_IMAGE', 'engine', 'hylafax')


@pytest.fixture
def start():
    """start(image, service, *, without=(), env=None, run=True) -> container name; removed afterwards."""
    made = []

    def run(image, service, *, without=(), env=None, started=True):
        name = f'{PREFIX}-processes-{uuid.uuid4().hex[:8]}'
        args = ['create', '--name', name, '--label', 'com.faxbot.scope=engine-processes',
                *compose_security(service, without=without)]
        for key, value in (env or {}).items():
            args += ['--env', f'{key}={value}']
        docker(*args, image)
        made.append(name)
        if started:
            docker('start', name)
        return name
    yield run
    for name in made:
        docker('rm', '--force', '--volumes', name, check=False)


def wait_for(probe, seconds, what):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f'Timed out waiting for {what}')


PROCESSES = ('for p in /proc/[0-9]*; do [ -r "$p/cmdline" ] || continue; '
             'printf "%s\\037" "${p#/proc/}" "$(awk \'/^Uid:/ {print $3}\' "$p/status")"; '
             'tr "\\000" " " < "$p/cmdline"; echo; done')


def processes(name):
    """(pid, effective uid, command line) for every process in the container but this probe's own."""
    out = docker('exec', name, 'sh', '-c', PROCESSES, check=False).stdout
    found = []
    for line in out.splitlines():
        parts = line.split('\x1f')
        if len(parts) == 3 and parts[2].strip() and 'for p in /proc' not in parts[2]:
            found.append((int(parts[0]), int(parts[1]), parts[2].strip()))
    return found


def status(name, pid):
    """The process's /proc status fields; empty when it has ended since it was listed (a check's sleep)."""
    text = docker('exec', name, 'cat', f'/proc/{pid}/status', check=False).stdout
    return {key: value.strip() for key, value in (line.split(':', 1) for line in text.splitlines() if ':' in line)}


def running(name):
    return docker('inspect', '-f', '{{.State.Running}}', name, check=False).stdout.strip() == 'true'


def asterisk_ready(name):
    return 'System uptime' in docker('exec', name, 'asterisk', '-rx', 'core show uptime seconds', check=False).stdout


def stat(name, *paths):
    out = docker('exec', name, 'stat', '-c', '%a %U:%G %n', *paths).stdout
    return {line.split()[2]: tuple(line.split()[:2]) for line in out.splitlines()}


# Asterisk ----------------------------------------------------------------------------------------------------

def test_asterisk_runs_once_as_its_own_user_with_every_fax_module(asterisk_image, start):
    name = start(asterisk_image, 'asterisk')
    wait_for(lambda: asterisk_ready(name), 60, 'Asterisk (its remote console, run as root)')
    found = processes(name)
    asterisk = [(pid, uid, command) for pid, uid, command in found if command.split()[0] == 'asterisk']
    # Started exactly once, with the command line its start script gives it, as uid 5060.
    assert [(uid, command) for _, uid, command in asterisk] == [(ASTERISK_ID, ASTERISK_COMMAND)], found
    details = status(name, asterisk[0][0])
    assert details['Uid'].split() == [str(ASTERISK_ID)] * 4 and details['Gid'].split() == [str(ASTERISK_ID)] * 4
    assert details['Groups'] == '' and details['NoNewPrivs'] == '1', details
    assert int(details['CapPrm'], 16) == 0 and int(details['CapEff'], 16) == 0, details
    # What stays root: the init process, the start script's login watcher (and its sleep). Nothing else.
    root = sorted({command.split()[0].rsplit('/', 1)[-1] for _, uid, command in found if uid == 0})
    assert set(root) <= {'dumb-init', 'bash', 'sleep'}, found
    # The fax modules, the trunk's channel driver and the engine's lines are loaded and running.
    modules = docker('exec', name, 'asterisk', '-rx', 'module show like res_fax').stdout + \
        docker('exec', name, 'asterisk', '-rx', 'module show like chan_').stdout
    for module in ('res_fax_spandsp.so', 'res_fax.so', 'chan_pjsip.so', 'chan_iax2.so'):
        assert re.search(rf'^{re.escape(module)}\s.*\sRunning\s', modules, re.MULTILINE), (module, modules)
    # What Asterisk writes is its own; its configuration is root's, readable by its group only; the data
    # folder gives new files its group (setgid), and only the shared folder's group may enter it.
    modes = stat(name, '/faxdata', '/faxdata/asterisk', '/faxdata/inbound', '/var/run/asterisk/asterisk.ctl',
                 '/var/lib/asterisk', '/var/log/asterisk', '/var/spool/asterisk', '/etc/asterisk',
                 '/etc/asterisk/pjsip.conf', '/etc/asterisk/manager.conf', '/etc/asterisk/extensions.conf')
    assert modes['/faxdata'] == ('2755', 'root:asterisk') and modes['/faxdata/asterisk'] == ('2750', 'root:asterisk')
    assert modes['/faxdata/inbound'][1] == 'asterisk:asterisk', modes
    assert modes['/var/run/asterisk/asterisk.ctl'] == ('660', 'asterisk:asterisk'), modes
    for folder in ('/var/lib/asterisk', '/var/log/asterisk', '/var/spool/asterisk'):
        assert modes[folder][1] == 'asterisk:asterisk', modes
    for path in ('/etc/asterisk', '/etc/asterisk/pjsip.conf', '/etc/asterisk/manager.conf',
                 '/etc/asterisk/extensions.conf'):
        assert modes[path][1] == 'root:asterisk' and modes[path][0][-1] == '0', modes
    # A stop reaches Asterisk (dumb-init passes it on), which ends cleanly at once.
    began = time.monotonic()
    docker('stop', '--time', '10', name)
    assert time.monotonic() - began < 8
    assert docker('inspect', '-f', '{{.State.ExitCode}}', name).stdout.strip() == '0'
    assert 'Asterisk cleanly ending' in docker('logs', name).stdout


def test_asterisk_needs_each_capability_compose_gives_it(asterisk_image, start):
    """Taken away one at a time, each capability in docker-compose.yml breaks something, and that failure is
    loud: the start script stops, Asterisk refuses to start, the data folder loses its setgid bit, or a stop
    no longer reaches Asterisk and Docker has to kill it."""
    spec = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services']['asterisk']
    assert spec['cap_drop'] == ['ALL'] and spec['security_opt'] == ['no-new-privileges:true']
    assert sorted(spec['cap_add']) == ['CHOWN', 'FSETID', 'KILL', 'SETGID', 'SETUID']
    failures = {}
    for cap in spec['cap_add']:
        name = start(asterisk_image, 'asterisk', without=(cap,))
        if cap == 'FSETID':
            wait_for(lambda: asterisk_ready(name), 60, 'Asterisk without FSETID')
            failures[cap] = stat(name, '/faxdata')['/faxdata'][0]
        elif cap == 'KILL':
            wait_for(lambda: asterisk_ready(name), 60, 'Asterisk without KILL')
            began = time.monotonic()
            docker('stop', '--time', '3', name)
            failures[cap] = (round(time.monotonic() - began), docker('inspect', '-f', '{{.State.ExitCode}}',
                                                                     name).stdout.strip())
        else:
            wait_for(lambda: not running(name), 30, f'Asterisk to stop without {cap}')
            failures[cap] = docker('logs', name).stderr + docker('logs', name).stdout
    assert 'chown' in failures['CHOWN'] and 'Operation not permitted' in failures['CHOWN'], failures
    assert 'Unable to setgid to 5060' in failures['SETGID'], failures
    assert 'Unable to setuid to 5060' in failures['SETUID'], failures
    # The data folder keeps its group but silently loses the bit that passes the group on.
    assert failures['FSETID'] == '755', failures
    # Docker waited out its stop time and killed Asterisk.
    assert failures['KILL'][0] >= 3 and failures['KILL'][1] == '137', failures


def test_the_login_watcher_still_restarts_asterisk_when_faxbot_writes_a_login(asterisk_image, start):
    """First boot in Compose: Asterisk starts without a manager login and the start script's watcher (root)
    restarts it once Faxbot writes one. With Asterisk as its own user, the watcher still sees it running and
    still reaches its control socket."""
    name = start(asterisk_image, 'asterisk', env={'FAXBOT_LOGIN_CHECK_SECONDS': '1'})
    wait_for(lambda: asterisk_ready(name), 60, 'Asterisk')
    time.sleep(3)
    assert running(name)
    docker('exec', name, 'sh', '-c',
           "printf 'api\\nSynthetic-Manager-Login-0123456789\\n' > /faxdata/asterisk/manager.credentials")
    wait_for(lambda: not running(name), 30, 'Asterisk to stop for the new login')
    logs = docker('logs', name)
    assert docker('inspect', '-f', '{{.State.ExitCode}}', name).stdout.strip() == '0'
    assert 'the manager login changed; Asterisk restarts once no call is up' in logs.stdout + logs.stderr


# The fax engine ----------------------------------------------------------------------------------------------

def engine_with_settings(start, image, *, without=(), left_job=False):
    """The engine container, given its settings once it waits for them (optionally with a job a restart
    left in uucp's send queue first)."""
    name = start(image, 'hylafax', without=without)
    wait_for(lambda: 'waiting for Faxbot' in docker('logs', name).stderr, 30, 'the engine to wait for settings')
    if left_job:
        docker('exec', '-u', 'uucp', name, 'sh', '-c', 'umask 077 && echo synthetic > /var/spool/hylafax/sendq/q7')
    docker('exec', name, 'mkdir', '-p', '/faxdata/hylafax')
    subprocess.run(['docker', '--context', CONTEXT, 'exec', '-i', name, 'sh', '-c',
                    'cat > /faxdata/hylafax/engine.conf'], input=ENGINE_CONF, text=True, check=True, timeout=60)
    return name


def engine_state(name):
    text = docker('exec', name, 'cat', '/faxdata/hylafax-out/engine.status', check=False).stdout
    found = re.search(r'"state": "([a-z]+)"', text)
    return found.group(1) if found else ('exited' if not running(name) else '')


def test_the_engine_daemons_run_once_as_uucp_and_a_left_job_is_moved_aside(engine_image, start):
    name = engine_with_settings(start, engine_image, left_job=True)

    def daemons():
        found = processes(name)
        names = [command for _, _, command in found
                 if command.split()[0].rsplit('/', 1)[-1] in ('iaxmodem', 'faxgetty', 'faxq', 'hfaxd')]
        return found if len(names) >= 4 else None
    found = wait_for(daemons, 60, "the engine's daemons")
    time.sleep(2)
    found = processes(name)
    commands = sorted(command for _, uid, command in found
                      if command.split()[0].rsplit('/', 1)[-1] in ('iaxmodem', 'faxgetty', 'faxq', 'hfaxd'))
    # Each daemon exactly once, with the arguments the installed versions read, all as uucp.
    assert commands == ['faxgetty -D ttyIAX1', 'faxq', 'hfaxd -i 4559', 'iaxmodem ttyIAX1'], found
    uucp = int(docker('exec', name, 'id', '-u', 'uucp').stdout.strip())
    # The capabilities docker-compose.yml leaves the container (CHOWN 0, DAC_OVERRIDE 1, FOWNER 3, KILL 5,
    # SETGID 6, SETUID 7, SYS_CHROOT 18): the most any process in it can ever hold.
    allowed = sum(1 << bit for bit in (0, 1, 3, 5, 6, 7, 18))
    for pid, uid, command in found:
        program = command.split()[0].rsplit('/', 1)[-1]
        details = status(name, pid)
        if not details and program not in ('iaxmodem', 'faxgetty', 'faxq', 'hfaxd'):
            continue
        assert details['NoNewPrivs'] == '1' and int(details['CapBnd'], 16) == allowed, (command, details)
        if program in ('iaxmodem', 'faxgetty', 'faxq', 'hfaxd'):
            # They work as uucp with no effective capability. HylaFAX's design keeps root as their real and
            # saved user, to switch back for device and session work, so root here is the bounded set above.
            assert uid == uucp and int(details['CapEff'], 16) == 0, (command, details)
            assert details['Uid'].split()[:3] == ['0', str(uucp), '0'], (command, details)
        else:
            # Root: init, the start script, its syslog relay and sleeps.
            assert uid == 0 and program in ('dumb-init', 'bash', 'busybox', 'sleep'), (command, uid)
    # The root start script still reaches uucp's private folders: the left job was moved aside, not sent.
    assert 'moved 1 unfinished job(s) aside' in docker('logs', name).stderr
    assert docker('exec', name, 'sh', '-c', 'ls /var/spool/hylafax/sendq').stdout.strip() == ''
    assert stat(name, '/var/spool/hylafax/etc/ssl.pem')['/var/spool/hylafax/etc/ssl.pem'] == ('600', 'uucp:uucp')
    # Every line ready (its faxgetty answers the engine's status check through the job server): running.
    wait_for(lambda: engine_state(name) == 'running', 90, 'the engine to say it is running')


def test_the_engine_needs_each_capability_compose_gives_it(engine_image, start):
    """Taken away one at a time, each capability but KILL stops the engine: the start script cannot write the
    uucp spool's settings, the daemons cannot drop to uucp, or the job server cannot shut a session into the
    spool, so no line ever comes ready and Faxbot could not hand it a fax. (KILL only lets a stop reach the
    uucp daemons; without it Docker ends them when the container ends.)"""
    spec = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services']['hylafax']
    assert spec['cap_drop'] == ['ALL'] and spec['security_opt'] == ['no-new-privileges:true']
    assert sorted(spec['cap_add']) == ['CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'KILL', 'SETGID', 'SETUID', 'SYS_CHROOT']
    outcomes = {}
    for cap in [cap for cap in spec['cap_add'] if cap != 'KILL']:
        name = engine_with_settings(start, engine_image, without=(cap,))
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and engine_state(name) not in ('running', 'failed', 'exited'):
            time.sleep(1)
        outcomes[cap] = (engine_state(name), docker('logs', name).stderr[-600:])
    assert all(state != 'running' for state, _ in outcomes.values()), outcomes
    for cap in ('CHOWN', 'DAC_OVERRIDE', 'FOWNER'):
        assert '/var/spool/hylafax/etc/ssl.pem' in outcomes[cap][1], (cap, outcomes[cap])
