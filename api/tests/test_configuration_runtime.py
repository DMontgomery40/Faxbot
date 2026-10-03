"""Internal lifecycle integration, separate from operator GUI acceptance."""
from pathlib import Path

import pytest

from app.config import settings
from app.config_runtime import ConfigurationRuntime
from app.config_store import ConfigurationConflict


@pytest.fixture
def environment(tmp_path):
    return {'DATABASE_URL': 'sqlite:///' + str(tmp_path / 'installation.db'),
            'FAX_DATA_DIR': str(tmp_path / 'data'), 'FAX_DISABLED': 'true',
            'FAXBOT_CONFIG_PATH': str(tmp_path / 'absent.json'),
            'FAXBOT_PROVIDERS_DIR': str(tmp_path / 'providers'),
            'API_KEY': 'synthetic-original', 'FAX_HEADER': 'original'}


def test_restart_uses_canonical_values_ignoring_changed_invalid_imports(environment):
    first = ConfigurationRuntime(environment).prepare()
    try:
        first.publish_ready()
        assert settings.api_key == 'synthetic-original'
        first.manager.patch(first.manager.store.read(), {'fax_header': 'durable'}, actor='test')
    finally:
        first.close()
    second = ConfigurationRuntime({**environment, 'API_KEY': '***', 'MAX_FILE_SIZE_MB': 'invalid',
        'ENABLE_PERSISTED_SETTINGS': 'true', 'PERSISTED_ENV_PATH': '/missing/unreadable'}).prepare()
    try:
        with second.frame(second.candidate):
            assert settings.fax_header == 'durable'
            assert settings.api_key == 'synthetic-original'
    finally:
        second.close()


def test_preparation_does_not_promote_until_resources_are_ready(environment):
    first = ConfigurationRuntime(environment).prepare()
    original = first.snapshot
    pending = first.manager.patch(original, {'artifact_ttl_days': 1, 'fax_header': 'pending'}, actor='test')
    first.close()
    candidate = ConfigurationRuntime(environment).prepare()
    try:
        assert candidate.candidate.id == pending.desired.id
        with candidate.frame(candidate.candidate):
            assert settings.fax_header == 'pending'
        assert candidate.manager.store.read().active == original.active
        # Failed resource preparation follows close, without calling publish_ready.
    finally:
        candidate.close()
    retry = ConfigurationRuntime(environment).prepare()
    try:
        assert retry.snapshot.pending == pending.pending
        retry.publish_ready()
        assert retry.manager.store.read().pending is None
        assert settings.fax_header == 'pending'
    finally:
        retry.close()


def test_rolling_worker_uses_active_and_keeps_pending(environment):
    first = ConfigurationRuntime(environment).prepare()
    try:
        first.publish_ready()
        pending = first.manager.patch(first.snapshot, {'artifact_ttl_days': 1}, actor='test')
        second = ConfigurationRuntime(environment).prepare()
        try:
            assert not second.lifecycle.can_promote
            assert second.candidate.id == pending.active.id
            assert second.manager.store.read().pending == pending.pending
        finally:
            second.close()
    finally:
        first.close()


def test_concurrent_edit_rejects_prepared_candidate(environment):
    first = ConfigurationRuntime(environment).prepare()
    pending = first.manager.patch(first.snapshot, {'artifact_ttl_days': 1}, actor='test')
    first.close()
    candidate = ConfigurationRuntime(environment).prepare()
    try:
        edited = candidate.manager.patch(pending, {'fax_header': 'new desired'}, actor='test')
        with pytest.raises(ConfigurationConflict):
            candidate.publish_ready()
        assert candidate.manager.store.read().desired == edited.desired
        assert not candidate.serving
    finally:
        candidate.close()


def test_first_import_cannot_silently_redirect_bootstrap_locations(environment, tmp_path):
    from app.config_bootstrap import ConfigurationBootstrapError
    persisted = tmp_path / 'legacy.env'
    persisted.write_text('FAX_DATA_DIR=' + str(tmp_path / 'other-data') + '\n')
    with pytest.raises(ConfigurationBootstrapError, match='Set deployment'):
        ConfigurationRuntime({**environment, 'ENABLE_PERSISTED_SETTINGS': 'true',
                              'PERSISTED_ENV_PATH': str(persisted)}).prepare()
    assert not (tmp_path / 'other-data').exists()
    assert not (Path(environment['FAX_DATA_DIR']) / '.configuration.key').exists()


@pytest.mark.parametrize('patch', [{'ami_password': 'new-synthetic-secret'}, {'fax_disabled': True}])
def test_restart_cannot_abandon_unresolved_sip_work(environment, patch):
    from datetime import datetime
    from app.config_activation import ConfigurationActivationError
    environment = {**environment, 'FAX_BACKEND': 'sip', 'FAX_DISABLED': 'false'}
    first = ConfigurationRuntime(environment).prepare()
    store = first.manager.store
    try:
        store.accept_outbound(first.snapshot.active, {'id': 'unresolved-sip',
            'to_number': '+15555550123', 'file_name': 'synthetic.pdf', 'tiff_path': 'synthetic.tiff',
            'status': 'queued', 'created_at': datetime.utcnow(), 'updated_at': datetime.utcnow()})
        staged = first.manager.patch(first.snapshot, patch, actor='test')
        assert staged.pending is not None
    finally:
        first.close()
    with pytest.raises(ConfigurationActivationError, match='drained or reconciled'):
        ConfigurationRuntime(environment).prepare()
    assert store.read().pending == staged.pending


