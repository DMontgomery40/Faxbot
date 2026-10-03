"""faxbot admin on temporary installations: recovery, backup and restore, upgrade and status.

Each test creates a real installation by starting the application once, stops
it, then runs the local commands against its database and files. A server
lifespan is started again to prove the result works through the API.
"""
import json
import os
from pathlib import Path
import stat
import time
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import app.main as main_module
from app.cli.main import app as cli_app
from app.config_values import ConfigurationValues
from app.schema import HEAD, create_database_engine

BOOTSTRAP = 'synthetic-admin-bootstrap-key'
ORIGIN = 'https://testserver'
NOBODY = 'http://127.0.0.1:9'
LOCKS = {'.faxbot-startup.lock', '.faxbot-serving.lock'}


def _environment(monkeypatch, root, database_url=None):
    for name in ConfigurationValues.environment_keys():
        monkeypatch.delenv(name, raising=False)
    for name in ('FAXBOT_URL', 'FAXBOT_API_KEY', 'FAXBOT_PROFILE', 'FAXBOT_CLI_DEBUG'):
        monkeypatch.delenv(name, raising=False)
    values = {
        'DATABASE_URL': database_url or f"sqlite:///{root / 'installation.db'}",
        'FAX_DATA_DIR': str(root / 'faxdata'),
        'FAXBOT_INSTALLATION_KEY_PATH': str(root / 'installation.key'),
        'FAXBOT_DIRECT_KEY_PATH': str(root / 'direct.key'),
        'FAXBOT_CONFIG_PATH': str(root / 'absent-legacy.json'),
        'FAXBOT_PROVIDERS_DIR': str(root / 'providers'),
        'FAX_DISABLED': 'true', 'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_BACKEND': 'phaxio',
        'INBOUND_ENABLED': 'false', 'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP,
        'PUBLIC_API_URL': ORIGIN, 'FAXBOT_CONSOLE_ORIGINS': ORIGIN, 'MAX_REQUESTS_PER_MINUTE': '0',
        'ENABLE_PERSISTED_SETTINGS': 'false', 'ENABLE_MCP_SSE': 'false', 'ENABLE_MCP_HTTP': 'false',
        'DIRECT_DELIVERY_ENABLED': 'true', 'DIRECT_ORGANIZATION': 'County Clinic',
        'DIRECT_FAX_NUMBER': '+15550006666', 'FAXBOT_CLI_CONFIG': str(root / 'cli.toml'),
        'COLUMNS': '200', 'TZ': 'UTC',
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    time.tzset()
    return values


class Installation:
    def __init__(self, monkeypatch, root, database_url=None):
        self.root = root
        self.environment = _environment(monkeypatch, root, database_url)
        self.runner = CliRunner()

    def serve(self):
        return TestClient(main_module.app, base_url=ORIGIN)

    def admin(self, *args, input=None, client=None):
        """A local admin command; the server address points where nothing listens unless client is given."""
        obj = {'client_factory': (lambda address, timeout: (client, False))} if client is not None else None
        return self.runner.invoke(cli_app, ['--url', ORIGIN if client is not None else NOBODY, 'admin',
                                            *[str(arg) for arg in args]], input=input, obj=obj)

    def remote(self, client, *args, key=BOOTSTRAP):
        result = self.runner.invoke(cli_app, ['--url', ORIGIN, '--key', key, *[str(arg) for arg in args]],
                                    obj={'client_factory': lambda address, timeout: (client, False)})
        return result

    def remote_json(self, client, *args, key=BOOTSTRAP):
        result = self.remote(client, '--json', *args, key=key)
        assert result.exit_code == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)

    def admin_json(self, *args):
        result = self.runner.invoke(cli_app, ['--url', NOBODY, '--json', 'admin', *[str(arg) for arg in args]])
        assert result.exit_code == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)


@pytest.fixture
def installation(monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, '_PAIR_ATTEMPTS', {})
    created = Installation(monkeypatch, tmp_path)
    with created.serve():
        pass
    yield created
    main_module.app.state.direct_http = None
    time.tzset()


def _send(installation, client, tmp_path, name='note.txt'):
    note = tmp_path / name
    note.write_text('Synthetic backup fax\n')
    return installation.remote_json(client, 'send', '+15551230001', note, '--queue')


# -- running installations are refused ---------------------------------------------------------

