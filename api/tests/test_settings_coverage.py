"""Every configuration value can be set from the command line and has a place in the console.

Each field of ConfigurationValues is accepted by `faxbot system settings set` and its
name appears in a console screen (api/admin_ui/src outside api/, tests and
mocks excluded: the API client and the type files are not a place), unless
READ_ONLY_OR_ENV says why not. AWAITING_CONSOLE holds the
fields the console has not placed yet: whoever places one removes its entry in
the same change (a listed field that the console now names fails here).

The second half covers the four settings promoted from environment-only reads
(SIP_PUBLIC_ADDRESS_CHECK_MINUTES, ENABLE_S3_DIAGNOSTICS, MOBILE_LOCAL_BASE,
DOCS_BASE_URL): each is set through PUT /admin/settings, read back and takes
effect without a restart, and an installation saved before the promotion takes
its variable once at the next start. All values are synthetic.
"""
import asyncio
import re
import sys
from types import SimpleNamespace

import pytest
from pydantic import AliasChoices

from app.config_values import ConfigurationValues
from api.tests.test_cli import BOOTSTRAP, Cli, _serve
from api.tests.test_config_env_secrets import (  # noqa: F401 (fixtures)
    ADMIN as INSTALLATION_ADMIN, Installation, _environment_audits, database_url, installation)
from api.tests.test_console_cli_parity import CONSOLE_SOURCE, screen_files

ADMIN = {'X-API-Key': BOOTSTRAP}

# Not set from the console or the command line, each for its reason.
READ_ONLY_OR_ENV = {
    'api_key': 'the installation key lives in .env and changes only through owner recovery; shown read-only as "Set in .env" under Access → Keys & phones',
    'database_url': 'moving the database needs the maintenance transfer; shown read-only',
    'fax_data_dir': 'the data folder is fixed for the installation; shown read-only',
    'faxbot_config_path': 'the older settings file, read once at first start; shown read-only under System → Developer',
    'require_api_key': 'authentication is always required; the console offers no switch to turn it off',
    # FreeSWITCH is kept for one release without a console page or a Setup choice; an installation that
    # still uses it reads and changes these with `faxbot providers show|configure freeswitch`.
    'fs_esl_host': 'FreeSWITCH, kept one release without a console page; faxbot providers configure freeswitch',
    'fs_esl_port': 'FreeSWITCH, kept one release without a console page; faxbot providers configure freeswitch',
    'fs_esl_password': 'FreeSWITCH, kept one release without a console page; faxbot providers configure freeswitch',
    'fs_t38_enable': 'FreeSWITCH, kept one release without a console page; faxbot providers configure freeswitch',
}

# Settings the console has not placed yet, with the home the map gives them. Builder L shrinks this.
AWAITING_CONSOLE: dict = {
    'sip_trunk_max_calls': 'Builder R places "Calls at once" on the trunk page in capacity M2',
    'sip_trunk_calls_per_second': 'Builder R places "New calls per second" on the trunk page in capacity M2',
}

PROMOTED = {'sip_public_address_check_minutes': 'SIP_PUBLIC_ADDRESS_CHECK_MINUTES',
            'enable_s3_diagnostics': 'ENABLE_S3_DIAGNOSTICS', 'mobile_local_base': 'MOBILE_LOCAL_BASE',
            'docs_base_url': 'DOCS_BASE_URL'}


def _console_names(files=None):
    """Settings named in the console's screens; the API client and the type files are not a place."""
    text = '\n'.join(path.read_text(encoding='utf-8') for path in (screen_files() if files is None else files))
    return {name for name in ConfigurationValues.model_fields if re.search(rf'\b{name}\b', text)}


def _variable(field):
    alias = field.validation_alias
    return alias.choices[0] if isinstance(alias, AliasChoices) else alias


# -- coverage --------------------------------------------------------------------------

def test_lists_name_real_settings_once_with_a_reason():
    fields = set(ConfigurationValues.model_fields)
    for name, entries in (('READ_ONLY_OR_ENV', READ_ONLY_OR_ENV), ('AWAITING_CONSOLE', AWAITING_CONSOLE)):
        assert sorted(set(entries) - fields) == [], f'{name} names settings that no longer exist'
        assert all(isinstance(reason, str) and reason.strip() for reason in entries.values()), name
    assert sorted(set(READ_ONLY_OR_ENV) & set(AWAITING_CONSOLE)) == []


