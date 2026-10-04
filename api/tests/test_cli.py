"""The faxbot command line against the real application, in process over HTTPS.

Each command runs through Typer's CliRunner. Its HTTP client is the TestClient,
an httpx client bound to the ASGI app, so requests take the same path as a real
server: credential transport, access policy, routing and storage.
"""
from io import BytesIO
import json
import re
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from typer.testing import CliRunner

import app.main as main_module
from app.cli.main import app as cli_app
from app.config_values import ConfigurationValues

BOOTSTRAP = 'synthetic-cli-bootstrap-key'
ORIGIN = 'https://testserver'
API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parent


def _serve(monkeypatch, tmp_path, **extra):
    for name in ConfigurationValues.environment_keys():
        monkeypatch.delenv(name, raising=False)
    for name in ('FAXBOT_URL', 'FAXBOT_API_KEY', 'FAXBOT_PROFILE', 'FAXBOT_CLI_DEBUG'):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        'DATABASE_URL': f"sqlite:///{tmp_path / 'cli.db'}",
        'FAX_DATA_DIR': str(tmp_path / 'faxdata'),
        'FAXBOT_INSTALLATION_KEY_PATH': str(tmp_path / 'installation.key'),
        'FAXBOT_CONFIG_PATH': str(tmp_path / 'absent-legacy.json'),
        'FAXBOT_PROVIDERS_DIR': str(tmp_path / 'providers'),
        'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / 'direct.key'),
        'FAX_DISABLED': 'true',
        'FAX_BACKEND': 'phaxio',
        'FAX_OUTBOUND_BACKEND': 'phaxio',
        'INBOUND_ENABLED': 'true',
        'REQUIRE_API_KEY': 'true',
        'API_KEY': BOOTSTRAP,
        'PUBLIC_API_URL': ORIGIN,
        'FAXBOT_CONSOLE_ORIGINS': ORIGIN,
        'MAX_REQUESTS_PER_MINUTE': '0',
        'ENABLE_PERSISTED_SETTINGS': 'false',
        'ENABLE_MCP_SSE': 'false',
        'ENABLE_MCP_HTTP': 'false',
        'DIRECT_DELIVERY_ENABLED': 'true',
        'DIRECT_ORGANIZATION': 'County Clinic',
        'DIRECT_FAX_NUMBER': '+15550006666',
        'DIRECT_ALLOW_PRIVATE_PEERS': 'true',  # synthetic partners have no public DNS
        'FAXBOT_CLI_CONFIG': str(tmp_path / 'cli-config' / 'config.toml'),
        'COLUMNS': '200',
        'TZ': 'UTC',
        **extra,
    }.items():
        monkeypatch.setenv(name, value)
    time.tzset()
    monkeypatch.setattr(main_module, '_PAIR_ATTEMPTS', {})
    with TestClient(main_module.app, base_url=ORIGIN) as client:
        yield client
    main_module.app.state.direct_http = None
    time.tzset()


@pytest.fixture
def server(monkeypatch, tmp_path):
    yield from _serve(monkeypatch, tmp_path)


@pytest.fixture
def plugins_cli(monkeypatch, tmp_path):
    for client in _serve(monkeypatch, tmp_path, FEATURE_V3_PLUGINS='true'):
        yield Cli(client)


class Cli:
    def __init__(self, client):
        self.client = client
        self.runner = CliRunner()

    def __call__(self, *args, key=BOOTSTRAP, input=None, url=ORIGIN):
        prefix = (['--url', url] if url else []) + (['--key', key] if key else [])
        return self.runner.invoke(cli_app, prefix + [str(arg) for arg in args], input=input,
                                  obj={'client_factory': lambda address, timeout: (self.client, False)})

    def json(self, *args, key=BOOTSTRAP, input=None):
        result = self(*(['--json'] + list(args)), key=key, input=input)
        assert result.exit_code == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)


@pytest.fixture
def cli(server):
    return Cli(server)


def pdf(path, text='Synthetic case document'):
    output = BytesIO()
    page = canvas.Canvas(output)
    page.drawString(40, 700, text)
    page.showPage()
    page.save()
    path.write_bytes(output.getvalue())
    return path


def restricted_key(cli, name='Front desk scanner', role='Fax operator', permissions=('fax:send', 'fax:read')):
    """An integration holding one role and a key limited to some of it."""
    assert cli('integrations', 'add', name).exit_code == 0
    assert cli('access', 'grant', name, role).exit_code == 0
    created = cli.json('keys', 'create', '--for', name, *[part for p in permissions for part in ('-p', p)],
                       '--name', name + ' key')
    return created['key']['id'], created['token']


# -- global behaviour ------------------------------------------------------------------

def test_help_lists_every_command_group_without_starting_the_server():
    result = CliRunner().invoke(cli_app, ['--help'], env={'COLUMNS': '200'})
    assert result.exit_code == 0
    # Hosted CI forces colour, so compare the help text without escape codes.
    plain = re.sub(r'\x1b\[[0-9;]*m', '', result.stdout)
    for group in ('send', 'status', 'jobs', 'inbound', 'users', 'integrations', 'groups', 'roles', 'access',
                  'resources', 'keys', 'sessions', 'mailboxes', 'numbers', 'audit', 'settings', 'providers',
                  'routing', 'intake', 'direct', 'cases', 'pair', 'owner', 'health', 'diagnostics', 'me',
                  'config', 'admin'):
        assert f' {group} ' in plain


