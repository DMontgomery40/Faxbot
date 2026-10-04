"""Faxbot creates the Asterisk manager login itself and shares it with its fax engine.

Nobody types the manager password twice: the first start that uses the SIP
trunk creates it (a setting saved by "system") and writes it where the
Asterisk container reads it. ASTERISK_AMI_PASSWORD in the environment wins,
and an installation already connected to an Asterisk of its own keeps its
password.
"""
import os
import stat

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main, sip_trunk
from app.ami import ami_client
from app.config_runtime import ConfigurationRuntime


BOOTSTRAP = 'synthetic-engine-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}


@pytest.fixture(autouse=True)
def no_engine(monkeypatch):
    """No test reaches an Asterisk: the manager connection is never opened."""
    async def connect():
        return None

    async def settle(timeout):
        return False
    monkeypatch.setattr(ami_client, 'connect', connect)
    monkeypatch.setattr(ami_client, 'settle', settle)


def _serve(monkeypatch, **environment):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        **environment}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    return TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'})


def _latest_actor(client):
    store = client.app.state.configuration_runtime.manager.store
    with store.engine.connect() as connection:
        rows = connection.execute(sa.select(store.revisions.c.actor).order_by(
            store.revisions.c.created_at.desc())).scalars().all()
    return rows


def _login_file(installation):
    return os.path.join(installation['FAX_DATA_DIR'], 'asterisk', 'manager.credentials')


def test_the_first_start_with_the_sip_trunk_creates_and_shares_the_manager_login(isolated_installation, monkeypatch):
    with _serve(monkeypatch, FAX_BACKEND='sip') as client:
        values = client.app.state.configuration_runtime.manager.store.read().active.values
        assert values.ami_password not in ('', 'changeme') and len(values.ami_password) >= 24
        # Two settings saved by "system": the inbound secret and this login.
        assert _latest_actor(client).count('system') == 2
        path = _login_file(isolated_installation)
        assert open(path).read() == f'api\n{values.ami_password}\n'
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        view = client.get('/admin/settings', headers=ADMIN).json()['sip']
        assert view['ami_password_is_default'] is False and view['ami_password_shared'] is True
        assert values.ami_password not in client.get('/admin/settings', headers=ADMIN).text
        created = values.ami_password
    # Later starts keep the same login.
    with _serve(monkeypatch, FAX_BACKEND='sip') as client:
        assert client.app.state.configuration_runtime.manager.store.read().active.values.ami_password == created


def test_choosing_the_sip_trunk_later_creates_the_login_at_the_restart_that_uses_it(isolated_installation, monkeypatch):
    with _serve(monkeypatch) as client:
        assert not os.path.exists(_login_file(isolated_installation))
        current = client.get('/admin/settings', headers=ADMIN).json()
        saved = client.put('/admin/settings', headers=ADMIN, json={
            'expected_revision_id': current['_meta']['desired_revision_id'], 'backend': 'sip'})
        assert saved.status_code == 200, saved.text
        assert saved.json()['_meta']['apply_state'] == 'pending_restart'
        # Nothing is created before the restart that starts using Asterisk.
        assert client.get('/admin/settings', headers=ADMIN).json()['sip']['ami_password_is_default'] is True
    monkeypatch.setenv('FAX_BACKEND', 'phaxio')
    with _serve(monkeypatch) as client:
        active = client.app.state.configuration_runtime.manager.store.read().active
        assert active.values.fax_backend == 'sip' and active.values.ami_password != 'changeme'
        assert open(_login_file(isolated_installation)).read() == f'api\n{active.values.ami_password}\n'


def test_a_password_in_the_environment_wins_for_both_containers(isolated_installation, monkeypatch):
    with _serve(monkeypatch, FAX_BACKEND='sip', ASTERISK_AMI_PASSWORD='synthetic-from-env') as client:
        values = client.app.state.configuration_runtime.manager.store.read().active.values
        assert values.ami_password == 'synthetic-from-env'
        # Only the inbound secret was created; the login came from the environment.
        assert _latest_actor(client).count('system') == 1
        assert open(_login_file(isolated_installation)).read() == 'api\nsynthetic-from-env\n'


def test_an_installation_already_using_its_own_asterisk_keeps_its_password(isolated_installation, monkeypatch):
    # An earlier release: the SIP trunk was in use with the shipped password.
    original = ConfigurationRuntime._create_engine_password
    monkeypatch.setattr(ConfigurationRuntime, '_create_engine_password', lambda self, snapshot, **_: snapshot)
    with _serve(monkeypatch, FAX_BACKEND='sip'):
        pass
    monkeypatch.setattr(ConfigurationRuntime, '_create_engine_password', original)
    with _serve(monkeypatch, FAX_BACKEND='sip') as client:
        assert client.app.state.configuration_runtime.manager.store.read().active.values.ami_password == 'changeme'
        assert client.get('/admin/settings', headers=ADMIN).json()['sip']['ami_password_is_default'] is True


def test_a_cloud_installation_creates_no_login(isolated_installation, monkeypatch):
    with _serve(monkeypatch) as client:
        assert client.app.state.configuration_runtime.manager.store.read().active.values.ami_password == 'changeme'
        assert not os.path.exists(_login_file(isolated_installation))


def test_only_logins_start_sh_accepts_are_written(tmp_path):
    from app.config_values import ConfigurationValues
    usable = ConfigurationValues.from_environment({'FAX_DATA_DIR': str(tmp_path), 'ASTERISK_AMI_PASSWORD': 'Abc_123-x'})
    assert sip_trunk.write_manager_credentials(usable).read_text() == 'api\nAbc_123-x\n'
    assert sip_trunk.manager_credentials_shared(usable)
    for password in ('semi;colon', ' padded', 'back\\slash'):
        values = ConfigurationValues.from_environment({'FAX_DATA_DIR': str(tmp_path / 'other'),
                                                       'ASTERISK_AMI_PASSWORD': password})
        assert sip_trunk.write_manager_credentials(values) is None
