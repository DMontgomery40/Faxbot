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


ENGINE_CAPABILITIES = {
    # start.sh hands Asterisk its folders (CHOWN; FSETID keeps the data folder's setgid bit), Asterisk drops
    # to its own user (SETUID, SETGID), and a stop reaches it (KILL).
    'asterisk': ['CHOWN', 'FSETID', 'SETUID', 'SETGID', 'KILL'],
    # The root start script writes the uucp spool's settings (CHOWN, DAC_OVERRIDE, FOWNER), the daemons switch
    # to uucp (SETUID, SETGID), the job server shuts each session into the spool (SYS_CHROOT), and a stop
    # reaches the daemons (KILL).
    'hylafax': ['CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'SETUID', 'SETGID', 'SYS_CHROOT', 'KILL'],
}


@pytest.mark.parametrize('name', sorted(ENGINE_CAPABILITIES))
def test_the_engine_containers_keep_only_the_capabilities_they_need(name):
    """Asterisk and the fax engine parse other machines' packets. Each drops every capability but the few
    its root start script needs; each capability is shown to be needed by starting the image without it
    (tests/test_engine_processes.py, FAXBOT_NATIVE_PROOF=1). Nothing in Asterisk's container can gain
    privileges. The engine cannot take that option: HylaFAX starts its sender and scripts working as uucp
    with root as their real user, which no-new-privileges turns into root with no capabilities, and the
    sender then cannot open its fax line (the loopback proof's case n found it)."""
    service = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services'][name]
    assert service.get('security_opt') == (['no-new-privileges:true'] if name == 'asterisk' else None)
    assert service['cap_drop'] == ['ALL'] and service['cap_add'] == ENGINE_CAPABILITIES[name]
    assert 'privileged' not in service and 'NET_BIND_SERVICE' not in service['cap_add']
    if name == 'asterisk':
        settings = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'asterisk.conf').read_text()
        assert 'runuser=asterisk\n' in settings and 'rungroup=asterisk\n' in settings
        assert 'astctlpermissions=0660\n' in settings
        assert 'groupadd --system --gid 5060 asterisk' in (ROOT / 'asterisk' / 'Dockerfile').read_text()


@pytest.mark.skipif(shutil.which('docker') is None, reason='The docker command is not installed.')
def test_every_compose_file_keeps_the_engine_containers_privileges():
    """The override files (public host, phone system, SSL Fax listener, fax ports) change ports, never privileges."""
    for extra in ('docker-compose.public.yml', 'docker-compose.sslfax.yml', 'docker-compose.fax-ports.yml'):
        result = subprocess.run(['docker', 'compose', '-f', 'docker-compose.yml', '-f', extra, 'config',
                                 '--format', 'json'], cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, (extra, result.stderr[-500:])
        services = json.loads(result.stdout)['services']
        for name, capabilities in ENGINE_CAPABILITIES.items():
            assert services[name]['cap_drop'] == ['ALL'], (extra, name)
            assert sorted(services[name]['cap_add']) == sorted(capabilities), (extra, name)
            assert services[name].get('security_opt') == (['no-new-privileges:true'] if name == 'asterisk'
                                                          else None), (extra, name)


def test_asterisk_starts_without_errors_and_keeps_every_fax_module():
    """Every start logged a burst of "X declined to load" ERRORs (seen on the acceptance install).

    The native proof (make native-proof) checks the real start log; this keeps
    the shipped configuration from drifting back.
    """
    etc = ROOT / 'asterisk' / 'etc' / 'asterisk'
    entries = re.findall(r'^(require|load|noload) => (\S+)$', (etc / 'modules.conf').read_text(), re.M)
    skipped = {module for kind, module in entries if kind == 'noload'}
    needed = {module for kind, module in entries if kind != 'noload'}
    assert not skipped & needed
    assert {'func_shell.so', 'func_base64.so', 'app_stack.so', 'app_userevent.so', 'res_fax.so',
            'res_fax_spandsp.so', 'chan_pjsip.so', 'res_pjsip.so'} <= needed
    assert {'app_amd.so', 'pbx_dundi.so', 'chan_unistim.so', 'app_festival.so', 'app_alarmreceiver.so',
            'app_followme.so', 'pbx_ael.so', 'res_prometheus.so', 'app_queue.so'} <= skipped
    # Core and PJSIP pieces report a missing file as an ERROR; each ships a minimal one.
    for name in ('cdr.conf', 'cel.conf', 'features.conf', 'acl.conf', 'pjproject.conf', 'pjsip_wizard.conf',
                 'statsd.conf', 'aeap.conf', 'websocket_client.conf', 'chan_websocket.conf', 'ari.conf'):
        assert (etc / name).is_file(), name
    assert 'astkeydir=/var/lib/asterisk\n' in (etc / 'asterisk.conf').read_text()


# A phone system on the local network: SIP and a small media range, published on the LAN address only.

PHONE_SYSTEM = ROOT / 'docker-compose.phone-system.yml'


def test_phone_system_override_publishes_sip_and_one_small_range_on_the_lan_address_only():
    asterisk = yaml.safe_load(PHONE_SYSTEM.read_text())['services']['asterisk']
    entries = [str(entry) for entry in asterisk['ports']]
    assert len(entries) == 3
    for entry in entries:
        # Every port is bound to the address the operator gives, never to every interface.
        assert entry.startswith('${FAXBOT_LAN_ADDRESS:?'), entry
    assert entries[0].endswith(':5060:5060/udp') and entries[1].endswith(':5060:5060/tcp')
    assert entries[2].endswith(':${FAXBOT_MEDIA_PORTS:-4000-4019}:${FAXBOT_MEDIA_PORTS:-4000-4019}/udp')
    environment = dict(item.split('=', 1) for item in asterisk['environment'])
    # Asterisk uses exactly the published range; only this file tells it the phone system address.
    assert environment['FAXBOT_MEDIA_PORTS'] == '${FAXBOT_MEDIA_PORTS:-4000-4019}'
    assert environment['FAXBOT_PHONE_SYSTEM_ADDRESS'].startswith('${FAXBOT_LAN_ADDRESS:?')
    assert _width('4000-4019') == 20


def _phone_system_config(extra):
    import os
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('FAXBOT_') and key != 'COMPOSE_FILE'}
    # An empty env file: a .env in the checkout must not supply the LAN address (output is parsed, never printed).
    command = ['docker', 'compose', '-p', 'faxbot-phone-system-check', '--env-file', os.devnull,
               '-f', 'docker-compose.yml', '-f', 'docker-compose.phone-system.yml', 'config', '--format', 'json']
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60,
                          env={**environment, **extra})