def test_admin_commands_refuse_a_running_installation(installation):
    with installation.serve() as client:
        locked = installation.admin('status')
        assert locked.exit_code == 10
        assert locked.stderr.strip() == 'Faxbot is running on this installation. Stop it, then run this command again.'
        answering = installation.admin('status', client=client)
        assert answering.exit_code == 10
        assert answering.stderr.strip() == (f'A Faxbot server answers at {ORIGIN}. Stop it, then run this command '
                                            'again.')
        assert installation.admin('recover-owner', '--yes').exit_code == 10
        assert installation.admin('backup', installation.root / 'refused').exit_code == 10
    assert not (installation.root / 'refused').exists()


# -- status and migrate ---------------------------------------------------------------------------

def test_status_and_migrate_on_an_existing_and_a_new_database(installation, monkeypatch, tmp_path):
    status = installation.admin_json('status')
    assert status['schema_revision'] == HEAD and status['schema_current'] is True
    assert status['counts']['users'] == 0 and status['counts']['owners'] == 0
    assert status['configuration'] == {'generation': 1, 'restart_pending': False, 'installation_key_set': True,
                                       'key_file_found': True}
    assert BOOTSTRAP not in json.dumps(status) and 'sqlite:///' not in json.dumps(status)
    human = installation.admin('status')
    assert human.exit_code == 0 and 'Schema up to date' in human.stdout
    assert installation.admin_json('migrate') == {'before': HEAD, 'after': HEAD, 'current': True, 'changed': False}

    fresh = tmp_path / 'fresh'
    (fresh / 'faxdata').mkdir(parents=True)
    monkeypatch.setenv('DATABASE_URL', f"sqlite:///{fresh / 'new.db'}")
    monkeypatch.setenv('FAX_DATA_DIR', str(fresh / 'faxdata'))
    migrated = installation.admin_json('migrate')
    assert migrated == {'before': None, 'after': HEAD, 'current': True, 'changed': True}
    empty = installation.admin_json('status')
    assert empty['configuration'] is None and empty['counts']['sent_faxes'] == 0
    missing = installation.runner.invoke(cli_app, ['--url', NOBODY, 'admin', '--data-dir', str(tmp_path / 'nope'),
                                                   'status'])
    assert missing.exit_code == 5 and 'There is no Faxbot data folder' in missing.stderr


# -- owner recovery ------------------------------------------------------------------------------

def test_recover_owner_then_enroll_an_owner_and_sign_in(installation):
    with installation.serve() as client:
        first = installation.remote(client, 'owner', 'enroll', '--login', 'olivia', '--name', 'Olivia Owner')
        assert first.exit_code == 0, first.stderr
        browser = client.post('/auth/key-login', json={'api_key': BOOTSTRAP}, headers={'Origin': ORIGIN})
        assert browser.status_code == 200
        bootstrap_session = client.cookies.get('__Host-faxbot_session')
        client.cookies.clear()
        assert bootstrap_session

    refused = installation.runner.invoke(cli_app, ['--url', NOBODY, '--json', 'admin', 'recover-owner'])
    assert refused.exit_code == 1 and 'Add --yes' in json.loads(refused.stdout)['error']['message']
    declined = installation.admin('recover-owner', input='n\n')
    assert declined.exit_code == 1
    recovered = installation.admin('recover-owner', input='y\n')
    assert recovered.exit_code == 0, recovered.stderr
    secret = recovered.stdout.split('Installation key (shown only once): ')[1].split()[0]
    assert len(secret) >= 40 and recovered.stdout.count(secret) == 1 and secret not in recovered.stderr
    assert 'faxbot' in recovered.stdout and 'owner enroll' in recovered.stdout

    with installation.serve() as client:
        assert installation.remote(client, 'me').exit_code == 3
        me = installation.remote_json(client, 'me', key=secret)
        assert me['principal']['kind'] == 'bootstrap'
        stale = client.get('/auth/me', headers={'Cookie': f'__Host-faxbot_session={bootstrap_session}'})
        assert stale.status_code == 401
        enrolled = installation.remote_json(client, 'owner', 'enroll', '--login', 'rescue', '--name', 'Rescue Owner',
                                            key=secret)
        signed_in = client.post('/auth/login', json={'login': 'rescue', 'password': enrolled['temporary_password']},
                                headers={'Origin': ORIGIN})
        assert signed_in.status_code == 200 and signed_in.json()['password_change_required'] is True
        client.cookies.clear()
        audit = installation.remote_json(client, 'audit', 'list', '--operation', 'owner.recover', key=secret)
        assert len(audit) == 1 and audit[0]['actor'] is None and audit[0]['outcome'] == 'allowed'
        assert audit[0]['details']['source'] == 'host_terminal'
        assert secret not in json.dumps(audit)
    status = installation.admin_json('status')
    assert status['counts']['owners'] == 2 and status['configuration']['generation'] == 2


