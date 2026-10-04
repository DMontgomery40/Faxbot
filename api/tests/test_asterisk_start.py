"""asterisk/start.sh: what the Asterisk container loads at start and what it tells Faxbot.

The script runs here with its directories pointed at a temporary folder and a
stand-in for the asterisk binary, so no container is needed.
"""
from pathlib import Path
import os
import shutil
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'asterisk' / 'start.sh'
TEMPLATES = ROOT / 'asterisk' / 'etc' / 'asterisk' / 'templates'
PUBLIC_ADDRESS = ROOT / 'asterisk' / 'bin' / 'faxbot-public-address'

pytestmark = pytest.mark.skipif(shutil.which('bash') is None or shutil.which('envsubst') is None,
                                reason='bash and envsubst are needed to run the start script.')

TRUNK = '\n'.join(['[global]', 'type=global', '', '[transport-tls]', 'type=transport',
                   'external_media_address=@FAXBOT_PUBLIC_ADDRESS@', 'local_net=@FAXBOT_LOCAL_NET@', ''])


def _environment(tmp_path, **environment):
    etc, data = tmp_path / 'etc', tmp_path / 'data'
    etc.mkdir(exist_ok=True)
    (data / 'asterisk').mkdir(parents=True, exist_ok=True)
    stand_in = tmp_path / 'asterisk-stand-in'
    stand_in.write_text('#!/bin/sh\nexit 0\n')
    stand_in.chmod(0o755)
    # The image installs the helper as an executable; here bash runs the repository copy.
    helper = tmp_path / 'faxbot-public-address'
    helper.write_text(f'#!/bin/sh\nexec bash {PUBLIC_ADDRESS} "$@"\n')
    helper.chmod(0o755)
    # A watcher that would outlive the test checks every minute; tests that follow it set this lower.
    return {'PATH': os.environ['PATH'], 'FAXBOT_ASTERISK_ETC': str(etc), 'FAXBOT_ASTERISK_TEMPLATES': str(TEMPLATES),
            'FAXBOT_DATA': str(data), 'FAXBOT_PUBLIC_ADDRESS_BIN': str(helper),
            'FAXBOT_PUBLIC_ADDRESS_FILE': str(data / 'asterisk' / 'public-address'), 'FAXBOT_LOCAL_NET': '172.18.0.0/16',
            'FAXBOT_ASTERISK_COMMAND': str(stand_in), 'FAXBOT_LOGIN_CHECK_SECONDS': '60', **environment}


def start(tmp_path, **environment):
    """Run start.sh once; returns (completed process, etc folder, shared folder)."""
    result = subprocess.run(['bash', str(SCRIPT)], env=_environment(tmp_path, **environment), capture_output=True,
                            text=True, timeout=30)
    return result, tmp_path / 'etc', tmp_path / 'data' / 'asterisk'


def test_each_start_records_the_trunk_it_loaded_and_that_faxbot_manages_it(tmp_path):
    (tmp_path / 'data' / 'asterisk').mkdir(parents=True)
    (tmp_path / 'data' / 'asterisk' / 'pjsip.conf').write_text(TRUNK)
    result, etc, shared = start(tmp_path)
    assert result.returncode == 0, result.stderr
    # Asterisk loads the trunk with the address lines settled; Faxbot gets the file as it was before that.
    assert '@FAXBOT_' not in (etc / 'pjsip.conf').read_text()
    assert (shared / 'pjsip.conf.started').read_text() == TRUNK
    assert oct((shared / 'pjsip.conf.started').stat().st_mode & 0o777) == '0o600'
    assert (shared / 'engine-started').read_text().strip().isdigit()


