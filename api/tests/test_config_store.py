"""Canonical configuration transactions on SQLite and PostgreSQL."""
import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.config_values import ConfigurationValues
from api.app.config_secrets import ConfigurationSecretError
from api.app.config_store import ConfigurationStore, ConfigurationConflict, ConfigurationCommitUncertain


def make_store(database, tmp_path):
    upgrade_schema(database)
    return ConfigurationStore(database, tmp_path / 'configuration.key')


def test_first_import_is_encrypted_and_subsequent_initialization_uses_canonical_state(database, tmp_path):
    store = make_store(database, tmp_path)
    original = ConfigurationValues.from_environment({'PHAXIO_API_SECRET': 'synthetic-original-secret'})
    first = store.initialize(original, actor='test')
    second = ConfigurationStore(database, tmp_path / 'configuration.key').initialize(
        original.with_patch({'phaxio_api_secret': 'different-secret'}), actor='test')
    assert second == first
    assert first.active.values.phaxio_api_secret == 'synthetic-original-secret'
    with database.connect() as connection:
        rows = connection.execute(sa.text('SELECT envelope FROM configuration_revisions')).scalars().all()
    assert len(rows) == 1
    assert 'synthetic-original-secret' not in rows[0]
    (tmp_path / 'configuration.key').unlink()
    with pytest.raises(ConfigurationSecretError):
        ConfigurationStore(database, tmp_path / 'configuration.key').initialize(original, actor='test')
    assert not (tmp_path / 'configuration.key').exists()


def test_durable_apply_stages_whole_candidate_and_rejects_stale_editors(database, tmp_path):
    store = make_store(database, tmp_path)
    first = store.initialize(ConfigurationValues.from_environment({}), actor='test')
    hot = store.apply(first, first.desired.values.with_patch({'fax_header': 'changed'}),
                      restart_required=False, actor='test')
    assert hot.active.values.fax_header == 'changed'
    assert hot.pending is None
    with pytest.raises(ConfigurationConflict):
        store.apply(first, first.desired.values.with_patch({'fax_header': 'stale'}),
                    restart_required=False, actor='test')
    staged = store.apply(hot, hot.desired.values.with_patch({'fax_header': 'pending', 'enable_mcp_http': True}),
                         restart_required=True, actor='test')
    assert staged.active == hot.active
    assert staged.desired.values.fax_header == 'pending'
    assert store.read() == staged
    edited = store.apply(staged, staged.desired.values.with_patch({'fax_header': 'pending-two'}),
                         restart_required=True, actor='test')
    with pytest.raises(ConfigurationConflict):
        store.apply(staged, staged.desired.values.with_patch({'fax_header': 'pending-stale'}),
                    restart_required=True, actor='test')
    activated = store.apply(edited, edited.desired.values.with_patch({'enable_mcp_http': False}),
                            restart_required=False, actor='test')
    assert activated.pending is None
    assert activated.active.values.fax_header == 'pending-two'
    assert store.apply(activated, activated.desired.values, restart_required=False, actor='test') == activated


@pytest.mark.parametrize('committed', [False, True])
def test_failed_commit_acknowledgement_can_be_reconciled_without_repeating_write(database, tmp_path, monkeypatch, committed):
    store = make_store(database, tmp_path)
    first = store.initialize(ConfigurationValues.from_environment({}), actor='test')
    original_commit = sa.engine.Connection.commit
    def lose_ack(connection):
        if committed:
            original_commit(connection)
        raise sa.exc.OperationalError(None, None, RuntimeError('synthetic-secret-database-failure'))
    monkeypatch.setattr(sa.engine.Connection, 'commit', lose_ack)
    with pytest.raises(ConfigurationCommitUncertain) as caught:
        store.apply(first, first.desired.values.with_patch({'fax_header': 'durable'}),
                    restart_required=False, actor='test')
    assert 'synthetic-secret' not in str(caught.value)
    monkeypatch.setattr(sa.engine.Connection, 'commit', original_commit)
    actual = store.read()
    assert actual.active.values.fax_header == ('durable' if committed else first.active.values.fax_header)
    with database.connect() as connection:
        assert connection.scalar(sa.text('SELECT count(*) FROM configuration_revisions')) == (2 if committed else 1)