def test_me_health_and_errors_map_to_plain_sentences(cli):
    me = cli.json('me')
    assert me['principal']['kind'] == 'bootstrap' and 'owner:recover' in me['permissions']
    human = cli('me')
    assert human.exit_code == 0 and 'installation key' in human.stdout
    health = cli('--json', 'health', key=None)
    assert json.loads(health.stdout)['live'] == {'status': 'ok'}
    assert health.exit_code in (0, 1)

    missing = cli('me', key=None)
    assert missing.exit_code == 3 and missing.stderr.startswith('No API key.')
    wrong = cli('me', key='fbk_live_000000000000_synthetic')
    assert wrong.exit_code == 3
    assert wrong.stderr.strip() == ('Faxbot did not accept the API key. Check --key, FAXBOT_API_KEY or your saved '
                                    'profile.')
    unknown = cli('status', '0' * 32)
    assert unknown.exit_code == 5 and unknown.stderr.strip() == 'Fax not found.'
    as_json = cli('--json', 'status', '0' * 32)
    assert as_json.exit_code == 5
    assert json.loads(as_json.stdout)['error'] == {'message': 'Fax not found.', 'exit_code': 5, 'status': 404,
                                                   'detail': 'Fax not found.'}
    unreachable = CliRunner().invoke(cli_app, ['--url', 'http://127.0.0.1:9', '--key', 'x', 'me'])
    assert unreachable.exit_code == 8 and 'Could not reach Faxbot at http://127.0.0.1:9' in unreachable.stderr
    assert 'Traceback' not in unreachable.stderr + unreachable.stdout


# -- faxes ------------------------------------------------------------------------------

def test_send_status_jobs_and_documents(cli, tmp_path):
    note = tmp_path / 'note.txt'
    note.write_text('Synthetic command line fax\n')
    sent = cli.json('send', '+15551230001', note, '--queue', '--idempotency-key', 'cli-test-1')
    assert sent['status'] == 'queued' and sent['delivery_state'] == 'held'
    again = cli.json('send', '+15551230001', note, '--queue', '--idempotency-key', 'cli-test-1')
    assert again['id'] == sent['id']
    human = cli('send', '+15551230002', note, '--queue')
    assert human.exit_code == 0 and 'Fax accepted.' in human.stdout and 'faxbot status' in human.stdout

    assert cli.json('status', sent['id'])['id'] == sent['id']
    listing = cli.json('jobs', 'list')
    assert listing['total'] == 2 and all(job['to_number'].startswith('*') for job in listing['jobs'])
    table = cli('jobs', 'list')
    assert table.exit_code == 0 and sent['id'] not in table.stdout and '+15551230001' not in table.stdout
    assert sent['id'] in cli('jobs', 'list', '--ids').stdout
    assert cli.json('jobs', 'get', sent['id'])['id'] == sent['id']
    history = cli.json('jobs', 'history', sent['id'])
    assert history['state'] == 'held'

    target = tmp_path / 'downloaded.pdf'
    saved = cli.json('jobs', 'pdf', sent['id'], '-o', target)
    assert saved['saved_to'] == str(target) and target.read_bytes().startswith(b'%PDF')
    refused = cli('jobs', 'pdf', sent['id'], '-o', target)
    assert refused.exit_code == 1 and 'already exists' in refused.stderr
    unconfirmed = cli('jobs', 'reconcile', sent['id'], '--provider-fax-id', 'abc123')
    assert unconfirmed.exit_code == 1 and '--confirm-original-account' in unconfirmed.stderr


def test_sending_is_refused_for_a_key_without_permission(cli, tmp_path):
    note = tmp_path / 'note.txt'
    note.write_text('Synthetic\n')
    _, token = restricted_key(cli, 'Viewer', role='Auditor', permissions=('fax:read',))
    denied = cli('send', '+15551230001', note, '--queue', key=token)
    assert denied.exit_code == 4
    assert denied.stderr.strip() == 'This API key is not allowed to do that.'


def test_received_faxes_simulate_list_get_and_download(cli, tmp_path):
    inbound_id = cli.json('inbound', 'simulate', '--from', '+15559990000', '--to', '+15551112222')['id']
    items = cli.json('inbound', 'list')
    assert [item['id'] for item in items] == [inbound_id]
    assert inbound_id not in cli('inbound', 'list').stdout and inbound_id in cli('inbound', 'list', '--ids').stdout
    assert cli.json('inbound', 'get', inbound_id)['to'] == '+15551112222'
    target = tmp_path / 'received.pdf'
    cli.json('inbound', 'pdf', inbound_id, '--output', target)
    assert target.read_bytes().startswith(b'%PDF')
    listing = cli('inbound', 'list').stdout
    assert 'Status' in listing and 'A test fax created in Faxbot.' in listing
    detail = cli('inbound', 'get', inbound_id).stdout
    digest = cli.json('inbound', 'get', inbound_id)['sha256']
    assert f'SHA-256 {digest[:12]}…' in detail and digest not in detail
    assert re.search(r'Document\s+1 page, ', detail) and re.search(r'Test fax\s+yes', detail)
    assert 'Provider fax ID' in detail and re.search(r'Sent\s+-', detail)
    received = cli('inbound', 'fetch', inbound_id)
    assert received.exit_code == 6 and received.stderr.strip() == 'Faxbot has nothing to fetch for this fax.'
    _, token = restricted_key(cli, 'Sender', role='Fax operator', permissions=('fax:send',))
    hidden = cli('inbound', 'get', inbound_id, key=token)
    assert hidden.exit_code == 5