def test_a_start_without_a_faxbot_trunk_leaves_no_stale_copy(tmp_path):
    shared = tmp_path / 'data' / 'asterisk'
    shared.mkdir(parents=True)
    (shared / 'pjsip.conf.started').write_text(TRUNK)
    result, etc, shared = start(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (shared / 'pjsip.conf.started').exists()
    assert (shared / 'engine-started').exists()
    assert 'transport-udp' in (etc / 'pjsip.conf').read_text()


# -- the manager login: environment first, then the one Faxbot writes ------------------------------

def _stand_ins(tmp_path, seconds):
    """An asterisk that runs for a while, and a control command that records what it was asked."""
    running = tmp_path / 'asterisk-running'
    running.write_text(f'#!/bin/sh\nsleep {seconds}\n')
    running.chmod(0o755)
    control = tmp_path / 'asterisk-control'
    control.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path / "control.log"}\n')
    control.chmod(0o755)
    return running, control


def _wait_for(path, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.05)
    return None


def test_the_login_faxbot_wrote_turns_the_manager_port_on(tmp_path):
    shared = tmp_path / 'data' / 'asterisk'
    shared.mkdir(parents=True)
    (shared / 'manager.credentials').write_text('api\nGenerated-Login_42\n')
    result, etc, _ = start(tmp_path)
    assert result.returncode == 0, result.stderr
    manager = (etc / 'manager.conf').read_text()
    assert 'enabled=yes' in manager and '[api]' in manager and 'secret=Generated-Login_42' in manager
    assert 'write=originate,reporting,system,command' in manager
    assert oct((etc / 'manager.conf').stat().st_mode & 0o777) == '0o600'


def test_a_login_in_the_environment_wins_and_the_file_is_not_followed(tmp_path):
    shared = tmp_path / 'data' / 'asterisk'
    shared.mkdir(parents=True)
    (shared / 'manager.credentials').write_text('api\nfrom-the-file\n')
    running, control = _stand_ins(tmp_path, 1.5)
    process = subprocess.Popen(['bash', str(SCRIPT)], env=_environment(tmp_path, FAXBOT_ASTERISK_COMMAND=str(running),
        FAXBOT_ASTERISK_CONTROL=str(control), FAXBOT_LOGIN_CHECK_SECONDS='0.1', ASTERISK_AMI_USERNAME='api',
        ASTERISK_AMI_PASSWORD='from-the-environment'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.4)
        (shared / 'manager.credentials').write_text('api\nchanged\n')
        assert process.wait(timeout=10) == 0
    finally:
        process.kill()
    assert 'secret=from-the-environment' in (tmp_path / 'etc' / 'manager.conf').read_text()
    assert not (tmp_path / 'control.log').exists()


def test_an_asterisk_started_first_runs_without_a_login_and_restarts_when_faxbot_writes_one(tmp_path):
    running, control = _stand_ins(tmp_path, 4)
    process = subprocess.Popen(['bash', str(SCRIPT)], env=_environment(tmp_path, FAXBOT_ASTERISK_COMMAND=str(running),
        FAXBOT_ASTERISK_CONTROL=str(control), FAXBOT_LOGIN_CHECK_SECONDS='0.1'),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait_for(tmp_path / 'data' / 'asterisk' / 'engine-started')
        assert 'enabled=no' in (tmp_path / 'etc' / 'manager.conf').read_text()
        time.sleep(0.3)
        assert not (tmp_path / 'control.log').exists()
        # Faxbot creates the login when the SIP trunk first comes into use.
        (tmp_path / 'data' / 'asterisk' / 'manager.credentials').write_text('api\nGenerated-Login_42\n')
        assert _wait_for(tmp_path / 'control.log') == '-rx core stop gracefully\n'
    finally:
        process.kill()
        process.wait(timeout=10)
    # Docker starts it again; this start reads the login.
    result, etc, _ = start(tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'secret=Generated-Login_42' in (etc / 'manager.conf').read_text()


def test_an_unusable_login_file_is_refused_without_echoing_it(tmp_path):
    shared = tmp_path / 'data' / 'asterisk'
    shared.mkdir(parents=True)
    (shared / 'manager.credentials').write_text('api\nbad;secret\n')
    result, _, _ = start(tmp_path)
    assert result.returncode != 0
    assert 'Unsupported AMI configuration syntax' in result.stderr and 'bad;secret' not in result.stderr
