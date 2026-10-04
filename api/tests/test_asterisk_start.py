"""asterisk/start.sh: what the Asterisk container loads at start and what it tells Faxbot.

The script runs here with its directories pointed at a temporary folder and a
stand-in for the asterisk binary, so no container is needed.
"""
from pathlib import Path
import os
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'asterisk' / 'start.sh'
TEMPLATES = ROOT / 'asterisk' / 'etc' / 'asterisk' / 'templates'
PUBLIC_ADDRESS = ROOT / 'asterisk' / 'bin' / 'faxbot-public-address'

pytestmark = pytest.mark.skipif(shutil.which('bash') is None or shutil.which('envsubst') is None,
                                reason='bash and envsubst are needed to run the start script.')

TRUNK = '\n'.join(['[global]', 'type=global', '', '[transport-tls]', 'type=transport',
                   'external_media_address=@FAXBOT_PUBLIC_ADDRESS@', 'local_net=@FAXBOT_LOCAL_NET@', ''])


def start(tmp_path, **environment):
    """Run start.sh once; returns (completed process, etc folder, shared folder)."""
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
    env = {'PATH': os.environ['PATH'], 'FAXBOT_ASTERISK_ETC': str(etc), 'FAXBOT_ASTERISK_TEMPLATES': str(TEMPLATES),
           'FAXBOT_DATA': str(data), 'FAXBOT_PUBLIC_ADDRESS_BIN': str(helper),
           'FAXBOT_PUBLIC_ADDRESS_FILE': str(data / 'asterisk' / 'public-address'), 'FAXBOT_LOCAL_NET': '172.18.0.0/16',
           'FAXBOT_ASTERISK_COMMAND': str(stand_in), **environment}
    result = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    return result, etc, data / 'asterisk'


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