def test_separate_processes_cannot_overwrite_the_same_revision(database, tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time
    store = make_store(database, tmp_path)
    first = store.initialize(ConfigurationValues.from_environment({}), actor='test')
    with database.connect() as connection:
        namespace = connection.scalar(sa.text('SELECT current_schema()')) if database.dialect.name == 'postgresql' else ''
    environment = {**os.environ, 'TEST_DATABASE_URL': database.url.render_as_string(hide_password=False),
                   'TEST_SCHEMA': namespace, 'PYTHONPATH': str(Path(__file__).resolve().parents[2])}
    script = '''
import os, sys, time
from pathlib import Path
from api.app.schema import create_database_engine
from api.app.config_store import ConfigurationStore, ConfigurationConflict
options = {'options': '-csearch_path='+os.environ['TEST_SCHEMA']} if os.environ['TEST_SCHEMA'] else {}
engine = create_database_engine(os.environ['TEST_DATABASE_URL'], connect_args=options)
store = ConfigurationStore(engine, sys.argv[1])
original = store.read()
Path(sys.argv[2]).touch()
deadline = time.monotonic()+20
while not Path(sys.argv[3]).exists():
    if time.monotonic()>deadline: raise RuntimeError('test barrier timeout')
    time.sleep(0.01)
try:
    store.apply(original, original.desired.values.with_patch({'fax_header': sys.argv[4]}), restart_required=False, actor='test')
    print('applied')
except ConfigurationConflict:
    print('conflict')
finally:
    engine.dispose()
'''
    gate = tmp_path / 'go'
    ready = [tmp_path / ('ready-' + str(i)) for i in range(2)]
    children = [subprocess.Popen([sys.executable, '-c', script, str(store.key_path), str(ready[i]), str(gate), 'editor-'+str(i)],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
    try:
        deadline = time.monotonic() + 20
        while not all(path.exists() for path in ready):
            assert all(child.poll() is None for child in children), 'Child configuration reader failed'
            assert time.monotonic() < deadline, 'Configuration read barrier timed out'
            time.sleep(0.01)
        gate.touch()
        results = [child.communicate(timeout=20) for child in children]
        assert all(child.returncode == 0 for child in children)
        assert sorted(stdout.strip() for stdout, _ in results) == ['applied', 'conflict']
        assert store.read().generation == first.generation + 1
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate()


def test_provider_rotation_retains_encrypted_original_profile_and_manifest(database, tmp_path):
    from api.app.config_profiles import ProviderConfiguration
    store = make_store(database, tmp_path)
    manifest = {'id': 'synthetic-provider', 'actions': {'send_fax': {'url': 'https://original.example/fax'}}}
    original = ProviderConfiguration('synthetic-provider', credentials={'api_key': 'original-secret'},
                                      settings={'sender': '+15551230001'}, traits={'requires_tiff': False}, manifest=manifest)
    first = store.initialize(ConfigurationValues.from_environment({}), actor='test',
                             providers={'outbound': original}, plugins={'inactive': {'settings': {'api_key': 'saved-secret'}}})
    old_id = first.active.profile_id('outbound')
    old = store.read_profile(old_id)
    manifest['actions']['send_fax']['url'] = 'https://changed.example/fax'
    assert old.configuration.manifest['actions']['send_fax']['url'] == 'https://original.example/fax'
    rotated = ProviderConfiguration('synthetic-provider', credentials={'api_key': 'rotated-secret'},
                                     settings={'sender': '+15551230001'}, traits={'requires_tiff': False}, manifest=manifest)
    second = store.apply(first, first.desired.values, restart_required=False, actor='test', providers={'outbound': rotated})
    new = store.read_profile(second.active.profile_id('outbound'))
    assert new.id != old.id
    assert new.account_id != old.account_id
    assert new.configuration.manifest_digest != old.configuration.manifest_digest
    assert store.read_profile(old_id) == old
    assert second.active.plugins.as_dict() == {'inactive': {'settings': {'api_key': 'saved-secret'}}}
    unchanged_profile = store.apply(second, second.desired.values.with_patch({'fax_header': 'unrelated'}),
                                     restart_required=False, actor='test', providers={'outbound': rotated})
    assert unchanged_profile.active.profile_id('outbound') == new.id
    with database.connect() as connection:
        envelopes = connection.execute(sa.text('SELECT envelope FROM provider_profiles')).scalars().all()
        assert len(envelopes) == 2
        assert all('original-secret' not in text and 'rotated-secret' not in text for text in envelopes)


def test_staged_provider_does_not_replace_active_and_disabled_role_has_no_fallback(database, tmp_path):
    from api.app.config_profiles import ProviderConfiguration
    store = make_store(database, tmp_path)
    outbound = ProviderConfiguration('phaxio', credentials={'key': 'outbound'})
    inbound = ProviderConfiguration('sinch', credentials={'key': 'inbound'})
    first = store.initialize(ConfigurationValues.from_environment({}), actor='test', providers={'outbound': outbound, 'inbound': inbound})
    staged = store.apply(first, first.desired.values.with_patch({'enable_mcp_http': True}),
                         restart_required=True, actor='test', providers={'inbound': inbound})
    assert staged.active.profile_id('outbound') == first.active.profile_id('outbound')
    assert staged.desired.profile_id('outbound') is None
    assert staged.desired.profile_id('inbound') == first.active.profile_id('inbound')
    live = store.apply(staged, staged.desired.values.with_patch({'enable_mcp_http': False}), restart_required=False, actor='test')
    assert live.active.profile_id('outbound') is None
    assert live.active.profile_id('inbound') == first.active.profile_id('inbound')


def test_acceptance_keeps_original_profile_after_rotation_and_refuses_stale_preparation(database, tmp_path):
    from datetime import datetime
    from api.app.config_profiles import ProviderConfiguration
    store = make_store(database, tmp_path)
    original = ProviderConfiguration('phaxio', credentials={'api_key': 'original'})
    first = store.initialize(ConfigurationValues.from_environment({}), actor='test', providers={'outbound': original})
    now = datetime.utcnow()
    job = dict(id='accepted', to_number='+15551230001', file_name='test.txt', tiff_path='',
               status='queued', pages=1, created_at=now, updated_at=now)
    binding = store.accept_outbound(first.active, job)
    assert binding.configuration.credentials == {'api_key': 'original'}
    second = store.apply(first, first.desired.values, restart_required=False, actor='test',
                         providers={'outbound': ProviderConfiguration('sip', credentials={'password': 'new'}, traits={'requires_tiff': True})})
    assert store.outbound_profile('accepted') == binding
    with pytest.raises(ConfigurationConflict):
        store.accept_outbound(first.active, {**job, 'id': 'stale-prepared-pdf'})
    with database.connect() as connection:
        assert connection.scalar(sa.text('SELECT count(*) FROM fax_jobs')) == 1
        assert connection.execute(sa.text('SELECT revision_id,profile_id FROM fax_job_bindings')).one() == (first.active.id, binding.id)
        assert connection.scalar(sa.text("SELECT backend FROM fax_jobs WHERE id='accepted'")) == 'phaxio'


def test_failed_binding_insert_rolls_back_job_acceptance(database, tmp_path):
    from datetime import datetime
    from api.app.config_profiles import ProviderConfiguration
    from api.app.config_store import ConfigurationStoreError
    store = make_store(database, tmp_path)
    first = store.initialize(ConfigurationValues.from_environment({}), actor='test', providers={'outbound': ProviderConfiguration('phaxio')})
    def reject_binding(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO fax_job_bindings'):
            raise sa.exc.OperationalError(None, None, RuntimeError('synthetic binding failure'))
    sa.event.listen(database, 'before_cursor_execute', reject_binding)
    try:
        now = datetime.utcnow()
        with pytest.raises(ConfigurationStoreError):
            store.accept_outbound(first.active, dict(id='rollback', to_number='+15551230001', file_name='test.txt',
                tiff_path='', status='queued', created_at=now, updated_at=now))
    finally:
        sa.event.remove(database, 'before_cursor_execute', reject_binding)
    with database.connect() as connection:
        assert connection.scalar(sa.text('SELECT count(*) FROM fax_jobs')) == 0
        assert connection.scalar(sa.text('SELECT count(*) FROM fax_job_bindings')) == 0


def test_profile_metadata_cannot_be_reassigned_and_unbound_history_has_no_fallback(database, tmp_path):
    from api.app.config_profiles import ProviderConfiguration
    from api.app.config_store import UnboundProviderProfile
    store = make_store(database, tmp_path)
    first = store.initialize(ConfigurationValues.from_environment({}), actor='test', providers={'outbound': ProviderConfiguration('phaxio')})
    with pytest.raises(UnboundProviderProfile):
        store.outbound_profile('legacy-unbound')
    identity = first.active.profile_id('outbound')
    with database.begin() as connection:
        connection.execute(sa.text('UPDATE provider_profiles SET account_id=:account WHERE id=:identity'),
                           {'account': 'different-account', 'identity': identity})
    with pytest.raises(ConfigurationSecretError):
        store.read_profile(identity)


def test_connection_acquisition_failure_is_sanitized(database, tmp_path, monkeypatch):
    from api.app.config_store import ConfigurationStoreError
    store = make_store(database, tmp_path)
    first = store.initialize(ConfigurationValues.from_environment({}), actor='test')
    def fail_connect():
        raise sa.exc.OperationalError('private-sql', {'password': 'private-secret'}, RuntimeError('private-backend'))
    monkeypatch.setattr(database, 'connect', fail_connect)
    with pytest.raises(ConfigurationStoreError) as caught:
        store.apply(first, first.desired.values.with_patch({'fax_header': 'new'}), restart_required=False, actor='test')
    assert 'private' not in str(caught.value)


def test_rollback_failure_does_not_replace_uncertain_commit(database, tmp_path, monkeypatch):
    store = make_store(database, tmp_path)
    first = store.initialize(ConfigurationValues.from_environment({}), actor='test')
    def fail_commit(connection):
        raise sa.exc.OperationalError(None, None, RuntimeError('private commit failure'))
    def fail_rollback(connection):
        raise sa.exc.OperationalError(None, None, RuntimeError('private rollback failure'))
    monkeypatch.setattr(sa.engine.Connection, 'commit', fail_commit)
    monkeypatch.setattr(sa.engine.Connection, 'rollback', fail_rollback)
    with pytest.raises(ConfigurationCommitUncertain) as caught:
        store.apply(first, first.desired.values.with_patch({'fax_header': 'new'}), restart_required=False, actor='test')
    assert 'private' not in str(caught.value)


def test_pending_promotion_requires_stopped_installation_ownership_and_exact_candidate(database, tmp_path):
    from api.app.config_lifecycle import InstallationLifecycle
    store = make_store(database, tmp_path)
    first = store.initialize(ConfigurationValues.from_environment({}), actor='test')
    staged = store.apply(first, first.desired.values.with_patch({'enable_mcp_http': True}), restart_required=True, actor='test')
    serving = InstallationLifecycle(tmp_path).acquire()
    serving.mark_serving()
    try:
        with InstallationLifecycle(tmp_path) as rolling:
            with pytest.raises(ConfigurationConflict):
                store.promote_pending(staged, lifecycle=rolling)
            assert store.read() == staged
    finally:
        serving.close()
    with InstallationLifecycle(tmp_path) as stopped:
        newer = store.apply(staged, staged.desired.values.with_patch({'fax_header': 'newer'}), restart_required=True, actor='test')
        with pytest.raises(ConfigurationConflict):
            store.promote_pending(staged, lifecycle=stopped)
        active = store.promote_pending(newer, lifecycle=stopped)
        assert active.pending is None
        assert active.active == newer.pending
        stopped.mark_serving()
    assert store.read() == active