@pytest.mark.skipif(shutil.which('docker') is None, reason='The docker command is not installed.')
def test_docker_compose_config_binds_every_phone_system_port_to_the_lan_address():
    result = _phone_system_config({'FAXBOT_LAN_ADDRESS': '192.0.2.20'})
    assert result.returncode == 0, 'docker compose config failed'
    services = json.loads(result.stdout)['services']
    ports = services['asterisk']['ports']
    assert {port.get('host_ip') for port in ports} == {'192.0.2.20'}
    assert sorted((int(port['published']), port['protocol']) for port in ports) == sorted(
        [(5060, 'udp'), (5060, 'tcp')] + [(number, 'udp') for number in range(4000, 4020)])
    assert services['asterisk']['environment']['FAXBOT_PHONE_SYSTEM_ADDRESS'] == '192.0.2.20'
    assert services['asterisk']['restart'] == 'unless-stopped'
    # The operator can widen the range; Asterisk refuses more than 100 ports at start (test_asterisk_start).
    wider = _phone_system_config({'FAXBOT_LAN_ADDRESS': '192.0.2.20', 'FAXBOT_MEDIA_PORTS': '4000-4049'})
    assert wider.returncode == 0
    wider_ports = json.loads(wider.stdout)['services']['asterisk']['ports']
    assert len(wider_ports) == 52 and json.loads(wider.stdout)['services']['asterisk']['environment'][
        'FAXBOT_MEDIA_PORTS'] == '4000-4049'


@pytest.mark.skipif(shutil.which('docker') is None, reason='The docker command is not installed.')
def test_docker_compose_refuses_the_phone_system_file_without_a_lan_address():
    result = _phone_system_config({})
    assert result.returncode != 0 and 'FAXBOT_LAN_ADDRESS' in result.stderr


# Secrets: only the API reads .env; every other service names exactly what it may receive.

COMPOSE_FILES = sorted(ROOT.glob('docker-compose*.yml'))
# Secret-bearing variables each service may receive, besides the API (which reads .env for its settings).
SECRETS_ALLOWED = {
    'asterisk': {'SIP_PASSWORD', 'ASTERISK_AMI_PASSWORD', 'ASTERISK_INBOUND_SECRET'},
    'hylafax': set(),
    'faxbot-mcp': set(),
    'faxbot-mcp-py-sse': set(),
}


