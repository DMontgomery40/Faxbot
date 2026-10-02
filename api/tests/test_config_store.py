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