def test_every_setting_is_in_the_console_or_listed():
    assert CONSOLE_SOURCE.is_dir()
    present = _console_names()
    missing = sorted(set(ConfigurationValues.model_fields) - present - set(READ_ONLY_OR_ENV) - set(AWAITING_CONSOLE))
    assert missing == [], f'Settings with no place in the console: {missing}'
    placed = sorted(set(AWAITING_CONSOLE) & present)
    assert placed == [], f'The console now names these; remove them from AWAITING_CONSOLE: {placed}'


def test_a_setting_named_only_by_the_api_client_or_types_has_no_place():
    api = sorted((CONSOLE_SOURCE / 'api').glob('*.ts'))
    assert api and not set(api) & set(screen_files())
    # A name in the response types alone is not a place on a screen.
    assert _console_names(api) - _console_names() <= set(READ_ONLY_OR_ENV)


def test_every_setting_has_a_name_the_settings_change_accepts():
    from app.cli.commands.settings import _request_names
    from app.main import UpdateSettingsRequest
    names = _request_names()
    refused = sorted(name for name in ConfigurationValues.model_fields
                     if names[name] not in UpdateSettingsRequest.model_fields)
    assert refused == []
    assert names['fax_backend'] == names['backend'] == 'backend'


@pytest.fixture
def cli(monkeypatch, tmp_path):
    for client in _serve(monkeypatch, tmp_path):
        yield Cli(client)


def _store(cli):
    return cli.client.app.state.configuration_runtime.manager.store


def test_the_command_line_sets_every_setting(cli):
    values = _store(cli).read().desired.values
    written = values.to_environment()
    assignments = []
    for name, field in ConfigurationValues.model_fields.items():
        if name in READ_ONLY_OR_ENV:
            continue
        value = getattr(values, name)
        typed = ('true' if value else 'false') if isinstance(value, bool) else str(value)
        assignments.append(f'{name}={written.get(_variable(field), typed)}')
    result = cli('--json', 'system', 'settings', 'set', *assignments)
    assert result.exit_code == 0, result.stdout + result.stderr

    # Text that looks like a number stays text: a caller ID, an outside-line prefix.
    typed = cli('system', 'settings', 'set', 'fs_caller_id_number=3035551234', 'sip_trunk_dial_prefix=9',
                'fax_backend=phaxio', 'max_file_size_mb=12', 'fax_disabled=yes')
    assert typed.exit_code == 0, typed.stdout + typed.stderr
    saved = _store(cli).read().desired.values
    assert (saved.fs_caller_id_number, saved.sip_trunk_dial_prefix, saved.fax_backend) == ('3035551234', '9', 'phaxio')
    assert (saved.max_file_size_mb, saved.fax_disabled) == (12, True)


# -- the four promoted settings --------------------------------------------------------

