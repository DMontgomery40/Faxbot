"""The internet address Faxbot found reaches Asterisk at start, exactly or not at all.

Runs the image's own ``faxbot-public-address`` helper on rendered trunk files:
an IPv4 address on a network that keeps port numbers is filled in with this
container's subnet as local_net; anything else removes the three lines, so the
carrier follows Faxbot's packets instead of a public address with the wrong port.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app import sip_trunk, stun
from app.config_values import ConfigurationValues


HELPER = Path(__file__).resolve().parents[2] / 'asterisk' / 'bin' / 'faxbot-public-address'
LINES = ('external_media_address=@FAXBOT_PUBLIC_ADDRESS@\nexternal_signaling_address=@FAXBOT_PUBLIC_ADDRESS@\n'
         'local_net=@FAXBOT_LOCAL_NET@\n')
pytestmark = pytest.mark.skipif(shutil.which('bash') is None, reason='bash is not installed.')


def values(tmp_path, **extra):
    return ConfigurationValues.from_environment({
        'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser', 'SIP_TRUNK_PASSWORD': 'synthetic-pass',
        'SIP_TRUNK_CALLER_ID': '+15555550100', 'FAX_DATA_DIR': str(tmp_path), **extra})


def run_helper(tmp_path, record, *, local_net='172.18.0.0/16', text=None):
    conf = tmp_path / 'pjsip.conf'
    conf.write_text(text if text is not None else '[transport-tls]\nbind=0.0.0.0:5061\n' + LINES + '\n[trunk-aor]\n')
    info = tmp_path / 'public-address'
    if record is not None:
        info.write_text(record if isinstance(record, str) else json.dumps(record) + '\n')
    # Only the tools the helper needs, so the host cannot answer for it: with an empty
    # FAXBOT_LOCAL_NET the helper asks `ip route` for the subnet when `ip` exists (it
    # does on Linux, not on macOS), and "no subnet known" must mean exactly that here.
    tools = tmp_path / 'bin'
    tools.mkdir(exist_ok=True)
    for tool in ('sed', 'grep', 'head', 'awk', 'mktemp', 'mv'):
        found = shutil.which(tool)
        assert found, f'{tool} is not installed.'
        (tools / tool).symlink_to(found)
    environment = {'PATH': str(tools), 'FAXBOT_PUBLIC_ADDRESS_FILE': str(info), 'FAXBOT_LOCAL_NET': local_net}
    result = subprocess.run([shutil.which('bash'), str(HELPER), str(conf)], env=environment, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    applied = tmp_path / 'public-address.applied'
    return conf.read_text(), applied.read_text().strip() if applied.exists() else None


def test_an_address_on_a_network_that_keeps_ports_is_filled_in_with_the_container_subnet(tmp_path):
    text, applied = run_helper(tmp_path, {'ip': '198.51.100.7', 'ports_preserved': True, 'probed_at': 1})
    assert ('external_media_address=198.51.100.7\nexternal_signaling_address=198.51.100.7\n'
            'local_net=172.18.0.0/16\n') in text
    assert '@FAXBOT' not in text and applied == '198.51.100.7'


@pytest.mark.parametrize('record,local_net', [
    ({'ip': '198.51.100.7', 'ports_preserved': False, 'probed_at': 1}, '172.18.0.0/16'),   # ports change
    (None, '172.18.0.0/16'),                                                                # never probed
    ({'ip': None, 'ports_preserved': False, 'probed_at': 1}, '172.18.0.0/16'),             # STUN blocked
    ({'ip': '198.51.100.999', 'ports_preserved': True}, '172.18.0.0/16'),                  # not an address
    ('{"ip": "198.51.100.7;evil", "ports_preserved": true}', '172.18.0.0/16'),             # injected text
    ({'ip': '198.51.100.7', 'ports_preserved': True}, ''),                                 # no subnet known
    ({'ip': '198.51.100.7', 'ports_preserved': True}, '172.18.0.0/16; rm -rf /'),         # bad subnet
])
def test_anything_less_than_an_exact_address_removes_the_lines(tmp_path, record, local_net):
    text, applied = run_helper(tmp_path, record, local_net=local_net)
    assert 'external_' not in text and 'local_net' not in text and '@FAXBOT' not in text
    assert text.startswith('[transport-tls]\nbind=0.0.0.0:5061\n\n[trunk-aor]') and applied == ''


def test_a_typed_address_or_an_older_file_is_left_alone(tmp_path):
    typed = '[transport-udp]\nexternal_media_address=203.0.113.10\nlocal_net=10.0.0.0/8\n'
    text, applied = run_helper(tmp_path, {'ip': '198.51.100.7', 'ports_preserved': True}, text=typed)
    assert text == typed and applied is None


def test_apply_records_what_stun_found_and_reports_when_the_advertised_address_changes(tmp_path):
    settings = values(tmp_path)
    keeps = stun.Probe(public_ip='198.51.100.7', local_ip='172.18.0.5', local_port=40000,
                       mapped=(('a', 40000), ('b', 40000)), probed_at=1791075343.0)
    changes = stun.Probe(public_ip='198.51.100.7', local_ip='172.18.0.5', local_port=40000,
                         mapped=(('a', 61001), ('b', 61002)), probed_at=1791075400.0)
    assert sip_trunk.write_public_address(settings, keeps) is True
    assert sip_trunk.read_public_address(settings) == {'ip': '198.51.100.7', 'ports_preserved': True,
                                                       'probed_at': 1791075343}
    assert sip_trunk.write_public_address(settings, keeps) is False
    assert sip_trunk.write_public_address(settings, changes) is True
    assert sip_trunk.read_public_address(settings)['ports_preserved'] is False
    assert sip_trunk.applied_public_address(settings) is None
    # The helper reads exactly the file Faxbot wrote.
    sip_trunk.write_public_address(settings, keeps)
    rendered = sip_trunk.render_pjsip(settings)
    assert rendered.count('@FAXBOT_PUBLIC_ADDRESS@') == 2 and 'local_net=@FAXBOT_LOCAL_NET@' in rendered
    conf = tmp_path / 'pjsip.conf'
    conf.write_text(rendered)
    result = subprocess.run(['bash', str(HELPER), str(conf)], capture_output=True, text=True, timeout=30,
                            env={'PATH': '/usr/bin:/bin', 'FAXBOT_LOCAL_NET': '172.18.0.0/16',
                                 'FAXBOT_PUBLIC_ADDRESS_FILE': str(sip_trunk.public_address_path(settings))})
    assert result.returncode == 0, result.stderr
    assert 'external_signaling_address=198.51.100.7' in conf.read_text()
    assert sip_trunk.applied_public_address(settings) == '198.51.100.7'