def test_recovery_with_settings_waiting_for_a_restart_takes_effect_at_the_next_start(installation):
    with installation.serve() as client:
        staged = installation.remote_json(client, 'settings', 'set', 'artifact_ttl_days=9')
        assert staged['_meta']['apply_state'] == 'pending_restart'
    recovered = installation.admin_json('recover-owner', '--yes')
    assert recovered['active_after_restart'] is True
    secret = recovered['installation_key']
    with installation.serve() as client:
        assert installation.remote(client, 'me').exit_code == 3
        assert installation.remote_json(client, 'me', key=secret)['principal']['kind'] == 'bootstrap'
        limits = installation.remote_json(client, 'settings', 'get', 'limits', key=secret)['limits']
        assert limits['artifact_ttl_days'] == 9


def test_recover_owner_needs_the_installation_key_file(installation, monkeypatch, tmp_path):
    monkeypatch.setenv('FAXBOT_INSTALLATION_KEY_PATH', str(tmp_path / 'missing.key'))
    result = installation.admin('recover-owner', '--yes')
    assert result.exit_code == 5 and 'installation key file' in result.stderr


# -- backup and restore ------------------------------------------------------------------------------

def _tree(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob('*'))
            if path.is_file() and path.name not in LOCKS}


def test_backup_and_restore_round_trip_on_sqlite(installation, tmp_path):
    with installation.serve() as client:
        sent = _send(installation, client, tmp_path)
        card = installation.remote_json(client, 'direct', 'card')['card']
    data_dir = Path(installation.environment['FAX_DATA_DIR'])
    database = installation.root / 'installation.db'
    original = {'data': _tree(data_dir), 'key': (installation.root / 'installation.key').read_bytes(),
                'direct': (installation.root / 'direct.key').read_bytes()}

    folder = tmp_path / 'backups' / 'monday'
    made = installation.admin_json('backup', folder)
    assert made['data_files'] >= 1 and made['direct_delivery_key'] is True and made['schema_revision'] == HEAD
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert set(manifest['files']) >= {'database/faxbot.sqlite3', 'keys/installation.key', 'keys/direct-identity.key'}
    assert not any(Path(name).name in LOCKS for name in manifest['files'])
    for path in folder.rglob('*'):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert installation.admin('backup', folder).exit_code == 6
    inside = installation.admin('backup', data_dir / 'nested')
    assert inside.exit_code == 6 and 'outside the data folder' in inside.stderr
    assert not (data_dir / 'nested').exists()

    occupied = installation.admin('restore', folder)
    assert occupied.exit_code == 6 and 'Add --force' in occupied.stderr

    # Lose the installation, then restore it in the same places.
    lost = tmp_path / 'lost'
    lost.mkdir()
    for path in (database, data_dir, installation.root / 'installation.key', installation.root / 'direct.key'):
        path.rename(lost / path.name)
    restored = installation.admin_json('restore', folder)
    assert restored['needs_upgrade'] is False and restored['data_files'] == made['data_files']
    assert _tree(data_dir) == original['data']
    assert (installation.root / 'installation.key').read_bytes() == original['key']
    assert (installation.root / 'direct.key').read_bytes() == original['direct']
    assert stat.S_IMODE((installation.root / 'installation.key').stat().st_mode) == 0o600

    with installation.serve() as client:
        jobs = installation.remote_json(client, 'jobs', 'list')
        assert [job['id'] for job in jobs['jobs']] == [sent['id']]
        target = tmp_path / 'after-restore.pdf'
        installation.remote_json(client, 'jobs', 'pdf', sent['id'], '-o', target)
        assert target.read_bytes().startswith(b'%PDF')
        assert installation.remote_json(client, 'direct', 'card')['card'] == card

    forced = installation.admin_json('restore', folder, '--force')
    assert forced['data_files'] == made['data_files']