def test_legacy_failed_status_does_not_release_unresolved_ami_delivery(environment):
    from datetime import datetime
    from app.config_activation import ConfigurationActivationError
    environment = {**environment, 'FAX_BACKEND': 'sip', 'FAX_DISABLED': 'false'}
    first = ConfigurationRuntime(environment).prepare()
    store = first.manager.store
    try:
        store.accept_outbound(first.snapshot.active, {'id': 'legacy-ambiguous',
            'to_number': '+15555550123', 'file_name': 'synthetic.pdf', 'tiff_path': 'synthetic.tiff',
            'status': 'queued', 'created_at': datetime.utcnow(), 'updated_at': datetime.utcnow()})
        with store._locked() as connection:
            connection.execute(store.jobs.update().values(status='failed'))
            deliveries = store.delivery_tables['outbound_deliveries']
            connection.execute(deliveries.update().values(state='reconciliation_required', dispatch_mode='legacy'))
        first.manager.patch(first.snapshot, {'ami_password': 'replacement'}, actor='test')
    finally:
        first.close()
    with pytest.raises(ConfigurationActivationError, match='drained or reconciled'):
        ConfigurationRuntime(environment).prepare()


def test_permanently_held_acceptance_does_not_pin_an_unused_ami_connection(environment):
    from datetime import datetime
    environment = {**environment, 'FAX_BACKEND': 'sip', 'FAX_DISABLED': 'true'}
    first = ConfigurationRuntime(environment).prepare()
    try:
        first.manager.store.accept_outbound(first.snapshot.active, {'id': 'held-job',
            'to_number': '+15555550123', 'file_name': 'synthetic.pdf', 'tiff_path': 'synthetic.tiff',
            'status': 'queued', 'created_at': datetime.utcnow(), 'updated_at': datetime.utcnow()})
        staged = first.manager.patch(first.snapshot, {'ami_password': 'replacement'}, actor='test')
    finally:
        first.close()
    candidate = ConfigurationRuntime(environment).prepare()
    try:
        assert candidate.candidate.id == staged.desired.id
    finally:
        candidate.close()


@pytest.mark.asyncio
async def test_actual_lifespan_releases_runtime_after_resource_failure(environment, monkeypatch):
    import os
    from app import main
    for key in list(os.environ):
        if key in main.ConfigurationValues.environment_keys():
            monkeypatch.delenv(key)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    async def fail(tasks):
        raise RuntimeError('synthetic owned resource failure')
    monkeypatch.setattr(main, '_initialize_runtime', fail)
    with pytest.raises(RuntimeError, match='synthetic owned resource failure'):
        async with main.lifespan(main.app):
            pytest.fail('Failed resource initialization became ready')
    assert not main.app.state.runtime_active
    assert main.app.state.configuration_runtime is None
    retry = ConfigurationRuntime(environment).prepare()
    try:
        assert retry.lifecycle.can_promote
    finally:
        retry.close()


@pytest.mark.asyncio
async def test_cancelled_startup_joins_ownership_work_before_cleanup():
    import asyncio
    import threading
    from app.config_runtime import run_lifecycle_step
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def operation():
        started.set()
        assert release.wait(2)
        finished.set()
    task = asyncio.create_task(run_lifecycle_step(operation))
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_shutdown_child_cancellation_preserves_startup_ownership_until_thread_finishes():
    import asyncio
    import threading
    from app.config_runtime import run_lifecycle_step
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    before = asyncio.all_tasks()
    def operation():
        started.set()
        assert release.wait(5)
        finished.set()
    task = asyncio.create_task(run_lifecycle_step(operation))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        for child in asyncio.all_tasks() - before:
            child.cancel()
        for _ in range(4):
            await asyncio.sleep(0)
        assert not task.done()
        assert not finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert finished.is_set()


def test_shared_serving_lease_refuses_database_upgrade_before_any_ddl(environment):
    import sqlalchemy as sa
    from app.config_bootstrap import ConfigurationBootstrapError
    from app.config_lifecycle import InstallationLifecycle
    from app.schema import create_database_engine
    from app.schema_configuration import frozen_metadata, REVISION

    engine = create_database_engine(environment['DATABASE_URL'])
    with engine.begin() as connection:
        frozen_metadata().create_all(connection)
        connection.exec_driver_sql('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)')
        connection.execute(sa.text('INSERT INTO alembic_version VALUES (:revision)'), {'revision': REVISION})
    Path(environment['FAX_DATA_DIR']).mkdir(parents=True, exist_ok=True)
    serving = InstallationLifecycle(Path(environment['FAX_DATA_DIR']))
    serving.acquire()
    serving.mark_serving()
    candidate = ConfigurationRuntime(environment)
    try:
        with pytest.raises(ConfigurationBootstrapError, match='Stop all Faxbot workers'):
            candidate.prepare()
        with engine.connect() as connection:
            assert connection.scalar(sa.text('SELECT version_num FROM alembic_version')) == REVISION
            assert 'outbound_deliveries' not in sa.inspect(connection).get_table_names()
    finally:
        candidate.close()
        serving.close()
        engine.dispose()