# -- access management -----------------------------------------------------------------

def test_users_groups_roles_access_mailboxes_numbers_audit_and_sessions(cli, server):
    added = cli.json('users', 'add', 'alice', '--name', 'Alice Example')
    assert added['user']['login'] == 'alice' and len(added['temporary_password']) >= 20
    human = cli('users', 'add', 'bob', '--name', 'Bob Example')
    assert human.exit_code == 0 and 'Temporary password (shown only once): ' in human.stdout
    users = cli.json('users', 'list', '--kind', 'user')
    assert {item['login'] for item in users} == {'alice', 'bob'}
    table = cli('users', 'list')
    assert 'Alice Example' in table.stdout and added['user']['id'] not in table.stdout
    with_ids = cli('users', 'list', '--ids')
    assert added['user']['id'] in with_ids.stdout
    assert cli.json('users', 'get', 'alice')['display_name'] == 'Alice Example'
    assert cli.json('users', 'update', 'Alice Example', '--name', 'Alice Smith')['user']['display_name'] == 'Alice Smith'
    reset = cli.json('users', 'reset-password', 'alice')
    assert reset['temporary_password'] != added['temporary_password']
    nothing = cli('users', 'update', 'alice')
    assert nothing.exit_code == 1 and nothing.stderr.startswith('Nothing to change.')

    assert cli.json('integrations', 'add', 'Lab printer')['integration']['kind'] == 'integration'
    assert [item['display_name'] for item in cli.json('integrations', 'list')] == ['Lab printer']

    assert cli.json('groups', 'add', 'Front desk', '--description', 'Reception staff')['group']['name'] == 'Front desk'
    cli.json('groups', 'update', 'Front desk', '--description', 'Reception')
    cli.json('groups', 'members', 'add', 'Front desk', 'alice')
    group = cli.json('groups', 'get', 'Front desk')
    assert [member['display_name'] for member in group['members']] == ['Alice Smith']
    cli.json('groups', 'members', 'remove', 'Front desk', 'alice')
    assert cli.json('groups', 'get', 'Front desk')['members'] == []
    assert any(item['name'] == 'Front desk' for item in cli.json('groups', 'list'))

    catalogue = cli.json('roles', 'permissions')
    assert any(item['permission'] == 'fax:send' for item in catalogue)
    role = cli.json('roles', 'add', 'Senders', '-p', 'fax:send', '-p', 'fax:read')['role']
    assert role['permissions'] == ['fax:read', 'fax:send']
    updated = cli.json('roles', 'update', 'Senders', '--add', 'fax:document', '--remove', 'fax:read')['role']
    assert updated['permissions'] == ['fax:document', 'fax:send']
    assert cli.json('roles', 'get', 'senders')['name'] == 'Senders'
    assert any(item['name'] == 'Owner' for item in cli.json('roles', 'list'))

    cli.json('mailboxes', 'add', 'Billing')
    cli.json('mailboxes', 'update', 'Billing', '--name', 'Billing office')
    assert [item['label'] for item in cli.json('mailboxes', 'list')] == ['Billing office']
    cli.json('numbers', 'add', '+15550100002', '--mailbox', 'Billing office')
    cli.json('numbers', 'update', '+1 555 010 0002', '--number', '+15550100003')
    assert [item['to_number'] for item in cli.json('numbers', 'list')] == ['+15550100003']

    granted = cli.json('access', 'grant', 'group:Front desk', 'Senders', '--on', 'mailbox:Billing office')
    assert granted['assignment']['resource']['name'] == 'Billing office'
    listing = cli.json('access', 'list', '--who', 'Front desk')
    assert [(item['role']['name'], item['resource']['name']) for item in listing] == [('Senders', 'Billing office')]
    cli.json('access', 'revoke', 'Front desk', 'Senders', '--on', 'mailbox:Billing office')
    assert cli.json('access', 'list', '--who', 'Front desk') == []
    missing = cli('access', 'revoke', 'Front desk', 'Senders', '--on', 'mailbox:Billing office')
    assert missing.exit_code == 1 and 'does not have Senders' in missing.stderr
    places = cli.json('resources', 'list')
    assert {item['kind'] for item in places} >= {'installation', 'mailbox', 'personal'}

    audit = cli.json('audit', 'list', '--limit', '5')
    assert len(audit) == 5 and audit[0]['operation']

    assert cli.json('sessions', 'list', '--for', 'alice') == []
    login = server.post('/auth/login', json={'login': 'alice', 'password': reset['temporary_password']},
                        headers={'Origin': ORIGIN})
    assert login.status_code == 200, login.text
    server.cookies.clear()
    sessions = cli.json('sessions', 'list', '--for', 'alice')
    assert len(sessions) == 1 and sessions[0]['source_kind'] == 'password'
    assert sessions[0]['session_id'] not in cli('sessions', 'list', '--for', 'alice').stdout
    assert cli.json('sessions', 'revoke', sessions[0]['session_id'])['changed'] is True
    assert cli.json('sessions', 'list', '--for', 'alice')[0]['revoked_at'] is not None


