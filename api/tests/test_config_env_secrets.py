"""Credentials in the environment (.env) are read at every start and are the values in force.

Every setting marked secret, except API_KEY and DATABASE_URL, follows its
environment variable at each start: a changed value becomes exactly one new
configuration revision by "environment" with an audit naming the setting, an
unchanged one adds nothing, and a removed one leaves the stored value editable.
While the environment supplies a credential, the API refuses to change it.
All values here are synthetic.
"""
import json
import os
import time
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

BOOTSTRAP = 'synthetic-env-secrets-bootstrap'
ORIGIN = 'https://testserver'
ADMIN = {'X-API-Key': BOOTSTRAP}
REFUSAL = 'This key is set in .env. Change it there, then run docker compose up -d.'


@pytest.fixture(params=['sqlite', 'postgresql'])
def database_url(request, tmp_path):
    from app.schema import create_database_engine
    if request.param == 'sqlite':
        yield f"sqlite:///{tmp_path / 'installation.db'}"
        return
    url = os.environ.get('FAXBOT_SCHEMA_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set FAXBOT_SCHEMA_TEST_POSTGRES_URL to run the PostgreSQL environment credential tests')
    admin = create_database_engine(url)
    namespace = 'faxbot_env_secrets_' + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA {namespace}')
    scoped = sa.engine.make_url(url).update_query_dict({'options': '-csearch_path=' + namespace})
    try:
        yield scoped.render_as_string(hide_password=False)
    finally:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA {namespace} CASCADE')
        admin.dispose()


class Installation:
    def __init__(self, monkeypatch, root, database_url):
        import app.main as main_module
        from app.config_values import ConfigurationValues
        self.main, self.monkeypatch = main_module, monkeypatch
        monkeypatch.setattr(main_module, '_PAIR_ATTEMPTS', {})
        for name in ConfigurationValues.environment_keys():
            monkeypatch.delenv(name, raising=False)
        for name in ('FAXBOT_URL', 'FAXBOT_API_KEY', 'FAXBOT_PROFILE', 'FAXBOT_CLI_DEBUG'):
            monkeypatch.delenv(name, raising=False)
        self.base = {
            'DATABASE_URL': database_url, 'FAX_DATA_DIR': str(root / 'faxdata'),
            'FAXBOT_INSTALLATION_KEY_PATH': str(root / 'installation.key'),
            'FAXBOT_CONFIG_PATH': str(root / 'absent-legacy.json'), 'FAXBOT_PROVIDERS_DIR': str(root / 'providers'),
            'FAX_DISABLED': 'true', 'FAX_BACKEND': 'humblefax', 'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP,
            'PUBLIC_API_URL': ORIGIN, 'FAXBOT_CONSOLE_ORIGINS': ORIGIN, 'MAX_REQUESTS_PER_MINUTE': '0',
            'ENABLE_PERSISTED_SETTINGS': 'false', 'ENABLE_MCP_SSE': 'false', 'ENABLE_MCP_HTTP': 'false',
            'FAXBOT_CLI_CONFIG': str(root / 'cli.toml'), 'TZ': 'UTC', 'COLUMNS': '200',
        }
        for name, value in self.base.items():
            monkeypatch.setenv(name, value)
        time.tzset()

    def start(self, **credentials):
        """Start with these credential variables set (None removes one)."""
        for name, value in credentials.items():
            if value is None:
                self.monkeypatch.delenv(name, raising=False)
            else:
                self.monkeypatch.setenv(name, value)
        return TestClient(self.main.app, base_url=ORIGIN)

    def store(self):
        return self.main.app.state.configuration_runtime.manager.store


