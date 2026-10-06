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
        # The per-minute inbound buckets are process-wide, so earlier test files could use them up.
        'INBOUND_LIST_RPM': '0',
        'INBOUND_GET_RPM': '0',
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
    assert cli('access', 'integrations', 'add', name).exit_code == 0
    assert cli('access', 'grants', 'add', name, role).exit_code == 0
    created = cli.json('access', 'keys', 'create', '--for', name, *[part for p in permissions for part in ('-p', p)],
                       '--name', name + ' key')
    return created['key']['id'], created['token']


# -- global behaviour ------------------------------------------------------------------

def _commands(typer_app, prefix=(), hidden=False):
    """{path: (callback, hidden)} for every command under a Typer app, hidden ones included."""
    found = {}
    for info in typer_app.registered_commands:
        found[(*prefix, info.name)] = (info.callback, hidden or info.hidden)
    for info in typer_app.registered_groups:
        found.update(_commands(info.typer_instance, (*prefix, info.name), hidden or info.hidden))
    return found


def _original(callback):
    return getattr(callback, '__wrapped__', callback)


def _plain(text):
    # Hosted CI forces colour, so compare the help text without escape codes.
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def test_help_lists_send_status_and_the_eight_areas_without_starting_the_server():
    import typer.main
    from app.cli.nouns import NOUNS
    assert NOUNS == ('received', 'sent', 'numbers', 'recipients', 'providers', 'costs', 'access', 'system')
    root = typer.main.get_command(cli_app)
    assert [name for name, command in root.commands.items() if not command.hidden] == ['send', 'status', *NOUNS]
    result = CliRunner().invoke(cli_app, ['--help'], env={'COLUMNS': '200'})
    plain = _plain(result.stdout)
    assert result.exit_code == 0 and ' received ' in plain and ' system ' in plain
    assert ' jobs ' not in plain and ' admin ' not in plain and ' config ' not in plain
    for noun in NOUNS:
        shown = CliRunner().invoke(cli_app, [noun, '--help'], env={'COLUMNS': '200'})
        assert shown.exit_code == 0 and 'Usage: faxbot ' + noun in _plain(shown.stdout)


def test_every_command_is_shown_in_help_and_the_older_names_are_gone():
    commands = _commands(cli_app)
    assert [path for path, (_, hidden) in commands.items() if hidden] == []
    # Names from before the console's eight areas, removed before the first tagged release that had them.
    for older in (('jobs', 'list'), ('inbound', 'list'), ('settings', 'get'), ('admin', 'migrate'), ('health',),
                  ('me',), ('tunnel', 'status'), ('actions', 'list'), ('sent', 'history'), ('sent', 'reconcile'),
                  ('providers', 'config'), ('providers', 'registry'), ('access', 'grant')):
        typed = CliRunner().invoke(cli_app, [*older, '--help'], env={'COLUMNS': '200'})
        assert typed.exit_code == 2, older
        assert older not in commands


def test_me_health_and_errors_map_to_plain_sentences(cli):
    me = cli.json('access', 'me')
    assert me['principal']['kind'] == 'bootstrap' and 'owner:recover' in me['permissions']
    human = cli('access', 'me')
    assert human.exit_code == 0 and 'installation key' in human.stdout
    health = cli('--json', 'system', 'health', key=None)
    assert json.loads(health.stdout)['live'] == {'status': 'ok'}
    assert health.exit_code in (0, 1)

    missing = cli('access', 'me', key=None)
    assert missing.exit_code == 3 and missing.stderr.startswith('No API key.')
    wrong = cli('access', 'me', key='fbk_live_000000000000_synthetic')
    assert wrong.exit_code == 3
    assert wrong.stderr.strip() == ('Faxbot did not accept the API key. Check --key, FAXBOT_API_KEY or your saved '
                                    'profile.')
    unknown = cli('status', '0' * 32)
    assert unknown.exit_code == 5 and unknown.stderr.strip() == 'Fax not found.'
    as_json = cli('--json', 'status', '0' * 32)
    assert as_json.exit_code == 5
    assert json.loads(as_json.stdout)['error'] == {'message': 'Fax not found.', 'exit_code': 5, 'status': 404,
                                                   'detail': 'Fax not found.'}
    unreachable = CliRunner().invoke(cli_app, ['--url', 'http://127.0.0.1:9', '--key', 'x', 'access', 'me'])
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
    # Faxbot picks each fax's route when it sends it: at accept time only the planned route, never the
    # provider setting the fax was accepted under.
    assert 'Provider' not in human.stdout.replace('Provider fax ID', '') and 'Planned route' in human.stdout

    assert cli.json('status', sent['id'])['id'] == sent['id']
    listing = cli.json('sent', 'list')
    assert listing['total'] == 2 and all(job['to_number'].startswith('*') for job in listing['jobs'])
    assert set(listing['costs']) <= {job['id'] for job in listing['jobs']}
    assert 'Cost' in cli('sent', 'list').stdout
    # The console's words for each state, never the raw value.
    table = cli('sent', 'list').stdout
    assert 'Held test fax' in table and ' held ' not in table and 'queued' not in table
    shown = cli('status', sent['id']).stdout
    assert 'Held test fax' in shown and 'This test fax is held and will not be sent.' in shown
    assert 'Provider' not in shown.replace('Provider fax ID', '')  # no route assigned to a held fax
    table = cli('sent', 'list')
    assert table.exit_code == 0 and sent['id'] not in table.stdout and '+15551230001' not in table.stdout
    assert sent['id'] in cli('sent', 'list', '--ids').stdout
    assert cli.json('sent', 'show', sent['id'])['id'] == sent['id']
    history = cli.json('sent', 'evidence', sent['id'])
    assert history['state'] == 'held'

    target = tmp_path / 'downloaded.pdf'
    saved = cli.json('sent', 'pdf', sent['id'], '-o', target)
    assert saved['saved_to'] == str(target) and target.read_bytes().startswith(b'%PDF')
    refused = cli('sent', 'pdf', sent['id'], '-o', target)
    assert refused.exit_code == 1 and 'already exists' in refused.stderr
    unconfirmed = cli('sent', 'confirm-receipt', sent['id'], '--provider-fax-id', 'abc123')
    assert unconfirmed.exit_code == 1 and '--confirm-original-account' in unconfirmed.stderr


def test_sending_is_refused_for_a_key_without_permission(cli, tmp_path):
    note = tmp_path / 'note.txt'
    note.write_text('Synthetic\n')
    _, token = restricted_key(cli, 'Viewer', role='Auditor', permissions=('fax:read',))
    denied = cli('send', '+15551230001', note, '--queue', key=token)
    assert denied.exit_code == 4
    assert denied.stderr.strip() == 'This API key is not allowed to do that.'


def test_received_faxes_simulate_list_get_and_download(cli, tmp_path):
    inbound_id = cli.json('system', 'diagnostics', 'test-fax', '--from', '+15559990000', '--to', '+15551112222')['id']
    items = cli.json('received', 'list')
    assert [item['id'] for item in items] == [inbound_id]
    assert inbound_id not in cli('received', 'list').stdout and inbound_id in cli('received', 'list', '--ids').stdout
    assert cli.json('received', 'show', inbound_id)['to'] == '+15551112222'
    target = tmp_path / 'received.pdf'
    cli.json('received', 'pdf', inbound_id, '--output', target)
    assert target.read_bytes().startswith(b'%PDF')
    listing = cli('received', 'list').stdout
    assert 'Status' in listing and 'A test fax created in Faxbot.' in listing
    detail = cli('received', 'show', inbound_id).stdout
    digest = cli.json('received', 'show', inbound_id)['sha256']
    assert f'SHA-256 {digest[:12]}…' in detail and digest not in detail
    assert re.search(r'Document\s+1 page, ', detail) and re.search(r'Test fax\s+yes', detail)
    assert 'Provider fax ID' in detail and re.search(r'Sent\s+-', detail)
    received = cli('received', 'fetch', inbound_id)
    assert received.exit_code == 6 and received.stderr.strip() == 'Faxbot has nothing to fetch for this fax.'
    _, token = restricted_key(cli, 'Sender', role='Fax operator', permissions=('fax:send',))
    hidden = cli('received', 'show', inbound_id, key=token)
    assert hidden.exit_code == 5


# -- access management -----------------------------------------------------------------