def test_names_from_the_server_are_printed_exactly(cli):
    name = 'Desk [a] [/] x :warning:'
    cli.json('users', 'add', 'desk', '--name', name)
    cli.json('groups', 'add', 'Night [b] shift')
    cli.json('groups', 'members', 'add', 'Night [b] shift', 'desk')
    for args in (('users', 'list'), ('users', 'get', 'desk'), ('groups', 'get', 'Night [b] shift')):
        result = cli(*args)
        assert result.exit_code == 0, (args, result.stderr)
        assert name in result.stdout, args
    assert 'Night [b] shift' in cli('groups', 'list').stdout


def test_management_is_refused_without_permission_and_after_policy_change(cli):
    _, token = restricted_key(cli)
    denied = cli('audit', 'list', key=token)
    assert denied.exit_code == 4 and denied.stderr.strip() == 'This API key is not allowed to do that.'
    refused = cli('groups', 'add', 'Night shift', key=token)
    assert refused.exit_code == 4


def test_a_key_whose_owner_has_a_temporary_password_says_so(cli, tmp_path):
    assert cli('users', 'add', 'sam', '--name', 'Sam Sender').exit_code == 0
    assert cli('access', 'grant', 'sam', 'Fax operator').exit_code == 0
    token = cli.json('keys', 'create', '--for', 'sam', '-p', 'fax:send', '-p', 'fax:read', '--name', 'Sam key')['token']
    sentence = 'The owner of this key must set a new password before it can be used.'
    listed = cli('jobs', 'list', key=token)
    assert listed.exit_code == 4 and listed.stderr.strip() == sentence
    note = tmp_path / 'note.txt'
    note.write_text('Synthetic\n')
    sent = cli('send', '+15551230001', note, '--queue', key=token)
    assert sent.exit_code == 4 and sent.stderr.strip() == sentence
    response = cli.client.get('/admin/fax-jobs', headers={'X-API-Key': token})
    assert response.status_code == 403 and response.json() == {'detail': sentence}


def test_keys_create_rotate_revoke_and_quiet_reveal(cli):
    key_id, token = restricted_key(cli, 'Scanner')
    assert token.startswith('fbk_live_' + key_id)
    assert cli.json('me', key=token)['principal']['display_name'] == 'Scanner'
    assert [item['id'] for item in cli.json('keys', 'list', '--for', 'Scanner')] == [key_id]
    cli.json('keys', 'update', key_id, '--note', 'On the reception desk', '--expires', '2099-01-31')
    rotated = cli('--quiet', 'keys', 'rotate', key_id)
    assert rotated.exit_code == 0
    new_token = rotated.stdout.strip()
    assert new_token.startswith('fbk_live_' + key_id) and new_token != token
    assert cli('me', key=token).exit_code == 3
    assert cli.json('me', key=new_token)['principal']['display_name'] == 'Scanner'
    cli.json('keys', 'revoke', key_id)
    assert cli('me', key=new_token).exit_code == 3
    listed = cli('keys', 'list')
    assert 'revoked' in listed.stdout and new_token not in listed.stdout


def test_owner_enroll_with_the_installation_key(cli, server):
    result = cli('owner', 'enroll', '--login', 'olivia', '--name', 'Olivia Owner')
    assert result.exit_code == 0, result.stderr
    assert 'Temporary password (shown only once): ' in result.stdout and 'faxbot' not in result.stderr
    password = result.stdout.split('Temporary password (shown only once): ')[1].split()[0]
    login = server.post('/auth/login', json={'login': 'olivia', 'password': password}, headers={'Origin': ORIGIN})
    assert login.status_code == 200 and login.json()['password_change_required'] is True
    server.cookies.clear()


# -- settings, providers, diagnostics, pairing ---------------------------------------------

def test_settings_get_set_validate_export(cli, monkeypatch):
    current = cli.json('settings', 'get')
    assert current['backend']['type'] == 'phaxio'
    limits = cli.json('settings', 'get', 'limits')
    assert set(limits) == {'limits'}
    assert cli('settings', 'get', 'nonsense').exit_code == 5
    saved = cli.json('settings', 'set', 'max_file_size_mb=7')
    assert saved['ok'] is True and saved['changed'] is True
    assert cli.json('settings', 'get', 'limits')['limits']['max_file_size_mb'] == 7
    unchanged = cli('settings', 'set', 'max_file_size_mb=7')
    assert unchanged.exit_code == 0 and 'Nothing changed' in unchanged.stdout
    rejected = cli('settings', 'set', 'max_file_size_mb=lots')
    assert rejected.exit_code == 9 and 'max_file_size_mb' in rejected.stderr
    exported = cli('settings', 'export')
    assert exported.exit_code == 0 and 'MAX_FILE_SIZE_MB=7' in exported.stdout and BOOTSTRAP not in exported.stdout
    monkeypatch.setenv('SINCH_PROJECT_ID', 'synthetic-project')
    validated = cli.json('settings', 'validate', 'sinch')
    assert validated['checks']['auth'] is False


def test_providers_diagnostics_and_status(cli):
    providers = cli.json('providers', 'list')
    assert providers['outbound'] == 'phaxio'
    status = cli.json('providers', 'status')
    assert status['backend'] == 'phaxio' and 'jobs' in status
    diagnostics = cli.json('diagnostics', 'run')
    assert 'summary' in diagnostics
    human = cli('diagnostics', 'run')
    assert human.exit_code == 0 and 'Healthy' in human.stdout


