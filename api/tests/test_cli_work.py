"""faxbot work and faxbot import against the real application, in process over HTTPS."""
from datetime import datetime, timedelta
import hashlib
import json
import uuid
import zipfile

import pytest

import app.main as main_module
from app.work.store import WorkStore
from api.tests.test_cli import BOOTSTRAP, ORIGIN, Cli, _serve, pdf
from api.tests.test_access_management_http import COOKIE, PASSWORD, _session_token


KEY = {'X-API-Key': BOOTSTRAP}


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr('app.work.worker.WorkWorker.step', lambda self, now=None: False)
    for client in _serve(monkeypatch, tmp_path):
        yield Cli(client)


def policy(client):
    return client.get('/auth/me', headers=KEY).json()['policy_version']


def ready_user(client, user_login, display_name, role='role_fax_operator', resource='installation'):
    """A user who has set their own password and holds a role, so they can own work."""
    created = client.post('/access/users', headers=KEY, json={'login': user_login, 'display_name': display_name,
                                                              'enabled': True, 'expected_policy_version': policy(client)})
    assert created.status_code == 200, created.text
    user = created.json()['user']
    roles = {role['id']: role for role in client.get('/access/roles', headers=KEY).json()['items']}
    version = client.get(f"/access/users/{user['id']}", headers=KEY).json()['version']
    granted = client.post('/access/assignments', headers=KEY, json={
        'subject': {'kind': 'principal', 'id': user['id'], 'version': version},
        'role': {'id': role, 'version': roles[role]['version']}, 'resource_id': resource,
        'expected_policy_version': policy(client)})
    assert granted.status_code == 200, granted.text
    origin = {'Origin': ORIGIN}
    temporary = created.json()['temporary_password']
    client.cookies.clear()
    signed_in = client.post('/auth/login', json={'login': user_login, 'password': temporary}, headers=origin)
    assert signed_in.status_code == 200, signed_in.text
    client.cookies.clear()
    cookie = {'Cookie': f'{COOKIE}={_session_token(signed_in)}'}
    csrf = client.get('/auth/me', headers=cookie).json()['csrf_token']
    changed = client.post('/auth/password', json={'current_password': temporary, 'password': PASSWORD},
                          headers={**cookie, **origin, 'X-CSRF-Token': csrf})
    assert changed.status_code == 200, changed.text
    client.cookies.clear()
    return user


def receive(tmp_path, to_number, text='Synthetic referral'):
    identity = uuid.uuid4().hex
    path = pdf(tmp_path / f'{identity}.pdf', text)
    content = path.read_bytes()
    moment = datetime.utcnow() - timedelta(minutes=5)
    main_module.app.state.access_runtime.inbound.accept(dict(
        id=identity, from_number='+15559990000', to_number=to_number, status='received', backend='sip', pages=1,
        size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), pdf_path=str(path),
        created_at=moment, received_at=moment, updated_at=moment), country='US')
    return identity


def feed(hours):
    engine = main_module.app.state.configuration_runtime.manager.store.engine
    WorkStore(engine).feed(installation_hours=hours)


def test_work_commands_from_target_to_export(cli, tmp_path):
    saved = cli('numbers', 'mailboxes', 'target', '--acknowledge-hours', '24')
    assert saved.exit_code == 0, saved.stdout + saved.stderr
    assert "Installation target: 24 hours. This is your team's operational target, not a legal deadline." in saved.stdout
    ready_user(cli.client, 'dana', 'Dana Example')
    inbound_id = receive(tmp_path, '+15550100001')
    feed(cli.json('numbers', 'mailboxes', 'target')['acknowledge_hours'])
    listing = cli('received', 'owners')
    assert listing.exit_code == 0 and 'Waiting for an owner.' in listing.stdout
    (item,) = cli.json('received', 'owners')['items']
    assert item['inbound_fax_id'] == inbound_id and item['id'] not in listing.stdout
    assert item['id'] in cli('received', 'owners', '--ids').stdout
    shown = cli('received', 'history', item['id'])
    assert 'Acknowledge within 24 hours of the document arriving (installation setting)' in shown.stdout
    assert 'The document arrived.' in shown.stdout
    nobody = cli('received', 'assign', item['id'], 'nobody')
    assert nobody.exit_code != 0 and 'nobody cannot see this document, or there is no such person. People who can: dana.' in nobody.stderr + nobody.stdout
    assigned = cli('received', 'assign', item['id'], 'dana')
    assert assigned.exit_code == 0 and 'Assigned to Dana Example; acknowledge by ' in assigned.stdout
    refused = cli('received', 'acknowledge', item['id'])
    assert refused.exit_code != 0 and 'Only the owner can acknowledge this item.' in refused.stderr + refused.stdout
    done = cli('received', 'done', item['id'], '--note', 'Filed in the case system')
    assert done.exit_code == 0 and 'Done: Filed in the case system.' in done.stdout
    assert cli('received', 'reopen', item['id']).exit_code == 0
    assert cli.json('received', 'owners', '--mine')['items'] == []
    target = tmp_path / 'evidence.zip'
    exported = cli('received', 'export', item['id'], '-o', target)
    assert exported.exit_code == 0 and target.exists()
    with zipfile.ZipFile(target) as archive:
        assert set(archive.namelist()) == {'manifest.json', 'original.pdf', 'history.txt'}
        manifest = json.loads(archive.read('manifest.json'))
    assert [event['kind'] for event in manifest['history']] == ['received', 'assigned', 'done', 'reopened']
    again = cli('received', 'export', item['id'], '-o', target)
    assert again.exit_code != 0 and 'already exists' in again.stderr + again.stdout