def _settings(client):
    response = client.get('/admin/settings', headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def _put(client, **changes):
    current = _settings(client)
    return client.put('/admin/settings', headers=ADMIN,
                      json={'expected_revision_id': current['_meta']['desired_revision_id'], **changes})


def test_promoted_settings_keep_their_variables_and_defaults():
    defaults = ConfigurationValues.from_environment({})
    assert (defaults.sip_public_address_check_minutes, defaults.enable_s3_diagnostics, defaults.mobile_local_base,
            defaults.docs_base_url) == (5, False, '', 'https://docs.faxbot.net/latest/')
    assert {name: _variable(ConfigurationValues.model_fields[name]) for name in PROMOTED} == PROMOTED
    read = ConfigurationValues.from_environment({'SIP_PUBLIC_ADDRESS_CHECK_MINUTES': '0',
                                                 'ENABLE_S3_DIAGNOSTICS': 'true',
                                                 'MOBILE_LOCAL_BASE': 'http://192.0.2.20:8080',
                                                 'DOCS_BASE_URL': 'https://docs.example.org/faxbot/'})
    assert (read.sip_public_address_check_minutes, read.enable_s3_diagnostics, read.mobile_local_base,
            read.docs_base_url) == (0, True, 'http://192.0.2.20:8080', 'https://docs.example.org/faxbot/')


def test_promoted_settings_need_the_permission_their_area_needs():
    from app.access.configuration import configuration_requirements
    before = ConfigurationValues.from_environment({})
    expected = {'sip_public_address_check_minutes': (12, 'providers:write'),
                'enable_s3_diagnostics': (True, 'providers:write'),
                # Where paired phones send their keys and where help links send people: owners only.
                'mobile_local_base': ('http://192.0.2.20:8080', 'owner:recover'),
                'docs_base_url': ('https://docs.example.org/faxbot/', 'owner:recover')}
    for name, (value, permission) in expected.items():
        assert configuration_requirements(before, before.with_patch({name: value})).permissions == {permission}, name


def test_the_documentation_address_is_a_setting_the_console_follows(cli):
    client = cli.client
    assert _settings(client)['developer'] == {'docs_base_url': 'https://docs.faxbot.net/latest/'}
    assert _put(client, docs_base_url='https://docs.example.org/faxbot/').status_code == 200
    assert _settings(client)['developer'] == {'docs_base_url': 'https://docs.example.org/faxbot/'}
    assert client.get('/admin/config', headers=ADMIN).json()['branding']['docs_base'] == 'https://docs.example.org/faxbot/'
    context = client.get('/auth/context', headers=ADMIN)
    assert context.status_code == 200 and context.json()['branding']['docs_base'] == 'https://docs.example.org/faxbot/'
    # Help links must stay a plain web address, or the console's sign-in context would fail.
    for refused in ('', 'docs.example.org', 'javascript:alert(1)', 'https://docs.example.org/?q=1',
                    'https://user:secret@docs.example.org/', 'https://docs.example.org/"x'):
        assert _put(client, docs_base_url=refused).status_code == 400, refused
    assert _settings(client)['developer'] == {'docs_base_url': 'https://docs.example.org/faxbot/'}


def test_the_phone_address_is_a_setting_pairing_hands_out(cli):
    client = cli.client
    assert _settings(client)['mobile'] == {'local_base': ''}
    assert _put(client, mobile_local_base='http://192.0.2.20:8080').status_code == 200
    assert _settings(client)['mobile'] == {'local_base': 'http://192.0.2.20:8080'}
    code = client.post('/admin/tunnel/pair', headers=ADMIN, json={}).json()['code']
    paired = client.post('/mobile/pair', json={'code': code, 'device_name': 'Synthetic phone'}, headers={'Origin': ''})
    assert paired.status_code == 200, paired.text
    assert paired.json()['base_urls']['local'] == 'http://192.0.2.20:8080'
    for refused in ('192.0.2.20:8080', 'ftp://192.0.2.20/', 'http://192.0.2.20:8080/?pair=1'):
        assert _put(client, mobile_local_base=refused).status_code == 400, refused
    # Empty offers no local address.
    assert _put(client, mobile_local_base='').status_code == 200
    code = client.post('/admin/tunnel/pair', headers=ADMIN, json={}).json()['code']
    paired = client.post('/mobile/pair', json={'code': code, 'device_name': 'Second phone'}, headers={'Origin': ''})
    assert paired.json()['base_urls']['local'] is None


def test_the_s3_check_is_a_setting_diagnostics_follow(cli, monkeypatch):
    import app.main as main_module
    client = cli.client
    asked = []

    class Bucket:
        def head_bucket(self, Bucket):
            asked.append(Bucket)

    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(client=lambda *args, **kwargs: Bucket()))
    monkeypatch.setitem(sys.modules, 'botocore', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'botocore.config', SimpleNamespace(Config=lambda **kwargs: kwargs))
    traits = main_module.providerHasTrait
    monkeypatch.setattr(main_module, 'providerHasTrait',
                        lambda role, trait: trait == 'needs_storage' or traits(role, trait))
    assert _put(client, storage_backend='s3', s3_bucket='synthetic-bucket', s3_region='us-east-1').status_code == 200
    assert _settings(client)['storage']['s3_diagnostics'] is False

    def storage():
        response = client.post('/admin/diagnostics/report', headers=ADMIN)
        assert response.status_code == 200, response.text
        found = [check for section in response.json()['sections'] for check in section['checks']
                 if check['id'] == 'server.storage']
        assert len(found) == 1, found
        return found[0]

    assert storage()['status'] == 'attention' and asked == []
    assert _put(client, enable_s3_diagnostics=True).status_code == 200
    assert _settings(client)['storage']['s3_diagnostics'] is True
    assert storage()['status'] == 'ok' and asked == ['synthetic-bucket']