def test_pairing_a_device_and_reusing_the_code(cli):
    issued = cli.json('pair', 'new')
    assert len(issued['code']) == 6 and issued['code'].isdigit()
    paired = cli.json('pair', 'device', issued['code'], '--device-name', 'Test phone', key=None)
    assert paired['token'].startswith('fbk_live_')
    assert cli.json('me', key=paired['token'])['principal']['display_name'] == 'Device: Test phone'
    again = cli('pair', 'device', issued['code'], key=None)
    assert again.exit_code == 4
    assert again.stderr.strip() == 'This pairing code did not work. Create a new code in the console and try again.'
    saved = cli.json('pair', 'device', cli.json('pair', 'new')['code'], '--save-profile', 'phone', key=None)
    assert saved['saved_profile'] == 'phone' and 'token' not in saved
    as_phone = cli('--profile', 'phone', '--json', 'me', key=None, url=None)
    assert as_phone.exit_code == 0 and json.loads(as_phone.stdout)['principal']['kind'] == 'integration'


def test_logs_tunnel_actions_restart_and_database(cli):
    logs = cli.json('logs', 'list', '--limit', '5')
    assert 'items' in logs and logs['count'] <= 5
    tail = cli('logs', 'tail')
    assert tail.exit_code == 9 and tail.stderr.strip() == ('The server does not keep an activity log file. Set '
                                                           'AUDIT_LOG_FILE to keep one.')
    assert cli.json('tunnel', 'status')['enabled'] is False
    assert cli.json('actions', 'list') == {'enabled': False, 'items': []}
    unconfirmed = cli('--json', 'restart')
    assert unconfirmed.exit_code == 1 and 'Add --yes' in json.loads(unconfirmed.stdout)['error']['message']
    refused = cli('restart', '--yes')
    assert refused.exit_code == 4 and 'ADMIN_ALLOW_RESTART' in refused.stderr
    database = cli.json('diagnostics', 'database')
    assert database['engine'] == 'sqlite' and database['connected'] is True
    callbacks = cli.json('providers', 'callbacks')
    assert callbacks['backend'] == 'phaxio' and callbacks['callbacks'][0]['url'].endswith('/phaxio-inbound')
    turned_off = cli('providers', 'config', 'phaxio')
    assert turned_off.exit_code == 5
    assert turned_off.stderr.strip() == 'Provider plugins are turned off on this installation.'


# -- global options after the subcommand ------------------------------------------------------

def test_global_options_are_accepted_before_or_after_the_subcommand(cli, tmp_path):
    inbound_id = cli.json('inbound', 'simulate', '--from', '+15559990000', '--to', '+15551112222')['id']
    before = cli.runner.invoke(cli_app, ['--url', ORIGIN, '--key', BOOTSTRAP, '--json', 'inbound', 'list'],
                               obj={'client_factory': lambda address, timeout: (cli.client, False)})
    after = cli('inbound', 'list', '--json', '--url', ORIGIN, '--key', BOOTSTRAP, url=None, key=None)
    equals = cli('inbound', 'list', '--json', f'--url={ORIGIN}', f'--key={BOOTSTRAP}', url=None, key=None)
    for result in (before, after, equals):
        assert result.exit_code == 0, result.stderr
        assert [item['id'] for item in json.loads(result.stdout)] == [inbound_id]

    # --quiet / -q after the subcommand prints only the secret shown once.
    key_id, token = restricted_key(cli, 'Scanner')
    for flag in ('--quiet', '-q'):
        rotated = cli('keys', 'rotate', key_id, flag)
        assert rotated.exit_code == 0 and rotated.stdout.strip().startswith('fbk_live_' + key_id)
        assert len(rotated.stdout.strip().splitlines()) == 1

    # --profile after the subcommand selects the saved profile.
    saved = cli('config', 'set-profile', 'clinic', '--key-stdin', '--no-use', input=BOOTSTRAP + '\n', key=None)
    assert saved.exit_code == 0, saved.stderr
    me = cli('me', '--profile', 'clinic', '--json', url=None, key=None)
    assert me.exit_code == 0, me.stderr
    assert json.loads(me.stdout)['principal']['kind'] == 'bootstrap'

    # An option the subcommand defines itself stays with it: set-profile --url is the profile's address.
    remote = cli('config', 'set-profile', 'remote', '--url', 'https://fax.example.test', '--no-key', '--no-use',
                 '--json', key=None)
    assert remote.exit_code == 0, remote.stderr
    assert json.loads(remote.stdout)['url'] == 'https://fax.example.test'

    # Option values are never taken for global options, and --help still works after a subcommand.
    noted = cli('keys', 'update', key_id, '--note', '--json')
    assert noted.exit_code == 0 and not noted.stdout.lstrip().startswith('{')
    assert [item['note'] for item in cli.json('keys', 'list') if item['id'] == key_id] == ['--json']
    helped = cli('inbound', 'list', '--help', url=None, key=None)
    assert helped.exit_code == 0 and 'Usage' in helped.stdout