def test_restore_refuses_a_backup_that_does_not_match_its_manifest(installation, tmp_path):
    folder = tmp_path / 'backup'
    installation.admin_json('backup', folder)
    empty = tmp_path / 'elsewhere'
    target = folder / 'keys' / 'installation.key'
    content = target.read_bytes()
    target.write_bytes(content[:-1] + (b'A' if content[-1:] != b'A' else b'B'))
    tampered = installation.runner.invoke(cli_app, ['--url', NOBODY, 'admin', '--data-dir', str(empty / 'data'),
                                                    '--database-url', f"sqlite:///{empty / 'db.sqlite'}",
                                                    '--key-file', str(empty / 'key'), 'restore', str(folder)])
    assert tampered.exit_code == 1 and 'does not match its manifest' in tampered.stderr
    target.write_bytes(content)
    (folder / 'data' / 'extra.txt').parent.mkdir(exist_ok=True)
    (folder / 'data' / 'extra.txt').write_text('added later')
    added = installation.runner.invoke(cli_app, ['--url', NOBODY, 'admin', '--data-dir', str(empty / 'data'),
                                                 '--database-url', f"sqlite:///{empty / 'db.sqlite'}",
                                                 '--key-file', str(empty / 'key'), 'restore', str(folder)])
    assert added.exit_code == 1 and 'files were added or removed' in added.stderr
    assert not (empty / 'db.sqlite').exists() and not (empty / 'key').exists()


# -- PostgreSQL ------------------------------------------------------------------------------------------

@pytest.fixture
def postgres_url():
    url = os.environ.get('FAXBOT_SCHEMA_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set FAXBOT_SCHEMA_TEST_POSTGRES_URL to run the PostgreSQL admin tests')
    admin = create_database_engine(url)
    namespace = 'faxbot_cli_test_' + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA {namespace}')
    scoped = sa.engine.make_url(url).update_query_dict({'options': '-csearch_path=' + namespace})
    try:
        yield scoped.render_as_string(hide_password=False), namespace, admin
    finally:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA {namespace} CASCADE')
        admin.dispose()


def test_postgresql_status_migrate_backup_and_restore(postgres_url, monkeypatch, tmp_path):
    url, namespace, admin = postgres_url
    monkeypatch.setattr(main_module, '_PAIR_ATTEMPTS', {})
    installation = Installation(monkeypatch, tmp_path, database_url=url)
    (tmp_path / 'faxdata').mkdir()
    assert installation.admin_json('migrate')['after'] == HEAD
    with installation.serve() as client:
        sent = _send(installation, client, tmp_path)
        enrolled = installation.remote_json(client, 'owner', 'enroll', '--login', 'pat', '--name', 'Pat Owner')
    status = installation.admin_json('status')
    assert status['database'].startswith('PostgreSQL database') and status['counts']['owners'] == 1
    assert 'faxbot_ci_synthetic' not in json.dumps(status) and 'Aa_' not in json.dumps(status)

    folder = tmp_path / 'pg-backup'
    made = installation.admin_json('backup', folder)
    assert (folder / 'database' / 'tables' / 'access_resources.jsonl').exists()
    assert installation.admin('restore', folder).exit_code == 6

    data_dir = Path(installation.environment['FAX_DATA_DIR'])
    lost = tmp_path / 'lost'
    lost.mkdir()
    for path in (data_dir, tmp_path / 'installation.key', tmp_path / 'direct.key'):
        if path.exists():
            path.rename(lost / path.name)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'DROP SCHEMA {namespace} CASCADE')
        connection.exec_driver_sql(f'CREATE SCHEMA {namespace}')
        connection.exec_driver_sql(f'CREATE TABLE {namespace}.someone_elses (id integer)')
    shared = installation.admin('restore', folder, '--force')
    assert shared.exit_code == 6 and 'not part of this Faxbot backup' in shared.stderr
    with admin.begin() as connection:
        assert connection.exec_driver_sql(
            f"SELECT count(*) FROM information_schema.tables WHERE table_schema = '{namespace}'").scalar() == 1
        connection.exec_driver_sql(f'DROP TABLE {namespace}.someone_elses')
    restored = installation.admin_json('restore', folder)
    assert restored['data_files'] == made['data_files']
    with installation.serve() as client:
        assert [job['id'] for job in installation.remote_json(client, 'jobs', 'list')['jobs']] == [sent['id']]
        signed_in = client.post('/auth/login', json={'login': 'pat', 'password': enrolled['temporary_password']},
                                headers={'Origin': ORIGIN})
        assert signed_in.status_code == 200
        client.cookies.clear()
        # New rows still get fresh identifiers after the restore.
        installation.remote_json(client, 'users', 'add', 'sam', '--name', 'Sam')
    forced = installation.admin_json('restore', folder, '--force')
    assert forced['data_files'] == made['data_files']