@pytest.fixture
def trunk_cli(monkeypatch, tmp_path):
    from app import sip_http, stun
    monkeypatch.setattr(stun, 'probe', lambda servers, **_: stun.Probe(
        public_ip='198.51.100.7', local_ip='172.18.0.5', local_port=40000,
        mapped=(('stun.telnyx.com:3478', 61001), ('stun.cloudflare.com:3478', 61002))))
    monkeypatch.setattr(sip_http, '_probes', {})
    for client in _serve(monkeypatch, tmp_path, SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_USERNAME='faxbotuser',
                         SIP_TRUNK_PASSWORD='synthetic-Trunk-Pass!42', SIP_TRUNK_CALLER_ID='+15555550100'):
        yield Cli(client)


def test_the_address_check_pace_is_a_setting_the_watcher_follows(trunk_cli, monkeypatch):
    from app import sip_http
    client = trunk_cli.client
    store = _store(trunk_cli)
    assert _settings(client)['sip']['trunk']['public_address_check_minutes'] == 5
    assert _put(client, sip_public_address_check_minutes=12).status_code == 200
    assert _settings(client)['sip']['trunk']['public_address_check_minutes'] == 12
    waits, probes = [], []

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:  # turned off while the watcher waits: no check when it wakes
            assert _put(client, sip_public_address_check_minutes=0).status_code == 200
        if len(waits) == 3:
            raise asyncio.CancelledError

    async def probe(preset, *, fresh=False):
        probes.append(preset)

    class Clock:
        def __getattr__(self, name):
            return getattr(asyncio, name)

    clock = Clock()
    clock.sleep = sleep
    monkeypatch.setattr(sip_http, 'asyncio', clock)
    monkeypatch.setattr(sip_http, 'probe_network', probe)

    async def watch():
        # As main.py starts it: the store's current values, not the startup frame.
        await sip_http.watch_public_address(values_source=lambda: store.read().active.values)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(watch())
    assert waits == [720, 720, 60] and probes == ['telnyx']


# -- upgrading an installation saved before the promotion ------------------------------

def _earlier_release(monkeypatch):
    """Save configuration as a release from before the promotion did: without the four variables."""
    original = ConfigurationValues.to_environment

    def without_promoted(self, **kwargs):
        return {key: value for key, value in original(self, **kwargs).items() if key not in PROMOTED.values()}
    monkeypatch.setattr(ConfigurationValues, 'to_environment', without_promoted)


def test_an_installation_saved_before_the_promotion_takes_each_variable_once(installation):
    with pytest.MonkeyPatch.context() as earlier:
        _earlier_release(earlier)
        with installation.start():
            pass
    with installation.start(SIP_PUBLIC_ADDRESS_CHECK_MINUTES='9', ENABLE_S3_DIAGNOSTICS='true',
                            MOBILE_LOCAL_BASE='http://192.0.2.30:8080',
                            DOCS_BASE_URL='https://docs.example.org/faxbot/') as client:
        values = installation.store().read().active.values
        assert (values.sip_public_address_check_minutes, values.enable_s3_diagnostics, values.mobile_local_base,
                values.docs_base_url) == (9, True, 'http://192.0.2.30:8080', 'https://docs.example.org/faxbot/')
        assert _environment_audits(installation.store())[-1]['settings'] == sorted(PROMOTED)
        config = client.get('/admin/config', headers=INSTALLATION_ADMIN)
        assert config.status_code == 200, config.text
        assert config.json()['branding']['docs_base'] == \
            'https://docs.example.org/faxbot/'
    # From then on the console and the command line own the values; the variable is not taken again.
    with installation.start(SIP_PUBLIC_ADDRESS_CHECK_MINUTES='30'):
        assert installation.store().read().active.values.sip_public_address_check_minutes == 9
        assert len(_environment_audits(installation.store())) == 1


def test_an_invalid_variable_on_upgrade_is_left_out_and_faxbot_starts(installation, caplog):
    with pytest.MonkeyPatch.context() as earlier:
        _earlier_release(earlier)
        with installation.start():
            pass
    with installation.start(DOCS_BASE_URL='docs.example.org', MOBILE_LOCAL_BASE='http://192.0.2.31:8080') as client:
        values = installation.store().read().active.values
        assert (values.docs_base_url, values.mobile_local_base) == ('https://docs.faxbot.net/latest/',
                                                                    'http://192.0.2.31:8080')
        assert client.get('/health').status_code == 200
    assert 'DOCS_BASE_URL in the environment is not valid; Faxbot kept its saved setting.' in caplog.text
    assert 'docs.example.org' not in caplog.text