def _secret_names():
    """Every variable of a setting Faxbot marks secret."""
    from pydantic import AliasChoices
    from app.config_values import ConfigurationValues
    names = set()
    for field in ConfigurationValues.model_fields.values():
        if (field.json_schema_extra or {}).get('secret'):
            alias = field.validation_alias
            names.update(alias.choices if isinstance(alias, AliasChoices) else [alias])
    return names


def _secret_bearing(name, known):
    """A secret setting's variable, or any *PASS*, *SECRET*, *TOKEN* or *KEY* name (a *_FILE path is not one)."""
    return name in known or (re.search(r'PASS|SECRET|TOKEN|(^|_)KEY($|_)', name) is not None
                             and not name.endswith('_FILE'))


def _environment_names(service):
    entries = service.get('environment') or []
    if isinstance(entries, dict):
        return set(entries)
    return {str(entry).split('=', 1)[0] for entry in entries}


@pytest.mark.parametrize('path', COMPOSE_FILES, ids=[path.name for path in COMPOSE_FILES])
def test_only_the_api_reads_env_and_each_service_gets_only_its_own_secrets(path):
    known = _secret_names()
    assert {'ASTERISK_AMI_PASSWORD', 'TELNYX_API_KEY'} <= known
    services = yaml.safe_load(path.read_text())['services']
    for name, service in services.items():
        if name == 'api':
            continue
        assert 'env_file' not in service, f'{path.name}: {name} must list its variables, not read all of .env'
        received = {variable for variable in _environment_names(service) if _secret_bearing(variable, known)}
        assert received <= SECRETS_ALLOWED[name], (path.name, name, sorted(received - SECRETS_ALLOWED[name]))


def test_asterisk_gets_exactly_what_its_start_script_and_dialplan_read():
    asterisk = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services']['asterisk']
    names = _environment_names(asterisk)
    assert names == {'TZ', 'SIP_USERNAME', 'SIP_PASSWORD', 'SIP_SERVER', 'SIP_FROM_DOMAIN', 'SIP_REGISTER',
                     'ASTERISK_AMI_USERNAME', 'ASTERISK_AMI_PASSWORD', 'ASTERISK_INBOUND_SECRET', 'FAX_HEADER',
                     'FAX_LOCAL_STATION_ID', 'FAXBOT_LOCAL_NET', 'FAXBOT_API_ADDRESS'}
    # The provider, mail and carrier keys the audit found in Asterisk's environment are never named.
    assert not names & {'GMAIL_PASSWORD', 'HUMBLEFAX_API_ACCESS_KEY', 'INTAKE_SMTP_PASSWORD', 'TELNYX_API_KEY',
                        'TELNYX_PASS', 'PHAXIO_API_SECRET'}
    # Every variable the start script, the helpers or the dialplan reads is still passed in.
    read = set()
    for script in ('start.sh', 'bin/faxbot-inbound-notify', 'bin/faxbot-public-address'):
        read |= set(re.findall(r'\$\{?((?:SIP|ASTERISK|FAX|FAXBOT)_[A-Z_]+)', (ROOT / 'asterisk' / script).read_text()))
    read |= set(re.findall(r'ENV\(([A-Z_]+)\)',
                           (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()))
    # Image paths and test hooks keep their built-in defaults; the overrides set the media and phone system values.
    builtin = {'FAXBOT_ASTERISK_TEMPLATES', 'FAXBOT_ASTERISK_ETC', 'FAXBOT_DATA', 'FAXBOT_TRUNK_CONF',
               'FAXBOT_PUBLIC_ADDRESS_BIN', 'FAXBOT_ASTERISK_COMMAND', 'FAXBOT_ASTERISK_CONTROL',
               'FAXBOT_LOGIN_CHECK_SECONDS', 'FAXBOT_MEDIA_PORTS', 'FAXBOT_PHONE_SYSTEM_ADDRESS',
               'FAXBOT_MANAGER_PERMIT', 'FAXBOT_DATA_DIR', 'FAXBOT_API_URL', 'FAXBOT_NOTIFY_RETRY_SECONDS',
               'FAXBOT_PUBLIC_ADDRESS_FILE'}
    assert read - builtin <= names, sorted(read - builtin - names)


@pytest.mark.parametrize('path', COMPOSE_FILES, ids=[path.name for path in COMPOSE_FILES])
def test_every_service_keeps_its_logs_small(path):
    base = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services']
    for name in yaml.safe_load(path.read_text())['services']:
        logging = base[name].get('logging') or {}
        assert logging.get('driver') == 'json-file', (path.name, name)
        assert logging.get('options') == {'max-size': '10m', 'max-file': '3'}, (path.name, name)