def _settings(client):
    response = client.get('/admin/settings', headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def _revisions(store):
    revisions = store.revisions
    with store.engine.connect() as connection:
        return connection.execute(sa.select(revisions.c.actor).order_by(revisions.c.created_at)).scalars().all()


def _environment_audits(store):
    audit = store.access_store.tables['access_audit']
    with store.engine.connect() as connection:
        return [json.loads(row) for row in connection.execute(sa.select(audit.c.details).where(
            audit.c.operation == 'configuration.environment').order_by(audit.c.created_at)).scalars()]


def _active_humblefax(store):
    snapshot = store.read()
    return store.read_profile(snapshot.active.profile_id('outbound')).configuration.credentials


@pytest.fixture
def installation(monkeypatch, tmp_path, database_url):
    created = Installation(monkeypatch, tmp_path, database_url)
    yield created
    created.main.app.state.direct_http = None


def test_environment_credentials_are_read_at_every_start(installation):
    with installation.start(HUMBLEFAX_API_ACCESS_KEY='synthetic-access-1', HUMBLEFAX_API_SECRET_KEY='synthetic-secret-1') as client:
        store = installation.store()
        assert _active_humblefax(store)['access_key'] == 'synthetic-access-1'
        meta = _settings(client)['_meta']
        assert meta['env_managed'] == ['humblefax_access_key', 'humblefax_secret_key']
        assert 'synthetic-access-1' not in json.dumps(_settings(client))
        first_generation = meta['generation']
        assert _revisions(store) == ['bootstrap']

    # The same values at the next start add no revision.
    with installation.start() as client:
        assert _settings(client)['_meta']['generation'] == first_generation
        assert _revisions(installation.store()) == ['bootstrap']

    # A changed value becomes exactly one revision by "environment", audited by name only.
    with installation.start(HUMBLEFAX_API_ACCESS_KEY='synthetic-access-2') as client:
        store = installation.store()
        assert _settings(client)['_meta']['generation'] == first_generation + 1
        assert _revisions(store) == ['bootstrap', 'environment']
        assert _active_humblefax(store)['access_key'] == 'synthetic-access-2'
        assert _active_humblefax(store)['secret_key'] == 'synthetic-secret-1'
        audits = _environment_audits(store)
        assert [entry['settings'] for entry in audits] == [['humblefax_access_key']]
        assert 'synthetic-access' not in json.dumps(audits)
    with installation.start() as client:
        assert _revisions(installation.store()) == ['bootstrap', 'environment']

    # Without the variables the stored values stay and can be edited again.
    with installation.start(HUMBLEFAX_API_ACCESS_KEY=None, HUMBLEFAX_API_SECRET_KEY=None) as client:
        store = installation.store()
        assert _settings(client)['_meta']['env_managed'] == []
        assert _active_humblefax(store)['access_key'] == 'synthetic-access-2'
        expected = _settings(client)['_meta']['desired_revision_id']
        edited = client.put('/admin/settings', headers=ADMIN, json={
            'expected_revision_id': expected, 'humblefax_access_key': 'synthetic-access-3'})
        assert edited.status_code == 200, edited.text
        assert _active_humblefax(store)['access_key'] == 'synthetic-access-3'


@pytest.mark.parametrize('variable, field, public', [
    ('HUMBLEFAX_ACCESS_KEY', 'humblefax_access_key', 'access_key'),
    ('HUMBLEFAX_API_ACCESS_KEY', 'humblefax_access_key', 'access_key'),
    ('HUMBLEFAX_API_SECRET_KEY', 'humblefax_secret_key', 'secret_key'),
    ('TELNYX_SIP_PASSWORD', 'sip_trunk_password', None),
    ('TELNYX_PASS', 'sip_trunk_password', None),
    ('TELNYX_API_KEY', 'telnyx_api_key', None),
])
def test_carrier_names_are_aliases(installation, variable, field, public):
    with installation.start(**{variable: 'synthetic-alias-value'}) as client:
        assert _settings(client)['_meta']['env_managed'] == [field]
        store = installation.store()
        assert getattr(store.read().active.values, field) == 'synthetic-alias-value'
        if public:
            assert _active_humblefax(store)[public] == 'synthetic-alias-value'


def test_the_telnyx_api_key_is_environment_managed_and_never_shown(installation):
    with installation.start(TELNYX_API_KEY='KEYsynthetic0telnyx_value') as client:
        settings = _settings(client)
        assert settings['_meta']['env_managed'] == ['telnyx_api_key']
        assert settings['sip']['telnyx_api_key_set'] is True
        assert 'KEYsynthetic0telnyx_value' not in json.dumps(settings)
        expected = settings['_meta']['desired_revision_id']
        refused = client.put('/admin/settings', headers=ADMIN, json={
            'expected_revision_id': expected, 'telnyx_api_key': 'KEYsynthetic-other'})
        assert refused.status_code == 409 and refused.json() == {'detail': REFUSAL}
        assert installation.store().read().active.values.telnyx_api_key == 'KEYsynthetic0telnyx_value'


def test_the_api_refuses_to_change_a_credential_set_in_the_environment(installation):
    with installation.start(HUMBLEFAX_API_SECRET_KEY='synthetic-secret-env') as client:
        before = _settings(client)
        expected = before['_meta']['desired_revision_id']
        refused = client.put('/admin/settings', headers=ADMIN, json={
            'expected_revision_id': expected, 'humblefax_secret_key': 'synthetic-other', 'max_file_size_mb': 7})
        assert refused.status_code == 409 and refused.json() == {'detail': REFUSAL}
        after = _settings(client)
        assert after['_meta']['generation'] == before['_meta']['generation']
        assert after['limits']['max_file_size_mb'] == before['limits']['max_file_size_mb']
        # Other settings still change normally.
        changed = client.put('/admin/settings', headers=ADMIN, json={
            'expected_revision_id': expected, 'max_file_size_mb': 7})
        assert changed.status_code == 200, changed.text
        assert installation.store().read().active.values.humblefax_secret_key == 'synthetic-secret-env'


def test_plugin_configuration_refuses_an_environment_credential(installation, monkeypatch):
    monkeypatch.setenv('FEATURE_V3_PLUGINS', 'true')
    with installation.start(HUMBLEFAX_API_ACCESS_KEY='synthetic-access-env') as client:
        expected = _settings(client)['_meta']['desired_revision_id']
        refused = client.put('/plugins/humblefax/config', headers=ADMIN, json={
            'expected_revision_id': expected, 'settings': {'access_key': 'synthetic-other'}})
        assert refused.status_code == 409 and refused.json() == {'detail': REFUSAL}
        # Empty settings would reset every HumbleFax field, including the one set in .env.
        cleared = client.put('/plugins/humblefax/config', headers=ADMIN, json={
            'expected_revision_id': expected, 'settings': {}})
        assert cleared.status_code == 409 and cleared.json() == {'detail': REFUSAL}
        assert installation.store().read().active.values.humblefax_access_key == 'synthetic-access-env'


def test_the_installation_key_and_database_location_keep_their_own_rules(installation, tmp_path):
    with installation.start() as client:
        assert client.get('/auth/me', headers=ADMIN).status_code == 200
        assert 'api_key' not in _settings(client)['_meta']['env_managed']
    with installation.start(API_KEY='a-different-environment-key') as client:
        assert client.get('/auth/me', headers=ADMIN).status_code == 200
        assert client.get('/auth/me', headers={'X-API-Key': 'a-different-environment-key'}).status_code == 401
        assert _settings(client)['_meta']['env_managed'] == []
        assert _revisions(installation.store()) == ['bootstrap']


def test_a_send_after_the_change_binds_the_new_account_and_an_earlier_fax_keeps_its_own(installation):
    def send(client, number):
        response = client.post('/fax', headers=ADMIN, data={'to': number},
                               files={'file': ('note.txt', b'Synthetic\n', 'text/plain')})
        assert response.status_code == 202, response.text
        return response.json()['id']

    def binding(store, job_id):
        bindings = store.job_bindings
        with store.engine.connect() as connection:
            profile_id = connection.execute(sa.select(bindings.c.profile_id).where(
                bindings.c.id == job_id)).scalar_one()
        return profile_id, store.read_profile(profile_id).configuration.credentials['access_key']

    with installation.start(HUMBLEFAX_API_ACCESS_KEY='synthetic-access-old', HUMBLEFAX_API_SECRET_KEY='synthetic-secret') as client:
        earlier = send(client, '+15551230001')
    with installation.start(HUMBLEFAX_API_ACCESS_KEY='synthetic-access-new') as client:
        later = send(client, '+15551230002')
        store = installation.store()
        old_profile, old_key = binding(store, earlier)
        new_profile, new_key = binding(store, later)
        assert (old_key, new_key) == ('synthetic-access-old', 'synthetic-access-new')
        assert old_profile != new_profile


def test_an_invalid_credential_in_the_environment_stops_startup_with_one_sentence(installation):
    with installation.start():
        pass
    with pytest.raises(Exception) as stopped:
        with installation.start(TELNYX_PASS='bad\npassword'):
            pass
    assert 'TELNYX_PASS' in str(stopped.value) and 'bad' not in str(stopped.value)


def test_the_command_line_marks_and_refuses_environment_credentials(installation):
    from typer.testing import CliRunner
    from app.cli.main import app as cli_app
    with installation.start(HUMBLEFAX_API_ACCESS_KEY='synthetic-access-env') as client:
        def cli(*args):
            return CliRunner().invoke(cli_app, ['--url', ORIGIN, '--key', BOOTSTRAP, *args],
                                      obj={'client_factory': lambda address, timeout: (client, False)})
        shown = cli('system', 'settings', 'get', 'humblefax')
        assert shown.exit_code == 0, shown.stderr
        assert 'set in .env' in shown.stdout and 'synthetic-access-env' not in shown.stdout
        assert 'Set in .env (change them there, then run docker compose up -d): humblefax_access_key' in shown.stdout
        refused = cli('system', 'settings', 'set', 'humblefax_access_key=synthetic-other')
        assert refused.exit_code == 6
        assert refused.stderr.strip() == REFUSAL
        assert installation.store().read().active.values.humblefax_access_key == 'synthetic-access-env'