def test_users_groups_roles_access_mailboxes_numbers_audit_and_sessions(cli, server):
    added = cli.json('access', 'users', 'add', 'alice', '--name', 'Alice Example')
    assert added['user']['login'] == 'alice' and len(added['temporary_password']) >= 20
    human = cli('access', 'users', 'add', 'bob', '--name', 'Bob Example')
    assert human.exit_code == 0 and 'Temporary password (shown only once): ' in human.stdout
    users = cli.json('access', 'users', 'list', '--kind', 'user')
    assert {item['login'] for item in users} == {'alice', 'bob'}
    table = cli('access', 'users', 'list')
    assert 'Alice Example' in table.stdout and added['user']['id'] not in table.stdout
    with_ids = cli('access', 'users', 'list', '--ids')
    assert added['user']['id'] in with_ids.stdout
    assert cli.json('access', 'users', 'show', 'alice')['display_name'] == 'Alice Example'
    assert cli.json('access', 'users', 'update', 'Alice Example', '--name', 'Alice Smith')['user']['display_name'] == 'Alice Smith'
    reset = cli.json('access', 'users', 'reset-password', 'alice')
    assert reset['temporary_password'] != added['temporary_password']
    nothing = cli('access', 'users', 'update', 'alice')
    assert nothing.exit_code == 1 and nothing.stderr.startswith('Nothing to change.')

    assert cli.json('access', 'integrations', 'add', 'Lab printer')['integration']['kind'] == 'integration'
    assert [item['display_name'] for item in cli.json('access', 'integrations', 'list')] == ['Lab printer']

    assert cli.json('access', 'groups', 'add', 'Front desk', '--description', 'Reception staff')['group']['name'] == 'Front desk'
    cli.json('access', 'groups', 'update', 'Front desk', '--description', 'Reception')
    cli.json('access', 'groups', 'members', 'add', 'Front desk', 'alice')
    group = cli.json('access', 'groups', 'show', 'Front desk')
    assert [member['display_name'] for member in group['members']] == ['Alice Smith']
    cli.json('access', 'groups', 'members', 'remove', 'Front desk', 'alice')
    assert cli.json('access', 'groups', 'show', 'Front desk')['members'] == []
    assert any(item['name'] == 'Front desk' for item in cli.json('access', 'groups', 'list'))

    catalogue = cli.json('access', 'roles', 'permissions')
    assert any(item['permission'] == 'fax:send' for item in catalogue)
    role = cli.json('access', 'roles', 'add', 'Senders', '-p', 'fax:send', '-p', 'fax:read')['role']
    assert role['permissions'] == ['fax:read', 'fax:send']
    updated = cli.json('access', 'roles', 'update', 'Senders', '--add', 'fax:document', '--remove', 'fax:read')['role']
    assert updated['permissions'] == ['fax:document', 'fax:send']
    assert cli.json('access', 'roles', 'show', 'senders')['name'] == 'Senders'
    assert any(item['name'] == 'Owner' for item in cli.json('access', 'roles', 'list'))

    cli.json('numbers', 'mailboxes', 'add', 'Billing')
    cli.json('numbers', 'mailboxes', 'update', 'Billing', '--name', 'Billing office')
    assert [item['label'] for item in cli.json('numbers', 'mailboxes', 'list')] == ['Billing office']
    cli.json('numbers', 'add', '+15550100002', '--mailbox', 'Billing office')
    cli.json('numbers', 'update', '+1 555 010 0002', '--number', '+15550100003')
    assert [item['number'] for item in cli.json('numbers', 'list')] == ['+15550100003']

    granted = cli.json('access', 'grants', 'add', 'group:Front desk', 'Senders', '--on', 'mailbox:Billing office')
    assert granted['assignment']['resource']['name'] == 'Billing office'
    listing = cli.json('access', 'grants', 'list', '--who', 'Front desk')
    assert [(item['role']['name'], item['resource']['name']) for item in listing] == [('Senders', 'Billing office')]
    cli.json('access', 'grants', 'remove', 'Front desk', 'Senders', '--on', 'mailbox:Billing office')
    assert cli.json('access', 'grants', 'list', '--who', 'Front desk') == []
    missing = cli('access', 'grants', 'remove', 'Front desk', 'Senders', '--on', 'mailbox:Billing office')
    assert missing.exit_code == 1 and 'does not have Senders' in missing.stderr
    places = cli.json('access', 'resources', 'list')
    assert {item['kind'] for item in places} >= {'installation', 'mailbox', 'personal'}

    audit = cli.json('system', 'audit', '--limit', '5')
    assert len(audit) == 5 and audit[0]['operation']

    assert cli.json('access', 'sessions', 'list', '--for', 'alice') == []
    login = server.post('/auth/login', json={'login': 'alice', 'password': reset['temporary_password']},
                        headers={'Origin': ORIGIN})
    assert login.status_code == 200, login.text
    server.cookies.clear()
    sessions = cli.json('access', 'sessions', 'list', '--for', 'alice')
    assert len(sessions) == 1 and sessions[0]['source_kind'] == 'password'
    assert sessions[0]['session_id'] not in cli('access', 'sessions', 'list', '--for', 'alice').stdout
    assert cli.json('access', 'sessions', 'revoke', sessions[0]['session_id'])['changed'] is True
    assert cli.json('access', 'sessions', 'list', '--for', 'alice')[0]['revoked_at'] is not None


def test_names_from_the_server_are_printed_exactly(cli):
    name = 'Desk [a] [/] x :warning:'
    cli.json('access', 'users', 'add', 'desk', '--name', name)
    cli.json('access', 'groups', 'add', 'Night [b] shift')
    cli.json('access', 'groups', 'members', 'add', 'Night [b] shift', 'desk')
    for args in (('access', 'users', 'list'), ('access', 'users', 'show', 'desk'),
                 ('access', 'groups', 'show', 'Night [b] shift')):
        result = cli(*args)
        assert result.exit_code == 0, (args, result.stderr)
        assert name in result.stdout, args
    assert 'Night [b] shift' in cli('access', 'groups', 'list').stdout


def test_management_is_refused_without_permission_and_after_policy_change(cli):
    _, token = restricted_key(cli)
    denied = cli('system', 'audit', key=token)
    assert denied.exit_code == 4 and denied.stderr.strip() == 'This API key is not allowed to do that.'
    refused = cli('access', 'groups', 'add', 'Night shift', key=token)
    assert refused.exit_code == 4


def test_a_key_whose_owner_has_a_temporary_password_says_so(cli, tmp_path):
    assert cli('access', 'users', 'add', 'sam', '--name', 'Sam Sender').exit_code == 0
    assert cli('access', 'grants', 'add', 'sam', 'Fax operator').exit_code == 0
    token = cli.json('access', 'keys', 'create', '--for', 'sam', '-p', 'fax:send', '-p', 'fax:read', '--name', 'Sam key')['token']
    sentence = 'The owner of this key must set a new password before it can be used.'
    listed = cli('sent', 'list', key=token)
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
    assert cli.json('access', 'me', key=token)['principal']['display_name'] == 'Scanner'
    assert [item['id'] for item in cli.json('access', 'keys', 'list', '--for', 'Scanner')] == [key_id]
    cli.json('access', 'keys', 'update', key_id, '--note', 'On the reception desk', '--expires', '2099-01-31')
    rotated = cli('--quiet', 'access', 'keys', 'rotate', key_id)
    assert rotated.exit_code == 0
    new_token = rotated.stdout.strip()
    assert new_token.startswith('fbk_live_' + key_id) and new_token != token
    assert cli('access', 'me', key=token).exit_code == 3
    assert cli.json('access', 'me', key=new_token)['principal']['display_name'] == 'Scanner'
    cli.json('access', 'keys', 'revoke', key_id)
    assert cli('access', 'me', key=new_token).exit_code == 3
    listed = cli('access', 'keys', 'list')
    assert 'revoked' in listed.stdout and new_token not in listed.stdout


def test_owner_enroll_with_the_installation_key(cli, server):
    result = cli('access', 'owner', 'enroll', '--login', 'olivia', '--name', 'Olivia Owner')
    assert result.exit_code == 0, result.stderr
    assert 'Temporary password (shown only once): ' in result.stdout and 'faxbot' not in result.stderr
    password = result.stdout.split('Temporary password (shown only once): ')[1].split()[0]
    login = server.post('/auth/login', json={'login': 'olivia', 'password': password}, headers={'Origin': ORIGIN})
    assert login.status_code == 200 and login.json()['password_change_required'] is True
    server.cookies.clear()


# -- settings, providers, diagnostics, pairing ---------------------------------------------