def test_global_options_are_moved_only_from_after_the_subcommand():
    import typer
    from typer._click import Context
    from app.cli.main import hoist_global_options
    root = typer.main.get_command(cli_app)
    context = Context(root)
    cases = {
        ('inbound', 'list', '--json'): ['--json', 'inbound', 'list'],
        ('--url', 'A', 'jobs', 'list', '--url=B', '-q'): ['--url', 'A', '--url=B', '-q', 'jobs', 'list'],
        ('me', '--profile', 'p', '--key', 'K'): ['--profile', 'p', '--key', 'K', 'me'],
        ('admin', '--data-dir', 'D', 'status', '--json'): ['--json', 'admin', '--data-dir', 'D', 'status'],
        ('config', 'set-profile', 'x', '--url', 'U', '--json'): ['--json', 'config', 'set-profile', 'x', '--url', 'U'],
        ('keys', 'update', 'k', '--note', '--json'): ['keys', 'update', 'k', '--note', '--json'],
        ('send', '+15551230001', '--', '--json'): ['send', '+15551230001', '--', '--json'],
        ('jobs', 'list', '--help'): ['jobs', 'list', '--help'],
        ('--json', 'me'): ['--json', 'me'],
    }
    for given, expected in cases.items():
        assert hoist_global_options(root, context, list(given)) == expected, given


# -- commands that change the installation or its host ----------------------------------------

class Recorder:
    """An HTTP client that records each request and answers from a table, for host-side commands."""

    def __init__(self, answers):
        import httpx
        self.requests = []
        self.answers = answers

        def handle(request):
            body = json.loads(request.content) if request.content else None
            self.requests.append((request.method, request.url.path, body, request.headers.get('X-API-Key')))
            status, payload = self.answers[(request.method, request.url.path)]
            return httpx.Response(status, json=payload)
        self.client = httpx.Client(base_url=ORIGIN, transport=httpx.MockTransport(handle))

    def __call__(self, *args, input=None):
        return CliRunner().invoke(cli_app, ['--url', ORIGIN, '--key', BOOTSTRAP, *[str(arg) for arg in args]],
                                  input=input, obj={'client_factory': lambda address, timeout: (self.client, False)})


def test_keys_approve_turns_a_key_waiting_for_review_into_a_working_key(cli):
    import sqlalchemy as sa
    key_id, token = restricted_key(cli, 'Old script', permissions=('fax:read',))
    store = main_module.app.state.access_runtime.store
    with store.engine.begin() as connection:
        connection.execute(sa.text("UPDATE access_key_bindings SET state = 'pending_review' WHERE id = "
                                   "(SELECT id FROM api_keys WHERE key_id = :key)"), {'key': key_id})
    assert cli('me', key=token).exit_code == 3
    listed = cli('keys', 'list')
    assert 'needs review' in listed.stdout
    approved = cli('keys', 'approve', key_id, '--for', 'Old script', '-p', 'fax:read')
    assert approved.exit_code == 0, approved.stderr
    assert approved.stdout.strip() == 'API key Old script key approved.'
    assert key_id not in approved.stdout
    assert cli.json('me', key=token)['principal']['display_name'] == 'Old script'
    assert [item.get('pending_review') for item in cli.json('keys', 'list') if item['id'] == key_id] == [False]


def test_jobs_reconcile_records_the_provider_id_without_sending():
    recorder = Recorder({
        ('GET', '/admin/fax-jobs/job-1'): (200, {'id': 'job-1', 'delivery_version': 4}),
        ('POST', '/admin/fax-jobs/job-1/reconcile'): (200, {'id': 'job-1', 'state': 'submitted'}),
    })
    result = recorder('jobs', 'reconcile', 'job-1', '--provider-fax-id', 'PX-881', '--confirm-original-account')
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == 'Provider fax ID recorded. Faxbot will follow this fax with the provider.'
    assert recorder.requests == [
        ('GET', '/admin/fax-jobs/job-1', None, BOOTSTRAP),
        ('POST', '/admin/fax-jobs/job-1/reconcile',
         {'expected_version': 4, 'provider_sid': 'PX-881', 'confirm_original_account': True}, BOOTSTRAP)]
    assert not any(path == '/fax' for _, path, _, _ in recorder.requests)


def test_tunnel_test_and_restart_send_one_request_and_print_one_sentence():
    recorder = Recorder({
        ('POST', '/admin/tunnel/test'): (200, {'ok': True, 'message': 'OK', 'target': 'fax.example.test:443/health'}),
        ('POST', '/admin/restart'): (200, {'ok': True}),
    })
    tested = recorder('tunnel', 'test')
    assert tested.exit_code == 0 and tested.stdout.strip() == 'Reachable at fax.example.test:443/health: OK'
    restarted = recorder('restart', '--yes')
    assert restarted.exit_code == 0
    assert restarted.stdout.strip() == 'Faxbot is restarting. Its service manager starts it again.'
    declined = recorder('restart', input='n\n')
    assert declined.exit_code == 1
    assert [(method, path) for method, path, _, _ in recorder.requests] == [
        ('POST', '/admin/tunnel/test'), ('POST', '/admin/restart')]


def test_actions_run_executes_an_approved_action(cli, monkeypatch):
    monkeypatch.setenv('ENABLE_ADMIN_EXEC', 'true')
    assert 'python_version' in [item['id'] for item in cli.json('actions', 'list')['items']]
    ran = cli.json('actions', 'run', 'python_version')
    assert ran['ok'] is True and ran['code'] == 0 and sys.version.split()[0] in ran['stdout']
    human = cli('actions', 'run', 'python_version')
    assert human.exit_code == 0 and human.stdout.startswith('Finished. (exit code 0)')
    unknown = cli('actions', 'run', 'rm_everything')
    assert unknown.exit_code == 5