def test_a_new_installation_reads_the_variables_at_first_start(installation):
    with installation.start(SIP_PUBLIC_ADDRESS_CHECK_MINUTES='0', MOBILE_LOCAL_BASE='http://192.0.2.32:8080'):
        values = installation.store().read().active.values
        assert (values.sip_public_address_check_minutes, values.mobile_local_base) == (0, 'http://192.0.2.32:8080')
        assert _environment_audits(installation.store()) == []


# -- the installation's time zone ---------------------------------------------------------

def test_the_time_zone_defaults_from_tz_and_never_to_a_made_up_one():
    for environment, expected in (({}, ''), ({'TZ': 'America/Denver'}, 'America/Denver'), ({'TZ': 'UTC'}, ''),
                                  ({'TZ': ':America/Denver'}, 'America/Denver'), ({'TZ': 'EST5EDT,M3.2.0'}, ''),
                                  ({'FAX_TIME_ZONE': 'Europe/London', 'TZ': 'America/Denver'}, 'Europe/London')):
        assert ConfigurationValues.from_environment(environment).time_zone == expected, environment


def test_the_time_zone_is_a_setting_refused_with_one_sentence_when_unknown(cli):
    client = cli.client
    assert _settings(client)['installation'] == {'time_zone': ''}
    assert _put(client, time_zone='America/Denver').status_code == 200
    assert _settings(client)['installation'] == {'time_zone': 'America/Denver'}
    refused = _put(client, time_zone='Mars/Olympus')
    assert refused.status_code == 400
    assert refused.json()['detail'] == 'Choose a time zone from the list, such as America/Denver.'
    assert cli('system', 'settings', 'set', 'time_zone=Europe/London').exit_code == 0
    assert _settings(client)['installation'] == {'time_zone': 'Europe/London'}


def test_the_time_zone_needs_only_the_settings_permission():
    from app.access.configuration import configuration_requirements
    before = ConfigurationValues.from_environment({})
    assert configuration_requirements(before, before.with_patch({'time_zone': 'America/Denver'})).permissions == \
        {'settings:write'}


def test_a_new_installation_takes_its_zone_from_tz_and_an_upgrade_takes_it_once(installation):
    with pytest.MonkeyPatch.context() as earlier:
        _earlier_release(earlier)
        earlier.setattr(ConfigurationValues, 'to_environment', (lambda original: lambda self, **kwargs: {
            key: value for key, value in original(self, **kwargs).items() if key != 'FAX_TIME_ZONE'})(
                ConfigurationValues.to_environment))
        with installation.start():
            pass
    with installation.start(TZ='America/Denver'):
        assert installation.store().read().active.values.time_zone == 'America/Denver'
    with installation.start(TZ='Europe/London'):  # taken once; the console owns it from then on
        assert installation.store().read().active.values.time_zone == 'America/Denver'


def test_environment_only_settings_are_shown_read_only_and_never_a_secret(cli, monkeypatch):
    from app import config_views

    client = cli.client
    # No shipped environment-only setting is secret today; a synthetic one keeps the redaction rule tested.
    monkeypatch.setattr(config_views, 'DEPLOYMENT_VARIABLES', config_views.DEPLOYMENT_VARIABLES + ('SYNTHETIC_SECRET',))
    monkeypatch.setattr(config_views, 'SECRET_DEPLOYMENT_VARIABLES', frozenset({'SYNTHETIC_SECRET'}))
    monkeypatch.setenv('FAXBOT_MEDIA_PORTS', '10000-10100')
    monkeypatch.setenv('SYNTHETIC_SECRET', 'synthetic-secret-value')
    monkeypatch.delenv('TZ', raising=False)
    response = client.get('/admin/settings', headers=ADMIN)
    assert response.status_code == 200, response.text
    deployment = response.json()['deployment']
    assert deployment['FAXBOT_MEDIA_PORTS'] == {'set': True, 'value': '10000-10100'}
    assert deployment['SYNTHETIC_SECRET'] == {'set': True, 'value': None}
    assert deployment['TZ'] == {'set': False, 'value': None}
    assert 'synthetic-secret-value' not in response.text
    assert 'admin_allow_restart' in response.json()['owner_only']