def test_work_settings_for_a_mailbox(cli):
    created = cli.client.post('/access/mailboxes', headers=KEY, json={
        'label': 'Front Desk', 'enabled': True, 'expected_policy_version': policy(cli.client)})
    assert created.status_code == 200, created.text
    ready_user(cli.client, 'sam', 'Sam Example', resource=created.json()['mailbox']['resource_id'])
    changed = cli('numbers', 'mailboxes', 'target', '--mailbox', 'front desk', '--hours', '4', '--backup', 'sam')
    assert changed.exit_code == 0, changed.stdout + changed.stderr
    assert 'Saved: Front Desk.' in changed.stdout and '4 hours' in changed.stdout and 'Sam Example' in changed.stdout
    row = cli.json('numbers', 'mailboxes', 'target')['mailboxes'][0]
    assert row['acknowledge_hours'] == 4 and row['backup']['name'] == 'Sam Example'
    cleared = cli.json('numbers', 'mailboxes', 'target', '--mailbox', 'Front Desk', '--use-installation-target', '--no-backup')
    assert cleared['mailboxes'][0]['acknowledge_hours'] is None and cleared['mailboxes'][0]['backup'] is None
    missing = cli('numbers', 'mailboxes', 'target', '--hours', '4')
    assert missing.exit_code != 0 and 'Add --mailbox' in missing.stderr + missing.stdout


def test_import_command_reports_the_servers_sentence(cli, tmp_path):
    text = tmp_path / 'note.pdf'
    text.write_bytes(b'not a pdf at all')
    refused = cli('received', 'import', text, '--source', 'case-system', '--id', 'op-1')
    assert refused.exit_code != 0 and 'The file is not a PDF.' in refused.stderr + refused.stdout


def test_import_command_imports_once_and_reports_a_replay(cli, tmp_path):
    document = pdf(tmp_path / 'referral.pdf', 'Imported from the case system')
    first = cli('received', 'import', document, '--source', 'case-system', '--id', 'case-7', '--to', '+15550100001',
                '--received-at', '2026-10-03T14:05:00Z')
    assert first.exit_code == 0 and 'Document imported.' in first.stdout, first.stdout + first.stderr
    again = cli.json('received', 'import', document, '--source', 'case-system', '--id', 'case-7', '--to', '+15550100001')
    assert again['status'] == 'duplicate'


def test_a_user_runs_work_commands_with_their_own_key(cli, tmp_path):
    ready_user(cli.client, 'dana', 'Dana Example')
    created = cli.json('access', 'keys', 'create', '--for', 'dana', '-p', 'work:read', '-p', 'inbound:read',
                       '--name', 'Dana laptop')
    dana = created['token']
    receive(tmp_path, '+15550100001')
    feed(0)
    (item,) = cli.json('received', 'owners')['items']
    assert cli('received', 'assign', item['id'], 'dana').exit_code == 0
    mine = cli.json('received', 'owners', '--mine', key=dana)['items']
    assert [entry['id'] for entry in mine] == [item['id']]
    acknowledged = cli('received', 'acknowledge', item['id'], key=dana)
    assert acknowledged.exit_code == 0 and 'Acknowledged by Dana Example.' in acknowledged.stdout
    refused = cli('received', 'reopen', item['id'], key=dana)  # the key carries no work:manage
    assert refused.exit_code != 0


def test_received_commands_take_either_id_and_count_by_state(cli, tmp_path):
    ready_user(cli.client, 'dana', 'Dana Example')
    inbound_id = receive(tmp_path, '+15550100001')
    feed(0)
    (item,) = cli.json('received', 'owners')['items']
    assert item['id'] != inbound_id
    # Either list's ID works: the received fax's own ID for owner commands, the owners-list ID for fax commands.
    assert cli.json('received', 'history', inbound_id)['id'] == item['id']
    assert cli.json('received', 'show', item['id'])['id'] == inbound_id
    target = tmp_path / 'by-owner-id.pdf'
    assert cli('received', 'pdf', item['id'], '-o', target).exit_code == 0
    assert target.read_bytes().startswith(b'%PDF')
    assigned = cli('received', 'assign', inbound_id, 'dana')
    assert assigned.exit_code == 0 and 'Assigned to Dana Example' in assigned.stdout
    exported = tmp_path / 'by-fax-id.zip'
    assert cli('received', 'export', inbound_id, '-o', exported).exit_code == 0 and exported.exists()
    counts = cli.json('received', 'counts')
    assert (counts['open'], counts['unassigned'], counts['done']) == (1, 0, 0)
    human = cli('received', 'counts').stdout
    assert '0 received faxes are waiting for an owner.' in human and '1 received fax is open.' in human
    missing = cli('received', 'history', '0' * 32)
    assert missing.exit_code == 5 and missing.stderr.strip() == ('No received fax you can see has that ID. '
                                                                 'See faxbot received list --ids.')
    assert cli('received', 'show', '0' * 32).exit_code == 5