def test_settings_persist_writes_the_private_recovery_file(monkeypatch, tmp_path):
    target = tmp_path / 'recovery' / 'faxbot.env'
    for client in _serve(monkeypatch, tmp_path, PERSISTED_ENV_PATH=str(target)):
        cli = Cli(client)
        result = cli('settings', 'persist')
        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip() == f'Settings written to {target} on the server.'
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        written = target.read_text()
        assert 'MAX_FILE_SIZE_MB=' in written and f'API_KEY={BOOTSTRAP}' in written
        assert BOOTSTRAP not in result.stdout + result.stderr
        _, token = restricted_key(cli, 'Helper', role='Administrator', permissions=('settings:read',))
        assert cli('settings', 'persist', key=token).exit_code == 4


def test_providers_validate_and_install_an_http_manifest(plugins_cli, tmp_path):
    cli = plugins_cli
    manifest = tmp_path / 'provider.json'
    manifest.write_text(json.dumps({
        'id': 'synthetic-http', 'name': 'Synthetic HTTP provider', 'allowed_domains': ['synthetic.invalid'],
        'actions': {'send_fax': {'url': 'https://synthetic.invalid/send'},
                    'get_status': {'url': 'https://synthetic.invalid/status'}}}))
    checked = cli('providers', 'validate', manifest)
    assert checked.exit_code == 0, checked.stderr
    assert checked.stdout.strip() == 'The manifest is valid.'
    installed = cli('providers', 'install', manifest)
    assert installed.exit_code == 0, installed.stderr
    assert installed.stdout.strip() == ('Provider synthetic-http installed. Configure it with faxbot providers '
                                        'configure synthetic-http.')
    assert any(item['id'] == 'synthetic-http' for item in cli.json('providers', 'list'))
    broken = tmp_path / 'broken.json'
    broken.write_text('{not json')
    refused = cli('providers', 'install', broken)
    assert refused.exit_code == 1 and refused.stderr.strip() == f'{broken} is not valid JSON.'


def test_provider_plugins_list_config_and_configure(plugins_cli):
    cli = plugins_cli
    providers = cli.json('providers', 'list')
    assert any(item['id'] == 'phaxio' and item['enabled'] for item in providers)
    shown = cli.json('providers', 'config', 'phaxio')
    assert shown['enabled'] is True and set(shown['settings']) >= {'api_key', 'callback_url'}
    saved = cli('providers', 'configure', 'phaxio', '--secret', 'api_key', input='synthetic-phaxio-key\n' * 2)
    assert saved.exit_code == 0, saved.stderr
    assert 'synthetic-phaxio-key' not in saved.stdout
    masked = cli.json('providers', 'config', 'phaxio')['settings']['api_key']
    assert masked and 'synthetic-phaxio-key' not in masked
    assert isinstance(cli.json('providers', 'registry'), dict)


# -- routing, intake, direct delivery, cases ------------------------------------------------

def test_routing_destinations_costs_and_rate_cards(cli, tmp_path):
    assert cli.json('routing', 'destinations')['destinations'] == []
    updated = cli.json('routing', 'update-destination', '+15551230001', '--name', 'County records',
                       '--accepts-references')
    assert updated['display_name'] == 'County records' and updated['accepts_references'] is True
    view = cli.json('routing', 'destination', '+15551230001')
    assert view['display_name'] == 'County records'
    assert cli('routing', 'destination', '+15551230001').exit_code == 0
    assert 'providers' in cli.json('routing', 'costs')
    cards = tmp_path / 'cards.json'
    cards.write_text(json.dumps({'cards': [{'provider_id': 'phaxio', 'label': 'Phaxio list price',
                                            'per_page': '0.07', 'captured_on': '2026-10-01T00:00:00'}]}))
    replaced = cli.json('routing', 'rate-cards', '--replace', cards)
    assert [card['per_page'] for card in replaced['cards']] == ['0.07']
    assert cli.json('routing', 'rate-cards')['cards'][0]['label'] == 'Phaxio list price'
    bad = cli('routing', 'destination', 'not-a-number')
    assert bad.exit_code == 9


def test_intake_connectors_items_and_test_email(cli):
    added = cli.json('intake', 'connectors', 'add', 'Front desk email', '--host', '127.0.0.1', '--port', '9',
                     '--security', 'none', '--from', 'fax@clinic.example', '--to', 'desk@clinic.example')
    assert added['has_password'] is False
    assert [item['name'] for item in cli.json('intake', 'connectors', 'list')] == ['Front desk email']
    tested = cli('--json', 'intake', 'connectors', 'test', 'Front desk email')
    assert tested.exit_code == 1 and json.loads(tested.stdout)['ok'] is False
    assert cli.json('intake', 'items')['items'] == []
    updated = cli.json('intake', 'connectors', 'update', 'Front desk email', '--name', 'Desk email', '--disable')
    assert updated['name'] == 'Desk email' and updated['enabled'] is False and updated['version'] == 2
    cli.json('intake', 'connectors', 'update', 'Desk email', '--name', 'Front desk email', '--enable')
    assert cli('intake', 'retry', 'missing-item').exit_code in (5, 6, 9)
    cli.json('intake', 'connectors', 'remove', 'Front desk email')
    assert cli.json('intake', 'connectors', 'list') == []


