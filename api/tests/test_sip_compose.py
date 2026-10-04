"""The default Compose install publishes no SIP or media ports.

Asterisk registers with the carrier and starts every call's media from inside,
so nothing has to reach in. Publishing the old 4000-4999 media range started
one docker-proxy process per port and exhausted a 4 GB host before the API
could start. Only docker-compose.public.yml (public host, IP sign-in) publishes
ports, and its media range stays narrow and matches what Asterisk uses.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
NARROW = 32


def _published(service):
    """(host range, protocol) pairs from short-form or long-form port entries."""
    result = []
    for entry in service.get('ports') or []:
        if isinstance(entry, dict):
            published = str(entry.get('published', entry.get('target')))
            result.append((published, entry.get('protocol', 'tcp')))
        else:
            text, _, protocol = str(entry).partition('/')
            result.append((text.split(':')[-2] if text.count(':') else text, protocol or 'tcp'))
    return result


def _width(port_range):
    first, _, last = port_range.partition('-')
    return int(last or first) - int(first) + 1


def test_default_compose_file_publishes_no_asterisk_ports():
    services = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services']
    assert 'ports' not in services['asterisk']
    for name, service in services.items():
        for port_range, _ in _published(service):
            assert _width(port_range) == 1, (name, port_range)


@pytest.mark.skipif(shutil.which('docker') is None, reason='The docker command is not installed.')
def test_docker_compose_config_for_the_default_install_publishes_no_media_range():
    result = subprocess.run(['docker', 'compose', '-f', 'docker-compose.yml', 'config', '--format', 'json'],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-500:]
    services = json.loads(result.stdout)['services']
    assert 'asterisk' in services and not services['asterisk'].get('ports')
    published = [(name, port) for name, service in services.items() for port in service.get('ports') or []]
    assert all(int(port.get('published', 0)) not in range(4000, 5000) for _, port in published), published


@pytest.mark.parametrize('name', ['api', 'asterisk'])
def test_services_start_again_after_any_exit(name):
    """Restart API exits with code 0; only "always" or "unless-stopped" bring that process back."""
    service = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services'][name]
    assert service.get('restart') == 'unless-stopped'


@pytest.mark.skipif(shutil.which('docker') is None, reason='The docker command is not installed.')
def test_docker_compose_config_keeps_the_restart_policy():
    result = subprocess.run(['docker', 'compose', '-f', 'docker-compose.yml', '-f', 'docker-compose.public.yml',
                             'config', '--format', 'json'], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-500:]
    services = json.loads(result.stdout)['services']
    assert {services[name].get('restart') for name in ('api', 'asterisk')} == {'unless-stopped'}


def test_public_override_publishes_one_narrow_range_that_asterisk_uses():
    asterisk = yaml.safe_load((ROOT / 'docker-compose.public.yml').read_text())['services']['asterisk']
    ranges = [port_range for port_range, protocol in _published(asterisk) if _width(port_range) > 1]
    assert len(ranges) == 1 and _width(ranges[0]) <= NARROW
    environment = dict(item.split('=', 1) for item in asterisk['environment'])
    assert environment['FAXBOT_MEDIA_PORTS'] == ranges[0]


def test_start_script_splits_the_published_range_between_t38_and_audio():
    script = (ROOT / 'asterisk' / 'start.sh').read_text()
    assert 'FAXBOT_MEDIA_PORTS' in script
    assert re.search(r'udptl_last=\$\(\( first \+ \(last - first \+ 1\) / 3 - 1 \)\)', script)
    for name in ('rtp.conf', 'udptl.conf'):
        text = (ROOT / 'asterisk' / 'etc' / 'asterisk' / name).read_text()
        assert 'Publish UDP' not in text