def test_settings_get_set_validate_export(cli, monkeypatch):
    current = cli.json('system', 'settings', 'get')
    assert current['backend']['type'] == 'phaxio'
    limits = cli.json('system', 'settings', 'get', 'limits')
    assert set(limits) == {'limits'}
    assert cli('system', 'settings', 'get', 'nonsense').exit_code == 5
    saved = cli.json('system', 'settings', 'set', 'max_file_size_mb=7')
    assert saved['ok'] is True and saved['changed'] is True
    assert cli.json('system', 'settings', 'get', 'limits')['limits']['max_file_size_mb'] == 7
    unchanged = cli('system', 'settings', 'set', 'max_file_size_mb=7')
    assert unchanged.exit_code == 0 and 'Nothing changed' in unchanged.stdout
    rejected = cli('system', 'settings', 'set', 'max_file_size_mb=lots')
    assert rejected.exit_code == 9 and 'max_file_size_mb' in rejected.stderr
    # Sinch's user name alone is refused with one sentence; with its password it saves.
    lone = cli('system', 'settings', 'set', 'sinch_inbound_basic_user=synthetic-sinch-webhook')
    assert lone.exit_code != 0
    assert ('Enter the password Sinch sends as well; Faxbot does not accept the user name without it.'
            in ' '.join(lone.stderr.split()))
    assert cli('system', 'settings', 'set', 'sinch_inbound_basic_user=synthetic-sinch-webhook',
               'sinch_inbound_basic_pass=synthetic-webhook-pass').exit_code == 0
    exported = cli('system', 'settings', 'export')
    assert exported.exit_code == 0 and 'MAX_FILE_SIZE_MB=7' in exported.stdout and BOOTSTRAP not in exported.stdout
    monkeypatch.setenv('SINCH_PROJECT_ID', 'synthetic-project')
    validated = cli.json('system', 'settings', 'validate', 'sinch')
    assert validated['checks']['auth'] is False


def test_providers_diagnostics_and_status(cli, monkeypatch):
    from app import diagnostics_report
    monkeypatch.setattr(diagnostics_report, '_last', None)
    providers = cli.json('providers', 'list')
    assert providers['outbound'] == 'phaxio'
    status = cli.json('providers', 'status')
    assert status['backend'] == 'phaxio' and 'jobs' in status
    before = cli('system', 'diagnostics', 'show')
    assert before.exit_code == 0 and 'Diagnostics have not run yet.' in before.stdout
    diagnostics = cli.json('system', 'diagnostics', 'run')
    assert diagnostics['summary'] and diagnostics['checked_at']
    by_id = {item['id']: item for section in diagnostics['sections'] for item in section['checks']}
    assert by_id['sending.provider']['status'] == 'problem'  # the synthetic Phaxio profile has no keys
    human = cli('system', 'diagnostics', 'show')
    assert human.exit_code == 0 and diagnostics['summary'] in human.stdout
    assert 'Not working: Sending account. ' in human.stdout and '(Open Providers in the console.)' in human.stdout


def test_pairing_a_device_and_reusing_the_code(cli):
    issued = cli.json('access', 'pair', 'new')
    assert len(issued['code']) == 6 and issued['code'].isdigit()
    paired = cli.json('access', 'pair', 'device', issued['code'], '--device-name', 'Test phone', key=None)
    assert paired['token'].startswith('fbk_live_')
    assert cli.json('access', 'me', key=paired['token'])['principal']['display_name'] == 'Device: Test phone'
    again = cli('access', 'pair', 'device', issued['code'], key=None)
    assert again.exit_code == 4
    assert again.stderr.strip() == 'This pairing code did not work. Create a new code in the console and try again.'
    saved = cli.json('access', 'pair', 'device', cli.json('access', 'pair', 'new')['code'], '--save-profile', 'phone', key=None)
    assert saved['saved_profile'] == 'phone' and 'token' not in saved
    as_phone = cli('--profile', 'phone', '--json', 'access', 'me', key=None, url=None)
    assert as_phone.exit_code == 0 and json.loads(as_phone.stdout)['principal']['kind'] == 'integration'


def test_logs_restart_and_database(cli):
    logs = cli.json('system', 'logs', 'list', '--limit', '5')
    assert 'items' in logs and logs['count'] <= 5
    tail = cli('system', 'logs', 'tail')
    assert tail.exit_code == 9 and tail.stderr.strip() == ('The server does not keep an activity log file. Set '
                                                           'AUDIT_LOG_FILE to keep one.')
    for gone in (('system', 'tunnel', 'status'), ('system', 'actions', 'list'), ('tunnel', 'status'), ('actions', 'list')):
        assert cli(*gone).exit_code == 2, gone
    unconfirmed = cli('--json', 'system', 'restart')
    assert unconfirmed.exit_code == 1 and 'Add --yes' in json.loads(unconfirmed.stdout)['error']['message']
    refused = cli('system', 'restart', '--yes')
    assert refused.exit_code == 4 and 'ADMIN_ALLOW_RESTART' in refused.stderr
    database = cli.json('system', 'diagnostics', 'database')
    assert database['engine'] == 'sqlite' and database['connected'] is True
    callbacks = cli.json('providers', 'callbacks')
    assert callbacks['backend'] == 'phaxio' and callbacks['callbacks'][0]['url'].endswith('/phaxio-inbound')
    # With provider plugins off, as on a default install, a provider's settings still show.
    assert cli.json('providers', 'show', 'phaxio')['provider'] == 'phaxio'


# -- global options after the subcommand ------------------------------------------------------

def test_global_options_are_accepted_before_or_after_the_subcommand(cli, tmp_path):
    inbound_id = cli.json('system', 'diagnostics', 'test-fax', '--from', '+15559990000', '--to', '+15551112222')['id']
    before = cli.runner.invoke(cli_app, ['--url', ORIGIN, '--key', BOOTSTRAP, '--json', 'received', 'list'],
                               obj={'client_factory': lambda address, timeout: (cli.client, False)})
    after = cli('received', 'list', '--json', '--url', ORIGIN, '--key', BOOTSTRAP, url=None, key=None)
    equals = cli('received', 'list', '--json', f'--url={ORIGIN}', f'--key={BOOTSTRAP}', url=None, key=None)
    for result in (before, after, equals):
        assert result.exit_code == 0, result.stderr
        assert [item['id'] for item in json.loads(result.stdout)] == [inbound_id]

    # --quiet / -q after the subcommand prints only the secret shown once.
    key_id, token = restricted_key(cli, 'Scanner')
    for flag in ('--quiet', '-q'):
        rotated = cli('access', 'keys', 'rotate', key_id, flag)
        assert rotated.exit_code == 0 and rotated.stdout.strip().startswith('fbk_live_' + key_id)
        assert len(rotated.stdout.strip().splitlines()) == 1

    # --profile after the subcommand selects the saved profile.
    saved = cli('system', 'profiles', 'save', 'clinic', '--key-stdin', '--no-use', input=BOOTSTRAP + '\n', key=None)
    assert saved.exit_code == 0, saved.stderr
    me = cli('access', 'me', '--profile', 'clinic', '--json', url=None, key=None)
    assert me.exit_code == 0, me.stderr
    assert json.loads(me.stdout)['principal']['kind'] == 'bootstrap'

    # An option the subcommand defines itself stays with it: set-profile --url is the profile's address.
    remote = cli('system', 'profiles', 'save', 'remote', '--url', 'https://fax.example.test', '--no-key', '--no-use',
                 '--json', key=None)
    assert remote.exit_code == 0, remote.stderr
    assert json.loads(remote.stdout)['url'] == 'https://fax.example.test'

    # Option values are never taken for global options, and --help still works after a subcommand.
    noted = cli('access', 'keys', 'update', key_id, '--note', '--json')
    assert noted.exit_code == 0 and not noted.stdout.lstrip().startswith('{')
    assert [item['note'] for item in cli.json('access', 'keys', 'list') if item['id'] == key_id] == ['--json']
    helped = cli('received', 'list', '--help', url=None, key=None)
    assert helped.exit_code == 0 and 'Usage' in helped.stdout