def test_only_owners_allow_direct_partners_on_private_networks(cli, tmp_path):
    from app.direct.crypto import Identity, card
    saved = cli.json('settings', 'set', 'direct_allow_private_peers=false')
    assert saved['changed'] is True
    assert cli.json('settings', 'get', 'direct')['direct']['allow_private_peers'] is False
    local = tmp_path / 'local.json'
    local.write_text(json.dumps(card(Identity.generate(), organization='Basement server', fax_number='+15550007778',
                                     endpoint='https://127.0.0.1:8443')))
    refused = cli('direct', 'peers', 'add', local)
    assert refused.exit_code == 6
    assert refused.stderr.strip() == ("The partner's address points to a private or local network, which direct "
                                      'delivery refuses unless DIRECT_ALLOW_PRIVATE_PEERS is turned on.')
    _, token = restricted_key(cli, 'Settings helper', role='Administrator', permissions=('settings:read', 'settings:write'))
    assert cli('settings', 'set', 'direct_allow_private_peers=true', key=token).exit_code == 4
    cli.json('settings', 'set', 'direct_allow_private_peers=true')
    assert cli.json('direct', 'peers', 'add', local)['endpoint'] == 'https://127.0.0.1:8443'


def test_direct_card_peers_challenge_and_confirm(cli, tmp_path):
    from app.direct.crypto import Identity, card

    class Partner:
        async def request(self, method, url, **kwargs):
            assert url == 'https://valley.example/direct/verifications'
            return 200, {'verified': True}

    own = cli.json('direct', 'card')['card']
    assert own['organization'] == 'County Clinic'
    partner = tmp_path / 'valley.json'
    partner.write_text(json.dumps(card(Identity.generate(), organization='Valley Hospital',
                                       fax_number='+15550007777', endpoint='https://valley.example')))
    enrolled = cli.json('direct', 'peers', 'add', partner)
    assert enrolled['state'] == 'pending'
    assert [item['organization'] for item in cli.json('direct', 'peers', 'list')] == ['Valley Hospital']
    challenged = cli.json('direct', 'peers', 'challenge', 'Valley Hospital')
    assert challenged['code_sent'] is True and challenged['fax_id']
    main_module.app.state.direct_http = Partner()
    confirmed = cli.json('direct', 'peers', 'confirm', '+1 555 000 7777', '1234 5678')
    assert confirmed == {'confirmed': True, 'detail': 'The partner confirmed the code.'}
    assert cli.json('direct', 'deliveries') == []
    revoked = cli.json('direct', 'peers', 'revoke', 'Valley Hospital')
    assert revoked['state'] == 'revoked'


def test_case_packet_preview_send_and_documents(cli, tmp_path):
    first, second = pdf(tmp_path / 'referral.pdf', 'Referral'), pdf(tmp_path / 'labs.pdf', 'Labs')
    preview = cli.json('cases', 'send', 'CASE-1', '+15551230009', first, second, '--title', 'Referral',
                       '--title', 'Lab results', '--preview')
    assert preview['fax_id'] is None and [item['title'] for item in preview['documents']] == ['Referral', 'Lab results']
    sent = cli.json('cases', 'send', 'CASE-1', '+15551230009', first)
    assert sent['fax_id']
    documents = cli.json('cases', 'documents', 'CASE-1', '--to', '+15551230009')
    assert [item['title'] for item in documents['documents']] == ['referral']
    human = cli('cases', 'documents', 'CASE-1', '--to', '+15551230009')
    assert human.exit_code == 0 and 'referral' in human.stdout


# -- profiles --------------------------------------------------------------------------------

def test_profiles_keep_the_key_private_and_are_used_by_default(cli, tmp_path):
    saved = cli('config', 'set-profile', 'clinic', '--key-stdin', input=BOOTSTRAP + '\n', key=None)
    assert saved.exit_code == 0, saved.stderr
    path = tmp_path / 'cli-config' / 'config.toml'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    shown = cli('config', 'show', key=None)
    assert 'clinic' in shown.stdout and BOOTSTRAP not in shown.stdout
    me = cli('--json', 'me', key=None, url=None)
    assert me.exit_code == 0 and json.loads(me.stdout)['principal']['kind'] == 'bootstrap'
    prompted = cli('config', 'set-profile', 'other', '--no-use', input='\n', key=None)
    assert prompted.exit_code == 0 and BOOTSTRAP not in prompted.stdout
    assert cli.json('config', 'show', key=None)['profiles'][1] == {'profile': 'other', 'url': ORIGIN,
                                                                   'key_saved': False, 'default': False}
    cli.json('config', 'remove', 'other', key=None)
    elsewhere = cli('me', key=None, url='https://elsewhere.example')
    assert elsewhere.exit_code == 3 and elsewhere.stderr.startswith('No API key.')
    trailing = cli('--json', 'me', key=None, url=ORIGIN + '/')
    assert trailing.exit_code == 0
    unknown = cli('--profile', 'missing', 'me', key=None)
    assert unknown.exit_code == 1 and "no saved profile named 'missing'" in unknown.stderr


# -- generated reference ----------------------------------------------------------------------

def test_committed_command_reference_is_current():
    environment = {key: value for key, value in os.environ.items() if not key.startswith('FAXBOT_')}
    environment.update({'COLUMNS': '100', 'TERM': 'dumb', 'NO_COLOR': '1'})
    generated = subprocess.run([sys.executable, '-m', 'app.cli.reference'], cwd=API_ROOT, env=environment,
                               capture_output=True, text=True, timeout=60, check=True).stdout
    committed = (REPO_ROOT / 'docs/reference/cli.md').read_text(encoding='utf-8')
    assert generated == committed, 'Run make cli-docs and commit docs/reference/cli.md.'
