"""Release proof: an installation from the last release before the refresh upgrades and keeps its data.

The previous release's tables come from frozen SQL (fixtures/schema/dd8bd991-*.sql,
identical to db.py on main before the refresh), filled with representative rows
written the way that release wrote them: uuid-hex fax ids, absolute document
paths in the data folder, and API keys in its ``fbk_live_<id>_<secret>`` format
with scrypt or PBKDF2 hashes. Nothing here imports current models to build the
old database.

``faxbot admin migrate`` upgrades it on a stopped installation; the application
then starts on it. A second test carries the upgraded installation through
backup, loss and restore. ``build_previous_release`` is also used by the Docker
runbook to make the same database for the built image.
"""
import base64
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from typer.testing import CliRunner

FIXTURES = Path(__file__).with_name('fixtures') / 'schema'
BOOTSTRAP = 'synthetic-release-bootstrap-key'
ORIGIN = 'https://testserver'
NOBODY = 'http://127.0.0.1:9'
OLD_TIME = datetime(2025, 9, 1, 12, 34, 56)
HISTORICAL_TABLES = ('api_keys', 'fax_jobs', 'inbound_events', 'inbound_faxes', 'inbound_rules', 'mailboxes')


# -- the previous release's database ----------------------------------------------------------

def _b64u(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _previous_release_key(scopes, *, name, owner, algorithm='scrypt', revoked=False, note=None):
    """A key exactly as the previous release generated and stored it."""
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    salt = secrets.token_bytes(16)
    if algorithm == 'scrypt':
        digest = hashlib.scrypt(secret.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
        key_hash = f'scrypt${_b64u(salt)}${_b64u(digest)}$n=16384$r=8$p=1'
    else:
        digest = hashlib.pbkdf2_hmac('sha256', secret.encode(), salt, 200_000, dklen=32)
        key_hash = f'pbkdf2${_b64u(salt)}${_b64u(digest)}$rounds=200000'
    row = {'id': uuid.uuid4().hex, 'key_id': key_id, 'key_hash': key_hash, 'name': name, 'owner': owner,
           'scopes': scopes, 'created_at': OLD_TIME, 'last_used_at': None, 'expires_at': None,
           'revoked_at': OLD_TIME + timedelta(days=3) if revoked else None, 'note': note}
    return f'fbk_live_{key_id}_{secret}', row


def _pdf_bytes(label):
    return (b'%PDF-1.4\n% synthetic previous-release document: ' + label.encode() +
            b'\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n')


def _insert(connection, table, row):
    names = ', '.join(row)
    connection.execute(sa.text(f'INSERT INTO {table} ({names}) VALUES ({", ".join(":" + n for n in row)})'), row)


def build_previous_release(engine, data_dir, *, path_root=None):
    """Create the previous release's schema with representative rows and documents.

    Documents are written into data_dir; rows record them under path_root (the
    data folder as the server sees it, for example /faxdata in a container).
    Returns the tokens, ids and document digests the proof checks afterwards.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    root = str(path_root or data_dir)
    source = (FIXTURES / f'dd8bd991-{engine.dialect.name}.sql').read_text()
    source = '\n'.join(line for line in source.splitlines() if not line.startswith('--'))
    documents = {}

    def document(name, content):
        (data_dir / name).write_bytes(content)
        documents[name] = hashlib.sha256(content).hexdigest()
        return f'{root}/{name}', content

    keys = {
        'sender': _previous_release_key('fax:send,fax:read', name='Front desk scanner', owner='Front desk'),
        'records': _previous_release_key('inbound:list,inbound:read', name='Records sync', owner='Records team',
                                         note='Nightly import'),
        'unrestricted': _previous_release_key('*', name='Old admin script', owner='IT'),
        'retired': _previous_release_key('fax:send', name='Retired kiosk', owner='Front desk', revoked=True),
        'pbkdf2': _previous_release_key('fax:read', name='Status board', owner='Operations', algorithm='pbkdf2'),
    }
    sent, failed, billing, unassigned = (uuid.uuid4().hex for _ in range(4))
    with engine.begin() as connection:
        for statement in source.split(';'):
            if statement.strip():
                connection.exec_driver_sql(statement)
        for _, row in keys.values():
            _insert(connection, 'api_keys', row)
        sent_tiff, _ = document(f'{sent}.tiff', b'II*\x00synthetic previous-release tiff\n')
        document(f'{sent}.pdf', _pdf_bytes('sent referral'))
        failed_tiff, _ = document(f'{failed}.tiff', b'II*\x00synthetic failed tiff\n')
        document(f'{failed}.pdf', _pdf_bytes('failed letter'))
        _insert(connection, 'fax_jobs', {
            'id': sent, 'to_number': '+15551230001', 'file_name': 'referral.pdf', 'tiff_path': sent_tiff,
            'status': 'SUCCESS', 'error': None, 'pages': 2, 'backend': 'phaxio', 'outbound_backend': 'phaxio',
            'provider_sid': '880011', 'pdf_url': f'https://fax.example.test/fax/{sent}/pdf?token=expired',
            'pdf_token': 'expired-previous-token', 'pdf_token_expires_at': OLD_TIME,
            'created_at': OLD_TIME, 'updated_at': OLD_TIME + timedelta(minutes=2)})
        _insert(connection, 'fax_jobs', {
            'id': failed, 'to_number': '+15551230002', 'file_name': 'letter.txt', 'tiff_path': failed_tiff,
            'status': 'failed', 'error': 'Busy', 'pages': 1, 'backend': 'sip', 'outbound_backend': 'sip',
            'provider_sid': None, 'pdf_url': None, 'pdf_token': None, 'pdf_token_expires_at': None,
            'created_at': OLD_TIME + timedelta(hours=1), 'updated_at': OLD_TIME + timedelta(hours=1, minutes=5)})
        _insert(connection, 'mailboxes', {
            'id': uuid.uuid4().hex, 'label': 'Billing', 'allowed_scopes': 'inbound:list,inbound:read',
            'note': 'Invoices and remittances', 'created_at': OLD_TIME, 'updated_at': OLD_TIME})
        _insert(connection, 'inbound_rules', {
            'id': uuid.uuid4().hex, 'to_number': '+15550001111', 'mailbox_label': 'Billing', 'created_at': OLD_TIME})
        received = {}
        for identity, sender, number, label, backend, mailbox in (
                (billing, '+15559870001', '+15550001111', 'billing remittance', 'phaxio', 'Billing'),
                (unassigned, '+15559870002', '+15550002222', 'unassigned referral', 'sip', None)):
            path, content = document(f'{identity}.pdf', _pdf_bytes(label))
            received[identity] = content
            _insert(connection, 'inbound_faxes', {
                'id': identity, 'from_number': sender, 'to_number': number, 'status': 'received',
                'backend': backend, 'inbound_backend': backend,
                'provider_sid': 'PX-SYN-' + identity[:6] if backend == 'phaxio' else None, 'pages': 1,
                'size_bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'pdf_path': path,
                'tiff_path': None, 'mailbox_label': mailbox, 'retention_until': None,
                'pdf_token': 'previous-' + identity[:8], 'pdf_token_expires_at': OLD_TIME, 'error': None,
                'created_at': OLD_TIME + timedelta(hours=2), 'received_at': OLD_TIME + timedelta(hours=2),
                'updated_at': OLD_TIME + timedelta(hours=2)})
        _insert(connection, 'inbound_events', {
            'id': uuid.uuid4().hex, 'provider_sid': 'PX-SYN-' + billing[:6], 'event_type': 'fax.received',
            'created_at': OLD_TIME + timedelta(hours=2)})
    return {'tokens': {name: token for name, (token, _) in keys.items()},
            'key_ids': {name: row['key_id'] for name, (_, row) in keys.items()},
            'key_rows': {name: row for name, (_, row) in keys.items()},
            'sent': sent, 'failed': failed, 'billing': billing, 'unassigned': unassigned,
            'received': received, 'documents': documents}


def _snapshot(engine):
    with engine.connect() as connection:
        return {table: sorted((dict(row) for row in connection.execute(sa.text(f'SELECT * FROM {table}')).mappings()),
                              key=lambda row: row['id'])
                for table in HISTORICAL_TABLES}


def _digests(data_dir, names):
    return {name: hashlib.sha256((Path(data_dir) / name).read_bytes()).hexdigest() for name in names}


# -- installation environment ---------------------------------------------------------------------

def _environment(monkeypatch, root, database_url):
    from app.config_values import ConfigurationValues
    for name in ConfigurationValues.environment_keys():
        monkeypatch.delenv(name, raising=False)
    for name in ('FAXBOT_URL', 'FAXBOT_API_KEY', 'FAXBOT_PROFILE', 'FAXBOT_CLI_DEBUG'):
        monkeypatch.delenv(name, raising=False)
    values = {
        'DATABASE_URL': database_url, 'FAX_DATA_DIR': str(root / 'faxdata'),
        'FAXBOT_INSTALLATION_KEY_PATH': str(root / 'installation.key'),
        'FAXBOT_DIRECT_KEY_PATH': str(root / 'direct.key'),
        'FAXBOT_CONFIG_PATH': str(root / 'absent-legacy.json'), 'FAXBOT_PROVIDERS_DIR': str(root / 'providers'),
        'FAX_DISABLED': 'true', 'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_BACKEND': 'phaxio',
        'INBOUND_ENABLED': 'true', 'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP,
        'PUBLIC_API_URL': ORIGIN, 'FAXBOT_CONSOLE_ORIGINS': ORIGIN, 'MAX_REQUESTS_PER_MINUTE': '0',
        'ENABLE_PERSISTED_SETTINGS': 'false', 'ENABLE_MCP_SSE': 'false', 'ENABLE_MCP_HTTP': 'false',
        'FAXBOT_CLI_CONFIG': str(root / 'cli.toml'), 'COLUMNS': '200', 'TZ': 'UTC',
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    time.tzset()
    return values


class Installation:
    def __init__(self, monkeypatch, root, database_url):
        import app.main as main_module
        monkeypatch.setattr(main_module, '_PAIR_ATTEMPTS', {})
        self.main = main_module
        self.root = root
        self.data_dir = root / 'faxdata'
        self.environment = _environment(monkeypatch, root, database_url)
        self.runner = CliRunner()

    def serve(self):
        return TestClient(self.main.app, base_url=ORIGIN)

    def _invoke(self, args, client=None, input=None):
        from app.cli.main import app as cli_app
        obj = {'client_factory': (lambda address, timeout: (client, False))} if client is not None else None
        return self.runner.invoke(cli_app, [str(arg) for arg in args], input=input, obj=obj)

    def admin_json(self, *args):
        result = self._invoke(['--url', NOBODY, '--json', 'admin', *args])
        assert result.exit_code == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)

    def remote(self, client, *args, key=BOOTSTRAP, input=None):
        return self._invoke(['--url', ORIGIN, '--key', key, *args], client=client, input=input)

    def remote_json(self, client, *args, key=BOOTSTRAP, input=None):
        result = self.remote(client, '--json', *args, key=key, input=input)
        assert result.exit_code == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)


# -- databases ------------------------------------------------------------------------------------------

@pytest.fixture(params=['sqlite', 'postgresql'])
def database_url(request, tmp_path):
    from app.schema import create_database_engine
    if request.param == 'sqlite':
        yield f"sqlite:///{tmp_path / 'installation.db'}"
        return
    url = os.environ.get('FAXBOT_SCHEMA_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set FAXBOT_SCHEMA_TEST_POSTGRES_URL to run the PostgreSQL release upgrade proof')
    admin = create_database_engine(url)
    namespace = 'faxbot_release_test_' + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA {namespace}')
    scoped = sa.engine.make_url(url).update_query_dict({'options': '-csearch_path=' + namespace})
    try:
        yield scoped.render_as_string(hide_password=False)
    finally:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA {namespace} CASCADE')
        admin.dispose()


@pytest.fixture
def previous_release(monkeypatch, tmp_path, database_url):
    """A stopped installation of the previous release: its database, documents and environment."""
    from app.schema import create_database_engine
    installation = Installation(monkeypatch, tmp_path, database_url)
    engine = create_database_engine(database_url)
    try:
        installation.made = build_previous_release(engine, installation.data_dir)
        installation.before = _snapshot(engine)
    finally:
        engine.dispose()
    yield installation
    installation.main.app.state.direct_http = None
    time.tzset()


def _headers(token):
    return {'X-API-Key': token}


# -- upgrade -------------------------------------------------------------------------------------------------

def test_previous_release_upgrades_with_rows_documents_and_keys(previous_release):
    from app.schema import HEAD, create_database_engine
    installation, made = previous_release, previous_release.made
    status = installation.admin_json('status')
    assert status['schema_revision'] is None and status['schema_current'] is False
    assert status['configuration'] is None
    assert {name: status['counts'][name] for name in ('sent_faxes', 'received_faxes', 'api_keys', 'mailboxes')} == {
        'sent_faxes': 2, 'received_faxes': 2, 'api_keys': 4, 'mailboxes': 1}
    # The previous release has no installation key yet, so faxbot admin backup cannot cover it:
    # the upgrade runbook copies the database and data folder by hand before migrating.
    early = installation._invoke(['--url', NOBODY, 'admin', 'backup', installation.root / 'too-early'])
    assert early.exit_code == 5 and 'installation key file' in early.stderr
    assert not (installation.root / 'too-early').exists()

    assert installation.admin_json('migrate') == {'before': None, 'after': HEAD, 'current': True, 'changed': True}
    assert installation.admin_json('migrate')['changed'] is False

    # Every stored value of the previous release is unchanged; documents are untouched.
    engine = create_database_engine(installation.environment['DATABASE_URL'])
    try:
        after = _snapshot(engine)
    finally:
        engine.dispose()
    for table, rows in installation.before.items():
        assert [{name: row[name] for name in rows[0]} for row in after[table]] == rows, table
    assert _digests(installation.data_dir, made['documents']) == made['documents']

    tokens, ids = made['tokens'], made['key_ids']
    with installation.serve() as client:
        assert client.get('/health').json() == {'status': 'ok'}

        # Sent faxes keep their public fields; a send-and-read key keeps working.
        sent = client.get(f"/fax/{made['sent']}", headers=_headers(tokens['sender']))
        assert sent.status_code == 200, sent.text
        body = sent.json()
        assert {name: body[name] for name in ('id', 'to', 'status', 'pages', 'backend', 'provider_sid')} == {
            'id': made['sent'], 'to': '+15551230001', 'status': 'SUCCESS', 'pages': 2, 'backend': 'phaxio',
            'provider_sid': '880011'}
        assert body['created_at'].startswith('2025-09-01T12:34:56')
        failed = client.get(f"/fax/{made['failed']}", headers=_headers(tokens['sender'])).json()
        assert (failed['status'], failed['error'], failed['backend']) == ('failed', 'Busy', 'sip')
        queued = client.post('/fax', headers=_headers(tokens['sender']), data={'to': '+15551230003'},
                             files={'file': ('note.txt', b'Synthetic upgrade check\n', 'text/plain')})
        assert queued.status_code == 202, queued.text
        # The original documents of a sent fax are still served.
        document = client.get(f"/admin/fax-jobs/{made['sent']}/pdf", headers=_headers(BOOTSTRAP))
        assert document.status_code == 200
        assert hashlib.sha256(document.content).hexdigest() == made['documents'][f"{made['sent']}.pdf"]

        # A PBKDF2-hashed key from hosts without scrypt still authenticates.
        assert client.get(f"/fax/{made['sent']}", headers=_headers(tokens['pbkdf2'])).status_code == 200

        # Received faxes keep their fields and mailbox; listing and details work with the old key.
        listed = client.get('/inbound', headers=_headers(tokens['records']))
        assert listed.status_code == 200, listed.text
        rows = {row['id']: row for row in listed.json()}
        assert set(rows) == {made['billing'], made['unassigned']}
        billing = rows[made['billing']]
        assert (billing['fr'], billing['to'], billing['status'], billing['pages'], billing['mailbox']) == (
            '+15559870001', '+15550001111', 'received', 1, 'Billing')
        assert billing['received_at'].startswith('2025-09-01T14:34:56')
        assert rows[made['unassigned']]['mailbox'] is None
        detail = client.get(f"/inbound/{made['billing']}", headers=_headers(tokens['records']))
        assert detail.status_code == 200 and detail.json()['fr'] == '+15559870001'
        # inbound:read no longer opens documents; that needs inbound:document (a new key).
        narrowed = client.get(f"/inbound/{made['billing']}/pdf", headers=_headers(tokens['records']))
        assert narrowed.status_code == 403
        for identity in (made['billing'], made['unassigned']):
            opened = client.get(f'/inbound/{identity}/pdf', headers=_headers(BOOTSTRAP))
            assert opened.status_code == 200 and opened.content == made['received'][identity]

        # A revoked key stays revoked; an unrestricted key waits for review.
        assert client.get(f"/fax/{made['sent']}", headers=_headers(tokens['retired'])).status_code == 401
        waiting = client.get(f"/fax/{made['sent']}", headers=_headers(tokens['unrestricted']))
        assert waiting.status_code in (401, 403), waiting.text

        # Every key keeps its name, owner and note in the key list.
        listed_keys = {item['key_id']: item for item in client.get('/admin/api-keys', headers=_headers(BOOTSTRAP)).json()}
        for name, row in made['key_rows'].items():
            item = listed_keys[ids[name]]
            assert (item['name'], item['owner'], item['note']) == (row['name'], row['owner'], row['note']), name
            assert (item['revoked_at'] is not None) == (name == 'retired')
        assert listed_keys[ids['records']]['scopes'] == ['inbound:list', 'inbound:read']

        # The mailbox and its number route survive.
        mailboxes = installation.remote_json(client, 'mailboxes', 'list')
        assert [item['label'] for item in mailboxes] == ['Billing']
        numbers = installation.remote_json(client, 'numbers', 'list')
        assert [(item['to_number'], item['mailbox_label']) for item in numbers] == [('+15550001111', 'Billing')]

        # After review, the unrestricted key works with the permissions chosen for it. Its owner
        # first needs a role: a key never does more than its owner may.
        integration = f"Legacy integration {ids['unrestricted']}"
        assert installation.remote(client, 'access', 'grant', integration, 'Fax operator').exit_code == 0
        approved = installation.remote(client, 'keys', 'approve', ids['unrestricted'], '--for', integration,
                                       '-p', 'fax:read')
        assert approved.exit_code == 0, (approved.stdout, approved.stderr)
        reviewed = client.get(f"/fax/{made['sent']}", headers=_headers(tokens['unrestricted']))
        assert reviewed.status_code == 200, reviewed.text

    status = installation.admin_json('status')
    assert status['schema_current'] is True and status['configuration']['installation_key_set'] is True
    assert status['counts']['received_faxes'] == 2 and status['counts']['sent_faxes'] == 3


# -- backup and restore of the upgraded installation -------------------------------------------------------

def test_upgraded_installation_backs_up_and_restores_into_a_fresh_installation(previous_release, tmp_path):
    installation, made = previous_release, previous_release.made
    installation.admin_json('migrate')
    provider_secret = 'synthetic-phaxio-secret-' + secrets.token_hex(4)
    with installation.serve() as client:
        owner = installation.remote_json(client, 'owner', 'enroll', '--login', 'olivia', '--name', 'Olivia Owner')
        assert installation.remote(client, 'integrations', 'add', 'Records app').exit_code == 0
        assert installation.remote(client, 'access', 'grant', 'Records app', 'Fax operator').exit_code == 0
        created = installation.remote_json(client, 'keys', 'create', '--for', 'Records app', '-p', 'fax:read',
                                           '--name', 'Records app key')
        saved = installation.remote(client, 'settings', 'set', '--secret', 'phaxio_api_secret',
                                    input=f'{provider_secret}\n' * 2)
        assert saved.exit_code == 0, (saved.stdout, saved.stderr)
        assert provider_secret not in saved.stdout + saved.stderr
    database = installation.environment['DATABASE_URL']
    if database.startswith('sqlite:'):
        # Provider credentials are stored encrypted, never as plain text.
        assert provider_secret.encode() not in Path(database.removeprefix('sqlite:///')).read_bytes()

    folder = tmp_path / 'backups' / 'before-loss'
    made_backup = installation.admin_json('backup', folder)
    assert made_backup['data_files'] >= len(made['documents'])

    # Lose the database, the data folder and the keys; restore into the same, now empty, locations.
    lost = tmp_path / 'lost'
    lost.mkdir()
    for path in (installation.data_dir, installation.root / 'installation.key', installation.root / 'direct.key'):
        if path.exists():
            path.rename(lost / path.name)
    if database.startswith('sqlite:'):
        Path(database.removeprefix('sqlite:///')).rename(lost / 'installation.db')
    else:
        from app.schema import create_database_engine
        engine = create_database_engine(database)
        try:
            with engine.begin() as connection:
                namespace = connection.exec_driver_sql('SELECT current_schema()').scalar_one()
                connection.exec_driver_sql(f'DROP SCHEMA {namespace} CASCADE')
                connection.exec_driver_sql(f'CREATE SCHEMA {namespace}')
        finally:
            engine.dispose()
    restored = installation.admin_json('restore', folder)
    assert restored['needs_upgrade'] is False and restored['data_files'] == made_backup['data_files']
    assert _digests(installation.data_dir, made['documents']) == made['documents']

    # Faxbot starts again as a new process would: no pooled connection to the replaced database file.
    import app.db
    app.db.engine.dispose()
    # A different API_KEY in the environment does not replace the restored installation key.
    os.environ['API_KEY'] = 'a-different-environment-key'
    with installation.serve() as client:
        assert client.get('/auth/me', headers=_headers(BOOTSTRAP)).status_code == 200
        assert client.get('/auth/me', headers=_headers('a-different-environment-key')).status_code == 401
        signed_in = client.post('/auth/login', json={'login': 'olivia', 'password': owner['temporary_password']},
                                headers={'Origin': ORIGIN})
        assert signed_in.status_code == 200, signed_in.text
        client.cookies.clear()
        assert client.get(f"/fax/{made['sent']}", headers=_headers(created['token'])).status_code == 200
        assert client.get(f"/fax/{made['sent']}", headers=_headers(made['tokens']['sender'])).status_code == 200
        received = {row['id'] for row in client.get('/inbound', headers=_headers(BOOTSTRAP)).json()}
        assert received == {made['billing'], made['unassigned']}
        opened = client.get(f"/inbound/{made['billing']}/pdf", headers=_headers(BOOTSTRAP))
        assert opened.content == made['received'][made['billing']]
        users = installation.remote_json(client, 'users', 'list', '--kind', 'user')
        assert [user['login'] for user in users] == ['olivia']
        integrations = {item['display_name'] for item in installation.remote_json(client, 'users', 'list',
                                                                                   '--kind', 'integration')}
        assert 'Records app' in integrations
        assert f"Legacy integration {made['key_ids']['sender']}" in integrations
        settings = installation.remote_json(client, 'settings', 'get')
        shown = json.dumps(settings)
        assert provider_secret not in shown
        assert settings['phaxio']['api_secret'], 'the restored provider secret is reported as set'