def test_global_options_are_moved_only_from_after_the_subcommand():
    import typer
    from typer._click import Context
    from app.cli.main import hoist_global_options
    root = typer.main.get_command(cli_app)
    context = Context(root)
    cases = {
        ('received', 'list', '--json'): ['--json', 'received', 'list'],
        ('--url', 'A', 'sent', 'list', '--url=B', '-q'): ['--url', 'A', '--url=B', '-q', 'sent', 'list'],
        ('access', 'me', '--profile', 'p', '--key', 'K'): ['--profile', 'p', '--key', 'K', 'access', 'me'],
        ('system', 'status', '--data-dir', 'D', '--json'): ['--json', 'system', 'status', '--data-dir', 'D'],
        ('system', 'profiles', 'save', 'x', '--url', 'U', '--json'): ['--json', 'system', 'profiles', 'save', 'x', '--url', 'U'],
        ('access', 'keys', 'update', 'k', '--note', '--json'): ['access', 'keys', 'update', 'k', '--note', '--json'],
        ('send', '+15551230001', '--', '--json'): ['send', '+15551230001', '--', '--json'],
        ('sent', 'list', '--help'): ['sent', 'list', '--help'],
        ('--json', 'access', 'me'): ['--json', 'access', 'me'],
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
    assert cli('access', 'me', key=token).exit_code == 3
    listed = cli('access', 'keys', 'list')
    assert 'needs review' in listed.stdout
    approved = cli('access', 'keys', 'approve', key_id, '--for', 'Old script', '-p', 'fax:read')
    assert approved.exit_code == 0, approved.stderr
    assert approved.stdout.strip() == 'API key Old script key approved.'
    assert key_id not in approved.stdout
    assert cli.json('access', 'me', key=token)['principal']['display_name'] == 'Old script'
    assert [item.get('pending_review') for item in cli.json('access', 'keys', 'list') if item['id'] == key_id] == [False]


def test_jobs_reconcile_records_the_provider_id_without_sending():
    recorder = Recorder({
        ('GET', '/admin/fax-jobs/job-1'): (200, {'id': 'job-1', 'delivery_version': 4}),
        ('POST', '/admin/fax-jobs/job-1/reconcile'): (200, {'id': 'job-1', 'state': 'submitted'}),
    })
    result = recorder('sent', 'confirm-receipt', 'job-1', '--provider-fax-id', 'PX-881', '--confirm-original-account')
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == 'Provider fax ID recorded. Faxbot will follow this fax with the provider.'
    assert recorder.requests == [
        ('GET', '/admin/fax-jobs/job-1', None, BOOTSTRAP),
        ('POST', '/admin/fax-jobs/job-1/reconcile',
         {'expected_version': 4, 'provider_sid': 'PX-881', 'confirm_original_account': True}, BOOTSTRAP)]
    assert not any(path == '/fax' for _, path, _, _ in recorder.requests)


def test_restart_sends_one_request_and_prints_one_sentence():
    recorder = Recorder({('POST', '/admin/restart'): (200, {'ok': True})})
    restarted = recorder('system', 'restart', '--yes')
    assert restarted.exit_code == 0
    assert restarted.stdout.strip() == 'Faxbot is restarting. Its service manager starts it again.'
    declined = recorder('system', 'restart', input='n\n')
    assert declined.exit_code == 1
    assert [(method, path) for method, path, _, _ in recorder.requests] == [('POST', '/admin/restart')]


def test_settings_persist_writes_the_private_recovery_file(monkeypatch, tmp_path):
    target = tmp_path / 'recovery' / 'faxbot.env'
    for client in _serve(monkeypatch, tmp_path, PERSISTED_ENV_PATH=str(target)):
        cli = Cli(client)
        result = cli('system', 'settings', 'persist')
        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip().splitlines() == [
            f'Settings written to {target} on the server.',
            "The recovery copy goes away in the next release. To back up everything, run 'faxbot system backup'."]
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        written = target.read_text()
        assert 'MAX_FILE_SIZE_MB=' in written and f'API_KEY={BOOTSTRAP}' in written
        assert BOOTSTRAP not in result.stdout + result.stderr
        _, token = restricted_key(cli, 'Helper', role='Administrator', permissions=('settings:read',))
        assert cli('system', 'settings', 'persist', key=token).exit_code == 4


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


def test_providers_show_and_configure_work_on_a_default_install(cli):
    """They read and change the settings a default install serves; provider plugins stay off."""
    shown = cli.json('providers', 'show', 'phaxio')
    assert shown['provider'] == 'phaxio' and shown['in_use']['outbound'] is True and shown['in_use']['storage'] is False
    assert {'api_key', 'api_secret', 'callback_url'} <= set(shown['settings'])
    human = cli('providers', 'show', 'phaxio')
    assert human.exit_code == 0 and 'Phaxio' in human.stdout and 'sending' in human.stdout
    assert cli.json('providers', 'show', 'phaxio', '--role', 'storage')['in_use'] == {'storage': False}
    saved = cli('providers', 'configure', 'phaxio', '--secret', 'api_key', input='synthetic-phaxio-key\n' * 2)
    assert saved.exit_code == 0, saved.stderr
    assert 'synthetic-phaxio-key' not in saved.stdout
    masked = cli.json('providers', 'show', 'phaxio')['settings']['api_key']
    assert masked and 'synthetic-phaxio-key' not in masked
    address = 'https://fax.example.test/phaxio-callback'
    assert cli('providers', 'configure', 'phaxio', f'callback_url={address}').exit_code == 0
    assert cli.json('providers', 'show', 'phaxio')['settings']['callback_url'] == address
    unknown = cli('providers', 'configure', 'phaxio', 'colour=blue')
    assert unknown.exit_code == 1 and unknown.stderr.strip() == (
        "Phaxio has no setting named 'colour'. Run 'faxbot providers show phaxio' to see them.")
    assert cli('providers', 'configure', 'phaxio', '--enable').exit_code == 1
    assert cli('providers', 'configure', 'phaxio', '--role', 'inbound').exit_code == 1
    chosen = cli('providers', 'configure', 'phaxio', '--enable', '--role', 'inbound')
    assert chosen.exit_code == 0, chosen.stderr
    assert cli.json('providers', 'show', 'phaxio', '--role', 'inbound')['in_use'] == {'inbound': True}
    assert cli.json('system', 'settings', 'get', 'hybrid')['hybrid']['inbound_override'] == 'phaxio'
    # FreeSWITCH, kept one release without a console page, says so where its settings are shown.
    freeswitch = cli('providers', 'show', 'freeswitch')
    assert freeswitch.exit_code == 0 and freeswitch.stdout.strip().endswith(
        'FreeSWITCH is removed in the next release. Choose another provider with the Setup wizard.')
    missing = cli('providers', 'show', 'interfax')
    assert missing.exit_code == 5 and 'faxbot providers list' in missing.stderr
    assert cli.client.get('/plugins', headers={'X-API-Key': BOOTSTRAP}).status_code == 404


def test_providers_import_adds_several_descriptions(plugins_cli, tmp_path):
    cli = plugins_cli
    batch = tmp_path / 'providers.json'
    batch.write_text(json.dumps({'items': [
        {'id': 'synthetic-one', 'name': 'Synthetic one', 'allowed_domains': ['one.invalid'],
         'actions': {'send_fax': {'url': 'https://one.invalid/send'}, 'get_status': {'url': 'https://one.invalid/s'}}},
        {'name': 'No id'}]}))
    added = cli('providers', 'import', batch)
    assert added.exit_code == 0, added.stderr
    assert 'Added the provider Synthetic one.' in added.stdout and 'Could not add one provider: ' in added.stdout
    assert any(item['id'] == 'synthetic-one' for item in cli.json('providers', 'list'))
    markdown = tmp_path / 'providers.md'
    markdown.write_text('Notes\n\n```json\n' + json.dumps(
        {'id': 'synthetic-two', 'name': 'Synthetic two', 'allowed_domains': ['two.invalid'],
         'actions': {'send_fax': {'url': 'https://two.invalid/send'}, 'get_status': {'url': 'https://two.invalid/s'}}})
        + '\n```\n')
    assert [item['id'] for item in cli.json('providers', 'import', markdown)['imported']] == ['synthetic-two']


# -- routing, intake, direct delivery, cases ------------------------------------------------

def test_routing_destinations_costs_and_rate_cards(cli, tmp_path):
    assert cli.json('recipients', 'list')['destinations'] == []
    updated = cli.json('recipients', 'set', '+15551230001', '--name', 'County records',
                       '--accepts-references')
    assert updated['display_name'] == 'County records' and updated['accepts_references'] is True
    view = cli.json('recipients', 'show', '+15551230001')
    assert view['display_name'] == 'County records'
    assert cli('recipients', 'show', '+15551230001').exit_code == 0
    # The console's words: the preferred way to send, and what a case packet sends.
    shown = cli('recipients', 'show', '+15551230001').stdout
    assert re.search(r'Preferred way to send\s+Cheapest reliable', shown) and 'automatic' not in shown
    assert re.search(r'Case packets\s+Takes a one-page list instead', shown) and 'Accepts references' not in shown
    listed = cli('recipients', 'list').stdout
    assert 'Cheapest reliable' in listed and 'Takes a one-page list instead' in listed
    assert 'providers' in cli.json('costs', 'spending')
    cards = tmp_path / 'cards.json'
    cards.write_text(json.dumps({'cards': [{'provider_id': 'phaxio', 'label': 'Phaxio list price',
                                            'per_page': '0.07', 'captured_on': '2026-10-01T00:00:00'}]}))
    replaced = cli.json('costs', 'rate-cards', '--replace', cards)
    assert [card['per_page'] for card in replaced['cards']] == ['0.07']
    assert cli.json('costs', 'rate-cards')['cards'][0]['label'] == 'Phaxio list price'
    # Each route says how it charges and what this fax would cost, for the pages asked.
    three = cli.json('recipients', 'show', '+15551230001', '--pages', '3')['recommended_routes']
    assert three and three[0]['pages'] == 3 and three[0]['rate'] and three[0]['estimated_cost']
    table = cli('recipients', 'show', '+15551230001', '--pages', '3').stdout
    assert 'Estimated cost, 3 pages' in table and 'Rate' in table and ' estimate' in table
    assert 'Estimated cost, 1 page' in cli('recipients', 'show', '+15551230001').stdout
    bad = cli('recipients', 'show', 'not-a-number')
    assert bad.exit_code == 9
    human = cli('costs', 'spending')
    assert human.exit_code == 0 and 'No faxes sent in this period.' in human.stdout
    refused = cli('costs', 'reconcile')
    assert refused.exit_code != 0
    assert 'Faxbot needs a Telnyx API key to read call charges.' in refused.stdout + refused.stderr
    assert cli('costs', 'fax', '0' * 32).exit_code != 0


def test_routing_batching_show_set_off_and_send_now(cli, tmp_path):
    off = cli.json('recipients', 'together', 'show', '+15551230001')
    assert off['enabled'] is False and off['max_wait_minutes'] == 10
    refused = cli('recipients', 'together', 'set', '+15551230001')
    assert refused.exit_code != 0 and 'recipient agreed' in refused.stderr
    on = cli.json('recipients', 'together', 'set', '+15551230001', '--recipient-agreed', '--wait', '5',
                  '--max-pages', '12', '--mixed-senders')
    assert on['enabled'] is True and on['max_wait_minutes'] == 5 and on['max_pages'] == 12 and on['mixed_senders']
    human = cli('recipients', 'together', 'show', '+15551230001')
    assert human.exit_code == 0 and 'Recipient agreement recorded by' in human.stdout
    assert 'No faxes to this number have been sent together' in human.stdout
    gone = cli('recipients', 'together', 'off', '+15551230001')
    assert gone.exit_code == 0 and 'Off: faxes to this number go straight away.' in gone.stdout
    note = tmp_path / 'note.txt'
    note.write_text('Synthetic command line fax\n')
    sent = cli.json('send', '+15551230001', note, '--queue', '--now')
    assert sent['status'] == 'queued'
    waiting = cli('sent', 'send-now', sent['id'])
    assert waiting.exit_code != 0 and 'not waiting' in waiting.stderr


@pytest.fixture
def telnyx_cli(monkeypatch, tmp_path):
    for client in _serve(monkeypatch, tmp_path, TELNYX_API_KEY='KEYsynthetic-cli', SIP_TRUNK_PRESET='telnyx'):
        yield Cli(client)


def test_routing_reconcile_asks_the_carrier_and_costs_show_charges(telnyx_cli, monkeypatch):
    from app.routing import http as routing_http
    from api.tests.test_carrier_charges import FakeTelnyx
    sources = []
    monkeypatch.setattr(routing_http, 'carrier_source', lambda key: sources.append(key()) or FakeTelnyx([]))
    result = telnyx_cli('costs', 'reconcile')
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == 'No calls are waiting for a charge.'
    assert sources == ['KEYsynthetic-cli'] and 'KEYsynthetic-cli' not in result.stdout
    assert telnyx_cli.json('costs', 'reconcile')['checked'] == 0
    costs = telnyx_cli.json('costs', 'spending')
    assert costs['carrier_charges'] == {'carrier': 'Telnyx', 'supported': True, 'readable': True}
    human = telnyx_cli('costs', 'spending')
    assert 'call charges appear once' not in human.stdout
    # A Telnyx record of a call Faxbot never recorded is shown, included in Charged, and in the total.
    from datetime import datetime, timedelta
    import sqlalchemy as sa
    engine = telnyx_cli.client.app.state.configuration_runtime.manager.store.engine
    records = sa.Table('carrier_records', sa.MetaData(), autoload_with=engine)
    moment = datetime.utcnow() - timedelta(hours=1)
    with engine.begin() as connection:
        connection.execute(records.insert().values(
            id='r1', provider_id='telnyx', record_id='rec-r1', version=1, direction='inbound', calling='+17205550111',
            called='+13035550100', started_at=moment, answered_at=moment, finished_at=moment + timedelta(seconds=25),
            amount_micros=3200, raw_amount='0.0032', currency='USD', billed_seconds=60, call_seconds=25,
            effective_at=moment, observed_at=moment, applied=1, created_at=moment))
    human = telnyx_cli('costs', 'spending')
    assert 'Telnyx billed 1 call Faxbot has no record of: $0.0032. It is included in Charged.' in human.stdout
    assert 'Total: $0.0032' in human.stdout


def test_intake_connectors_items_and_test_email(cli):
    added = cli.json('numbers', 'email', 'connectors', 'add', 'Front desk email', '--host', '127.0.0.1', '--port', '9',
                     '--security', 'none', '--from', 'fax@clinic.example', '--to', 'desk@clinic.example')
    assert added['has_password'] is False
    assert [item['name'] for item in cli.json('numbers', 'email', 'connectors', 'list')] == ['Front desk email']
    tested = cli('--json', 'numbers', 'email', 'connectors', 'test', 'Front desk email')
    assert tested.exit_code == 1 and json.loads(tested.stdout)['ok'] is False
    assert cli.json('received', 'deliveries', 'list')['items'] == []
    updated = cli.json('numbers', 'email', 'connectors', 'update', 'Front desk email', '--name', 'Desk email', '--disable')
    assert updated['name'] == 'Desk email' and updated['enabled'] is False and updated['version'] == 2
    cli.json('numbers', 'email', 'connectors', 'update', 'Desk email', '--name', 'Front desk email', '--enable')
    assert cli('received', 'deliveries', 'retry', 'missing-item').exit_code in (5, 6, 9)
    cli.json('numbers', 'email', 'connectors', 'remove', 'Front desk email')
    assert cli.json('numbers', 'email', 'connectors', 'list') == []


def test_only_owners_allow_direct_partners_on_private_networks(cli, tmp_path):
    from app.direct.crypto import Identity, card
    saved = cli.json('system', 'settings', 'set', 'direct_allow_private_peers=false')
    assert saved['changed'] is True
    assert cli.json('system', 'settings', 'get', 'direct')['direct']['allow_private_peers'] is False
    local = tmp_path / 'local.json'
    local.write_text(json.dumps(card(Identity.generate(), organization='Basement server', fax_number='+15550007778',
                                     endpoint='https://127.0.0.1:8443')))
    refused = cli('recipients', 'partners', 'add', local)
    assert refused.exit_code == 6
    assert refused.stderr.strip() == ("The partner's address points to a private or local network, which direct "
                                      'delivery refuses unless DIRECT_ALLOW_PRIVATE_PEERS is turned on.')
    _, token = restricted_key(cli, 'Settings helper', role='Administrator', permissions=('settings:read', 'settings:write'))
    assert cli('system', 'settings', 'set', 'direct_allow_private_peers=true', key=token).exit_code == 4
    cli.json('system', 'settings', 'set', 'direct_allow_private_peers=true')
    assert cli.json('recipients', 'partners', 'add', local)['endpoint'] == 'https://127.0.0.1:8443'


def test_direct_card_peers_challenge_and_confirm(cli, tmp_path):
    from app.direct.crypto import Identity, card

    class Partner:
        async def request(self, method, url, **kwargs):
            assert url == 'https://valley.example/direct/verifications'
            return 200, {'verified': True}

    own = cli.json('recipients', 'partners', 'card')['card']
    assert own['organization'] == 'County Clinic'
    partner = tmp_path / 'valley.json'
    partner.write_text(json.dumps(card(Identity.generate(), organization='Valley Hospital',
                                       fax_number='+15550007777', endpoint='https://valley.example')))
    enrolled = cli.json('recipients', 'partners', 'add', partner)
    assert enrolled['state'] == 'pending'
    assert [item['organization'] for item in cli.json('recipients', 'partners', 'list')] == ['Valley Hospital']
    challenged = cli.json('recipients', 'partners', 'challenge', 'Valley Hospital')
    assert challenged['code_sent'] is True and challenged['fax_id']
    main_module.app.state.direct_http = Partner()
    confirmed = cli.json('recipients', 'partners', 'confirm', '+1 555 000 7777', '1234 5678')
    assert confirmed == {'confirmed': True, 'detail': 'The partner confirmed the code.'}
    assert cli.json('recipients', 'partners', 'deliveries') == []
    revoked = cli.json('recipients', 'partners', 'revoke', 'Valley Hospital')
    assert revoked['state'] == 'revoked'


def test_case_packet_preview_send_and_documents(cli, tmp_path):
    first, second = pdf(tmp_path / 'referral.pdf', 'Referral'), pdf(tmp_path / 'labs.pdf', 'Labs')
    preview = cli.json('recipients', 'cases', 'send', 'CASE-1', '+15551230009', first, second, '--title', 'Referral',
                       '--title', 'Lab results', '--preview')
    assert preview['fax_id'] is None and [item['title'] for item in preview['documents']] == ['Referral', 'Lab results']
    sent = cli.json('recipients', 'cases', 'send', 'CASE-1', '+15551230009', first)
    assert sent['fax_id']
    documents = cli.json('recipients', 'cases', 'documents', 'CASE-1', '--to', '+15551230009')
    assert [item['title'] for item in documents['documents']] == ['referral']
    human = cli('recipients', 'cases', 'documents', 'CASE-1', '--to', '+15551230009')
    assert human.exit_code == 0 and 'referral' in human.stdout
    (case,) = cli.json('recipients', 'cases', 'list')['cases']
    assert (case['case_id'], case['to'], case['documents']) == ('CASE-1', '+15551230009', 1)
    listed = cli('recipients', 'cases', 'list', '--limit', '5').stdout
    assert 'CASE-1' in listed and '1 sent, ' in listed and 'Last sent' in listed


def test_costs_savings_reads_as_estimates(cli):
    result = cli.json('costs', 'savings', '--days', '7')
    assert result['days'] == 7 and result['estimate'] is True
    assert {'sending_together', 'direct_delivery', 'case_packets'} <= set(result)
    human = cli('costs', 'savings')
    assert human.exit_code == 0
    assert 'No money saved in the last 30 days, as far as Faxbot can tell.' in human.stdout
    assert 'Sending together' in human.stdout and 'Direct delivery' in human.stdout and 'Case packets' in human.stdout
    assert result['sentence'] in ' '.join(cli('costs', 'savings', '--days', '7').stdout.split())


def test_costs_recommendations_has_a_receiving_section_of_estimates(cli):
    result = cli.json('costs', 'recommendations')['receiving']
    assert result['days'] == 30 and result['estimate'] is True and result['pool']['state'] == 'no_trunk'
    human = ' '.join(cli('costs', 'recommendations').stdout.split())
    assert 'Receiving Faxbot has no phone line from a carrier set up, so there are no received calls to compare.' in human
    assert 'Phaxio is your only fax service, so there is no second monthly fee to save.' in human


def test_costs_recommendations_has_a_plans_section(cli):
    plans = cli.json('costs', 'recommendations')['plans']
    assert plans['plans'] == [] and plans['estimate'] is True
    human = ' '.join(cli('costs', 'recommendations').stdout.split())
    assert 'Plans You pay no monthly fee for a fax service, so there is no plan to review.' in human


def test_the_plans_section_prints_each_plan_with_both_periods(capsys, monkeypatch):
    from app.cli import output
    from app.cli.commands.delivery import show_plans
    monkeypatch.setattr(output, 'home_currency', lambda: 'USD')
    monkeypatch.setenv('COLUMNS', '200')
    out = output.Output()
    money = lambda amount: [{'currency': 'USD', 'amount': amount}]  # noqa: E731
    window = {'days': 30, 'sent': 5, 'received': 0, 'own_numbers': 0, 'fee': money('10.00'), 'fee_per_fax': money('2.00'),
              'other_way': money('0.05'), 'number_rental': money('1.00'), 'other_routes': ['Telnyx'],
              'without_other_way': 0}
    show_plans(out, {'days': 30, 'plans': [{
        'route': 'humblefax', 'name': 'HumbleFax', 'monthly_fee': money('10.00'), 'state': 'review',
        'sentence': 'Worth reviewing: HumbleFax carried 5 faxes in the last 30 days.', 'action': 'If you decide...',
        'caveats': ['Your HumbleFax plan includes its own fax number.'],
        'windows': [window, {**window, 'days': 0, 'sent': 0, 'fee': [], 'fee_per_fax': [], 'other_way': [],
                             'number_rental': []}]}]})
    text = ' '.join(capsys.readouterr().out.split())
    assert 'HumbleFax, $10.00 a month' in text and 'Worth reviewing: HumbleFax carried 5 faxes' in text
    assert 'Plan fee per fax $2.00 -' in text and 'Rent for the fax number at your carrier $1.00 -' in text


# -- profiles --------------------------------------------------------------------------------

def test_profiles_keep_the_key_private_and_are_used_by_default(cli, tmp_path):
    saved = cli('system', 'profiles', 'save', 'clinic', '--key-stdin', input=BOOTSTRAP + '\n', key=None)
    assert saved.exit_code == 0, saved.stderr
    path = tmp_path / 'cli-config' / 'config.toml'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    shown = cli('system', 'profiles', 'list', key=None)
    assert 'clinic' in shown.stdout and BOOTSTRAP not in shown.stdout
    me = cli('--json', 'access', 'me', key=None, url=None)
    assert me.exit_code == 0 and json.loads(me.stdout)['principal']['kind'] == 'bootstrap'
    prompted = cli('system', 'profiles', 'save', 'other', '--no-use', input='\n', key=None)
    assert prompted.exit_code == 0 and BOOTSTRAP not in prompted.stdout
    assert cli.json('system', 'profiles', 'list', key=None)['profiles'][1] == {'profile': 'other', 'url': ORIGIN,
                                                                   'key_saved': False, 'default': False}
    cli.json('system', 'profiles', 'remove', 'other', key=None)
    elsewhere = cli('access', 'me', key=None, url='https://elsewhere.example')
    assert elsewhere.exit_code == 3 and elsewhere.stderr.startswith('No API key.')
    trailing = cli('--json', 'access', 'me', key=None, url=ORIGIN + '/')
    assert trailing.exit_code == 0
    unknown = cli('--profile', 'missing', 'access', 'me', key=None)
    assert unknown.exit_code == 1 and "no saved profile named 'missing'" in unknown.stderr


# -- generated reference ----------------------------------------------------------------------

def test_committed_command_reference_is_current():
    environment = {key: value for key, value in os.environ.items() if not key.startswith('FAXBOT_')}
    environment.update({'COLUMNS': '100', 'TERM': 'dumb', 'NO_COLOR': '1'})
    generated = subprocess.run([sys.executable, '-m', 'app.cli.reference'], cwd=API_ROOT, env=environment,
                               capture_output=True, text=True, timeout=60, check=True).stdout
    committed = (REPO_ROOT / 'docs/reference/cli.md').read_text(encoding='utf-8')
    assert generated == committed, 'Run make cli-docs and commit docs/reference/cli.md.'


# -- carrier SIP trunk -----------------------------------------------------------------

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


def test_trunk_status_and_calls_read_as_plain_sentences(trunk_cli):
    from app import sip_calls
    engine = trunk_cli.client.app.state.configuration_runtime.manager.store.engine
    sip_calls.SipCallRecords(engine).record_inbound_event({
        'UniqueID': '1791075343.12', 'Caller': '+13035550100', 'DID': '+15555550100', 'Status': 'FAILED',
        'Error': 'Timed out waiting for initial communication', 'Pages': '0', 'Mode': 'T38', 'Answered': '1791075343',
        'Ended': '1791075357'})
    status = trunk_cli('providers', 'trunk', 'status')
    assert status.exit_code == 0, status.stdout
    for sentence in ('Apply these settings to Asterisk, then restart the Asterisk service.',
                     'Encrypted (TLS)', '198.51.100.7', 'No ports need to be opened or forwarded.',
                     # A call has shown what the first test fax would have: no T.38 data came back.
                     "your network changes port numbers, and the last T.38 fax got no fax data back, so Telnyx "
                     "does not follow Faxbot's T.38 packets on this network.",
                     'A fax call from +13035550100 came in, but no fax data arrived from the carrier.'):
        assert sentence in status.stdout, sentence
    assert 'synthetic-Trunk-Pass' not in status.stdout and 'no_t38' not in status.stdout
    assert trunk_cli.json('providers', 'trunk', 'status')['behind_router'] is True
    calls = trunk_cli('providers', 'trunk', 'calls', '--direction', 'inbound')
    assert calls.exit_code == 0 and 'What happened' in calls.stdout and '+13035550100' in calls.stdout
    assert trunk_cli.json('providers', 'trunk', 'calls')['items'][0]['verdict'] == 'no_t38_data_back'
    assert trunk_cli('providers', 'trunk', 'calls', '--direction', 'sideways').exit_code != 0
    # After that verdict, the owner can switch new calls to audio fax and back.
    assert trunk_cli.json('providers', 'trunk', 'status')['suggest_audio'] is True
    assert 'Audio fax may work for new calls: run faxbot providers trunk mode audio.' in \
        trunk_cli('providers', 'trunk', 'status').stdout
    audio = trunk_cli('providers', 'trunk', 'mode', 'audio')
    assert audio.exit_code == 0 and 'New calls use audio fax once you restart the Asterisk service.' in audio.stdout
    assert trunk_cli.json('providers', 'trunk', 'status')['t38'] is False
    assert 'already uses audio fax' in trunk_cli('providers', 'trunk', 'mode', 'audio').stdout
    assert trunk_cli.json('providers', 'trunk', 'mode', 't38') == {
        'mode': 't38', 'changed': True, 'applied': True, 'engine': 'manual',
        'message': 'Saved for Asterisk. Restart the Asterisk service to use these settings.'}
    assert trunk_cli('providers', 'trunk', 'mode', 'fast').exit_code != 0
    # When Faxbot chose audio fax itself, status says why and how to try T.38 again.
    from app import sip_fax_mode
    assert trunk_cli('providers', 'trunk', 'mode', 'audio').exit_code == 0
    values = trunk_cli.client.app.state.configuration_runtime.manager.store.read().active.values
    sip_fax_mode.write(values, 'audio', sip_fax_mode.NO_DATA_BACK)
    status = trunk_cli('providers', 'trunk', 'status').stdout
    assert 'a T.38 fax got no fax data back on this network, so Faxbot uses audio fax.' in status
    assert 'To try T.38 again, run faxbot providers trunk mode t38.' in status
    # Provider names read the same as in the console.
    health = trunk_cli('system', 'health')
    assert 'Phaxio' in health.stdout and ' phaxio' not in health.stdout
    # Apply writes the trunk for Asterisk; this test install manages no Asterisk, so it says what to restart.
    applied = trunk_cli('providers', 'trunk', 'apply')
    assert applied.exit_code == 0, applied.stdout
    assert 'Saved for Asterisk. Restart the Asterisk service to use these settings.' in applied.stdout
    assert trunk_cli.json('providers', 'trunk', 'apply', '--no-wait')['engine'] == 'manual'


def test_trunk_telnyx_shows_t38_per_number_and_turns_it_on_for_one(monkeypatch, tmp_path):
    import httpx
    from app import sip_http, stun, telnyx_t38
    from api.tests.test_telnyx_t38 import FIRST, KEY, FakeTelnyx
    fake = FakeTelnyx()
    monkeypatch.setattr(telnyx_t38, 'TRANSPORT', httpx.MockTransport(fake))
    monkeypatch.setattr(stun, 'probe', lambda servers, **_: None)
    monkeypatch.setattr(sip_http, '_probes', {})
    for client in _serve(monkeypatch, tmp_path, SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_USERNAME='faxbotuser',
                         SIP_TRUNK_PASSWORD='synthetic-Trunk-Pass!42', SIP_TRUNK_CALLER_ID=FIRST,
                         SIP_TRUNK_DIDS=f'{FIRST},+15555550101', TELNYX_API_KEY=KEY):
        cli = Cli(client)
        values = client.app.state.configuration_runtime.manager.store.read().active.values
        telnyx_t38.check(values)
        shown = cli('providers', 'trunk', 'telnyx', 'status')
        assert shown.exit_code == 0, shown.stdout
        for sentence in ('Telnyx has fax over IP (T.38) turned off for +1 555-555-0100, so received faxes there arrive',
                         '+1 555-555-0100', 'Off', '+1 555-555-0101', 'On'):
            assert sentence in shown.stdout, sentence
        status = cli('providers', 'trunk', 'status').stdout
        assert 'Telnyx has fax over IP (T.38) turned off for +1 555-555-0100' in status
        assert 'To turn it on, run faxbot providers trunk telnyx t38-on followed by the number.' in status
        turned = cli('providers', 'trunk', 'telnyx', 't38-on', FIRST)
        assert turned.exit_code == 0, turned.stdout
        assert 'Telnyx now has fax over IP (T.38) turned on for +1 555-555-0100.' in turned.stdout
        assert cli.json('providers', 'trunk', 'telnyx', 'status')['ready'] is True
        assert [request.method for request in fake.requests].count('PATCH') == 1
        assert cli('providers', 'trunk', 'telnyx', 't38-on', '+15555550199').exit_code != 0


def test_trunk_network_says_whether_t38_can_come_back_and_what_to_do(trunk_cli, monkeypatch):
    from app import sip_network
    from app.sip_network import Discovery

    async def colima(*, fresh=False):
        return Discovery(kernel='6.8.0-64-generic', vendor='Apple Inc.', lima=True, in_container=True,
                         hops=('172.17.0.1', None, None), cpus=2, memory_gib=4, disk_gib=20)
    monkeypatch.setattr(sip_network, 'discover', colima)
    before = trunk_cli('providers', 'trunk', 'network', 'status')
    assert before.exit_code == 0 and 'Faxbot has not checked this network yet.' in before.stdout
    checked = trunk_cli('providers', 'trunk', 'network', 'check')
    assert checked.exit_code == 0, checked.stdout
    for sentence in ('Fax over IP (T.38) cannot work here: your network changes port numbers, which Telnyx cannot '
                     'handle for fax over IP.',
                     'Faxbot runs in Docker on this Mac, on a private network inside the Mac.',
                     ', Faxbot switched new calls to audio fax.',
                     'Connect Docker on this Mac directly to your local network', '  colima delete default',
                     '--cpu 2 --memory 4 --disk 20 --network-address --network-mode bridged',
                     'Never add --data', 'Audio fax keeps working meanwhile.', '198.51.100.7'):
        assert sentence in checked.stdout, sentence
    assert 'blocked' not in checked.stdout and 'colima_user' not in checked.stdout
    report = trunk_cli.json('providers', 'trunk', 'network', 'status')
    assert (report['t38'], report['platform'], report['t38_enabled']) == ('blocked', 'colima_user', False)
    # The older `faxbot trunk` group still reaches it, and trunk status points here.
    assert 'which Telnyx cannot handle' in trunk_cli('providers', 'trunk', 'network', 'status').stdout
    # Faxbot may open its fax ports on the router unless this is turned off.
    off = trunk_cli('providers', 'trunk', 'network', 'router-ports', 'off')
    assert off.exit_code == 0, off.stdout
    assert trunk_cli.json('providers', 'trunk', 'network', 'status')['router_ports_enabled'] is False
    assert trunk_cli('providers', 'trunk', 'network', 'router-ports', 'maybe').exit_code != 0
    assert trunk_cli.json('providers', 'trunk', 'network', 'router-ports', 'on')['router_ports_enabled'] is True
    status = trunk_cli('providers', 'trunk', 'status').stdout
    assert 'If the network check shows a problem, run faxbot providers trunk network status to see how to fix it.' \
        in status
    assert 'No ports need to be opened or forwarded.' not in status


def test_trunk_presets_and_use_cover_phone_systems_and_uk_and_australian_carriers(trunk_cli):
    listing = trunk_cli('providers', 'trunk', 'presets')
    assert listing.exit_code == 0, listing.stdout
    for text in ('avaya-ipoffice', 'avaya-aura', 'gamma', 'bt-one-voice', 'telstra-sip-connect', 'Phone system'):
        assert text in listing.stdout, text
    detail = trunk_cli('providers', 'trunk', 'presets', 'avaya-ipoffice')
    assert detail.exit_code == 0 and 'What you set in Avaya IP Office:' in detail.stdout
    assert '1. System, LAN1 (or LAN2), VoIP: tick SIP Trunks Enable.' in detail.stdout
    assert trunk_cli.json('providers', 'trunk', 'presets', 'avaya-aura')['sources'][0]['read_on'] == '2026-10-03'
    assert trunk_cli('providers', 'trunk', 'presets', 'nope').exit_code != 0
    # Choosing a phone system switches to sign-in by address; no username or password is asked for.
    used = trunk_cli('providers', 'trunk', 'use', 'avaya-ipoffice', '--host', '192.168.10.5', '--number-format', 'local',
                     '--prefix', '9')
    assert used.exit_code == 0, used.stdout
    assert "Saved Avaya IP Office as the trunk's phone system. Run faxbot providers trunk apply to connect it." in used.stdout
    status = trunk_cli.json('providers', 'trunk', 'status')
    assert (status['kind'], status['auth'], status['host'], status['dial_format'], status['dial_prefix']) == (
        'phone_system', 'ip', '192.168.10.5', 'local', '9')
    human = trunk_cli('providers', 'trunk', 'status').stdout
    assert 'Phone system' in human and 'Avaya IP Office' in human and 'Internet address' not in human
    assert trunk_cli('providers', 'trunk', 'use', 'avaya-ipoffice', '--transport', 'tls').exit_code != 0
    assert trunk_cli('providers', 'trunk', 'use', 'telnyx', '--number-format', 'local').exit_code != 0
    assert trunk_cli.json('providers', 'trunk', 'use', 'gamma', '--host', '192.0.2.40') == {'preset': 'gamma', 'changed': True}


# -- milestone 2: counts, checks, reload and costs -------------------------------------------

def test_settings_reload_together_check_and_efax_status_read_as_sentences(cli):
    reloaded = cli('system', 'settings', 'reload')
    assert reloaded.exit_code == 0 and reloaded.stdout.startswith('Faxbot read its saved settings again.')
    assert '_meta' in cli.json('system', 'settings', 'reload')
    check = cli('recipients', 'together', 'check', '+15551230001')
    assert check.exit_code == 0
    assert check.stdout.strip() == 'Faxes to this number go straight away; they do not wait for others.'
    assert cli.json('recipients', 'together', 'check', '+15551230001')['sends_together'] is False
    efax = cli('providers', 'efax', 'status')
    assert efax.exit_code == 0
    assert efax.stdout.strip() == 'Faxbot is not collecting received faxes from eFax; eFax is not set up to receive.'
    assert cli.json('providers', 'efax', 'status')['receiving'] is False


def test_costs_of_received_faxes_and_published_plans_in_use(cli):
    first = cli.json('system', 'diagnostics', 'test-fax', '--from', '+15559990001')['id']
    second = cli.json('system', 'diagnostics', 'test-fax', '--from', '+15559990002')['id']
    one = cli('costs', 'received', first)
    assert one.exit_code == 0 and one.stdout.strip()
    every = cli.json('costs', 'received', '--all')['costs']
    assert set(every) == {first, second}
    table = cli('costs', 'received', '--all').stdout
    assert 'From' in table and 'Cost' in table and '+15559990002' in table
    for wrong in ((), (first, '--all')):
        refused = cli('costs', 'received', *wrong)
        assert refused.exit_code == 1 and refused.stderr.strip() == 'Give a received fax ID, or --all.'
    in_use = cli.json('costs', 'plans', '--in-use')
    assert isinstance(in_use['items'], list)
    assert cli('costs', 'plans', '--in-use').exit_code == 0
    refused = cli('costs', 'plans')
    assert refused.exit_code == 1 and refused.stderr.strip() == 'Name a provider, such as efax, or add --in-use.'
    efax = cli.json('costs', 'plans', 'efax')
    assert efax['plans'] and efax['sentence']


def test_money_reads_as_the_console_shows_it(monkeypatch):
    from app.cli import output
    usd = lambda amount: {'currency': 'USD', 'amount': amount}
    assert [output.money_amount(usd(value), 'USD') for value in ('0.005', '0.0032', '1.5', '0.07', '0', '10')] == \
        ['$0.005', '$0.0032', '$1.50', '$0.07', '$0.00', '$10.00']
    assert output.money_amount({'currency': 'EUR', 'amount': '0.005'}, 'USD') == '0.005 EUR'
    assert output.money_amount({'currency': 'GBP', 'amount': '0.07'}, 'GBP') == '£0.07'
    assert output.money_amount({'currency': 'USD', 'amount': '0.0025'}, 'GBP') == '0.0025 USD'
    monkeypatch.setattr(output, 'home_currency', lambda: 'USD')
    assert output.cost_amount({'state': 'estimated', 'estimated_cost': [usd('0.0025')]}) == '$0.0025 estimate'
    assert output.cost_amount({'state': 'reported', 'reported_cost': [usd('0.005')]}) == '$0.005'
    assert output.cost_amount({'state': 'included'}) == 'In your plan'
    assert output.money([usd('0.005'), {'currency': 'EUR', 'amount': '0.01'}]) == '$0.005 + 0.01 EUR'


def test_a_recovered_fax_shows_when_it_arrived_and_that_it_was_brought_in_later():
    from app.cli.commands.fax import arrived, status_label
    from app.cli.output import local_time
    recovered = {'source_received_at': '2026-10-04T03:14:26', 'received_at': '2026-10-04T03:48:00', 'recovered': True}
    assert arrived(recovered) == local_time('2026-10-04T03:14:26') + ' · brought in later'
    assert arrived({'received_at': '2026-10-04T03:48:00'}) == local_time('2026-10-04T03:48:00')
    assert [status_label({'delivery_state': state}) for state in ('success', 'failed', 'in_progress',
                                                                  'reconciliation_required', 'held')] == \
        ['Delivered', 'Failed', 'In progress', 'Needs review', 'Held test fax']
    assert status_label({'delivery_state': 'ready', 'together': {'state': 'waiting'}}) == 'Waiting to go with other faxes'
    assert status_label({'status': 'SUCCESS'}) == 'Delivered'


def test_route_and_spending_words_match_the_console(monkeypatch):
    from app.cli import output
    from app.cli.commands import delivery
    monkeypatch.setattr(output, 'home_currency', lambda: 'USD')
    monkeypatch.setattr(delivery, 'money', output.money)
    plan = {'included_in_plan': True, 'monthly_fee': {'currency': 'USD', 'amount': '10'}, 'rate': None}
    assert delivery._route_rate(plan) == '$10.00 a month, faxes included'
    assert delivery._route_rate({'rate': '$0.005 a minute'}) == '$0.005 a minute'
    assert delivery._route_rate({}) == 'No price set'
    assert delivery.preferred_text({}) == 'Cheapest reliable'
    assert delivery.preferred_text({'preferred_route': 'direct'}) == 'Direct delivery'
    assert delivery.preferred_text({'preferred_route': 'sip', 'routes': [{'route': 'sip', 'label': 'Carrier trunk'}]}) \
        == 'Carrier trunk'
    assert delivery.references_text(False) == 'Full documents'

    class Lines:
        def __init__(self):
            self.lines = []

        def line(self, text):
            self.lines.append(text)
    out = Lines()
    delivery._unrecorded_lines(out, [{'unrecorded_calls': 1, 'unrecorded_matched_to_faxes': 1}])
    assert out.lines == ['1 call reached Faxbot without a call record; its fax is in Received. Included in Charged.']


def test_sent_list_names_the_route_that_carried_each_fax():
    from app.cli.commands.fax import _route_text
    assert _route_text({'backend': 'humblefax'}, None) == 'HumbleFax'
    assert _route_text({'backend': 'humblefax'}, {'routes': ['phaxio', 'signalwire']}) == 'SignalWire (after Phaxio)'
    assert _route_text({'backend': 'phaxio'}, {'routes': ['direct']}) == 'Direct delivery'


def test_status_names_the_route_faxbot_assigned_and_nothing_before_it():
    from app.cli.commands import fax
    from app.cli.errors import CliError

    class Api:
        def __init__(self, cost):
            self.cost = cost

        def get(self, path, params=None):
            if isinstance(self.cost, Exception):
                raise self.cost
            return self.cost
    job = {'id': 'f' * 32, 'backend': 'humblefax', 'to_number': '+13035550123', 'pages': 1}
    assert fax._assigned_route(Api({'routes': []}), job) is None
    assert fax._assigned_route(Api(CliError('Not allowed.')), job) is None
    assert fax._assigned_route(Api({'routes': ['sip']}), job)[0] == 'Provider'
    assert fax._planned_route(Api({'recommended_routes': [{'label': 'Telnyx'}]}), job) == ('Planned route', 'Telnyx')
    assert fax._planned_route(Api(CliError('Not allowed.')), job) is None
    fields = dict(fax._fax_fields(job))
    assert 'Provider' not in fields and 'Planned route' not in fields

