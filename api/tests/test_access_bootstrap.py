"""Internal configuration/access atomicity on real isolated databases."""
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.config_lifecycle import InstallationLifecycle
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.config_secrets import load_installation_key
from api.app.access.policy import AccessControl
from api.app.access.types import (AuthenticationError, AccessUnavailableError,
                                  InvalidTransactionError, ResourceRef)


NOW = datetime(2026, 10, 3, 12)


@pytest.fixture
def bootstrap_world(database, tmp_path):
    upgrade_schema(database)
    store = ConfigurationStore(database, tmp_path / 'configuration.key')
    metadata = sa.MetaData()
    metadata.reflect(database, only=['access_principals', 'access_state',
                                     'access_sessions', 'access_audit'])
    return store, metadata.tables, tmp_path


def state(store, tables):
    with store.engine.connect() as connection:
        principal = connection.execute(sa.select(tables['access_principals']).where(
            tables['access_principals'].c.id == 'bootstrap')).mappings().one()
        version = connection.execute(sa.select(tables['access_state'].c.policy_version)).scalar_one()
        sessions = connection.execute(sa.select(tables['access_sessions'])).mappings().all()
        audit = connection.execute(sa.select(tables['access_audit'])).mappings().all()
    return dict(principal), version, [dict(row) for row in sessions], [dict(row) for row in audit]


def initialize(store, key='synthetic-bootstrap-A'):
    return store.initialize(ConfigurationValues.from_environment({'API_KEY': key}), actor='test')


def session(store, tables):
    principal, _, _, _ = state(store, tables)
    with store.engine.begin() as connection:
        connection.execute(tables['access_sessions'].insert().values(
            id='synthetic-session', principal_id='bootstrap', source_kind='bootstrap',
            bootstrap_fingerprint='a' * 64, token_hash='b' * 64, csrf_hash='c' * 64,
            principal_security_version=principal['security_version'],
            created_at=NOW, last_used_at=NOW, expires_at=NOW + timedelta(hours=12)))


def test_initial_bootstrap_activation_is_an_audited_epoch_change(bootstrap_world):
    store, tables, _ = bootstrap_world
    before = state(store, tables)
    initialize(store)
    after = state(store, tables)
    assert after[0]['security_version'] == before[0]['security_version'] + 1
    assert after[0]['version'] == before[0]['version'] + 1
    assert after[1] == before[1] + 1
    assert len(after[3]) == len(before[3]) + 1
    assert 'synthetic-bootstrap-A' not in repr(after)


def test_rotation_and_reintroduction_never_revive_old_bootstrap_sessions(bootstrap_world):
    store, tables, _ = bootstrap_world
    current = initialize(store)
    session(store, tables)
    before = state(store, tables)
    for index, key in enumerate(('synthetic-bootstrap-B', '', 'synthetic-bootstrap-A'), 1):
        current = store.apply(current, current.desired.values.with_patch({'api_key': key}),
                              restart_required=False, actor='test')
        after = state(store, tables)
        assert after[0]['security_version'] == before[0]['security_version'] + index
        assert after[0]['version'] == before[0]['version'] + index
        assert after[1] == before[1] + index
        assert after[2][0]['revoked_at'] is not None
        assert len(after[3]) == len(before[3]) + index


def test_pending_key_waits_for_promotion_and_unrelated_edits_preserve_sessions(bootstrap_world):
    store, tables, directory = bootstrap_world
    current = initialize(store)
    session(store, tables)
    before = state(store, tables)
    ordinary = store.apply(current, current.desired.values.with_patch({'fax_header': 'new header'}),
                           restart_required=False, actor='test')
    assert state(store, tables) == before
    pending = store.apply(ordinary, ordinary.desired.values.with_patch({'api_key': 'synthetic-bootstrap-B'}),
                          restart_required=True, actor='test')
    assert state(store, tables) == before
    with InstallationLifecycle(directory) as lifecycle:
        active = store.promote_pending(pending, lifecycle=lifecycle)
    after = state(store, tables)
    assert active.pending is None
    assert after[0]['security_version'] == before[0]['security_version'] + 1
    assert after[1] == before[1] + 1
    assert after[2][0]['revoked_at'] is not None


def test_failed_activation_audit_rolls_back_configuration_epoch_and_sessions(bootstrap_world):
    store, tables, _ = bootstrap_world
    current = initialize(store)
    session(store, tables)
    before = state(store, tables)

    def reject_audit(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('INSERT INTO ACCESS_AUDIT'):
            raise RuntimeError('synthetic audit failure')

    sa.event.listen(store.engine, 'before_cursor_execute', reject_audit)
    try:
        with pytest.raises(RuntimeError, match='synthetic audit failure'):
            store.apply(current, current.desired.values.with_patch({'api_key': 'synthetic-bootstrap-B'}),
                        restart_required=False, actor='test')
    finally:
        sa.event.remove(store.engine, 'before_cursor_execute', reject_audit)
    assert store.read() == current
    assert state(store, tables) == before


def credentials(store):
    from api.app.access.bootstrap import BootstrapCredentials
    return BootstrapCredentials(store, installation_key=load_installation_key(store.key_path, allow_create=False))


def test_bootstrap_source_reads_current_active_key_and_epoch_on_same_connection(bootstrap_world, monkeypatch):
    store, tables, _ = bootstrap_world
    current = initialize(store)
    source = credentials(store)
    writer = ConfigurationStore(store.engine, store.key_path)
    control = AccessControl(store.access_store, current_bootstrap_fingerprint_on=source.current_fingerprint_on)
    with store.access_store.transaction() as connection:
        # Once the transaction exists the reader must perform no key-file I/O
        # or second connection, even for policy's repeated source validation.
        with monkeypatch.context() as guard:
            def forbidden(*args, **kwargs):
                pytest.fail('bootstrap reader attempted an external read')
            guard.setattr(store, '_cipher', forbidden)
            guard.setattr(store.engine, 'connect', forbidden)
            actor = source.authenticate_on(connection, 'synthetic-bootstrap-A')
            assert control.is_complete_owner_on(connection, actor, now=datetime.utcnow())
    current = writer.apply(current, current.desired.values.with_patch({'api_key': 'synthetic-bootstrap-B'}),
                          restart_required=False, actor='test')
    with store.access_store.transaction() as connection:
        with pytest.raises(AuthenticationError):
            source.authenticate_on(connection, 'synthetic-bootstrap-A')
        assert source.authenticate_on(connection, 'synthetic-bootstrap-B').replay_scope == 'key:env'
    store.apply(current, current.desired.values.with_patch({'api_key': 'synthetic-bootstrap-A'}),
                restart_required=False, actor='test')
    with store.access_store.transaction() as connection:
        with pytest.raises(AuthenticationError):
            control.authorize_on(connection, actor, 'users:read', ResourceRef('installation'), now=datetime.utcnow())
        assert source.authenticate_on(connection, 'synthetic-bootstrap-A').principal_security_version > actor.principal_security_version


def test_bootstrap_reader_rejects_unlocked_or_wrong_store_connection(bootstrap_world):
    store, _, _ = bootstrap_world
    initialize(store)
    source = credentials(store)
    with store.engine.begin() as connection:
        with pytest.raises(InvalidTransactionError):
            source.current_fingerprint_on(connection)
    another = ConfigurationStore(store.engine, store.key_path)
    with another.access_store.transaction() as connection:
        with pytest.raises(InvalidTransactionError):
            source.current_fingerprint_on(connection)


def test_bootstrap_reader_rejects_wrong_key_and_disabled_or_empty_source(bootstrap_world):
    from cryptography.fernet import Fernet
    from api.app.access.bootstrap import BootstrapCredentials
    store, tables, _ = bootstrap_world
    current = initialize(store)
    wrong = BootstrapCredentials(store, installation_key=Fernet.generate_key())
    with store.access_store.transaction() as connection:
        with pytest.raises(AccessUnavailableError) as failure:
            wrong.current_fingerprint_on(connection)
        assert failure.value.__context__ is None
    source = credentials(store)
    with store.engine.begin() as connection:
        connection.execute(tables['access_principals'].update().where(
            tables['access_principals'].c.id == 'bootstrap').values(enabled=0))
    with store.access_store.transaction() as connection:
        with pytest.raises(AuthenticationError):
            source.authenticate_on(connection, 'synthetic-bootstrap-A')
    store.apply(current, current.desired.values.with_patch({'api_key': ''}), restart_required=False, actor='test')
    with store.access_store.transaction() as connection:
        assert source.current_fingerprint_on(connection) is None
        for invalid in ('', None, b'synthetic-bootstrap-A', '\ud800', 'x' * (1024 * 1024 + 1)):
            with pytest.raises(AuthenticationError):
                source.authenticate_on(connection, invalid)
    assert 'synthetic-bootstrap' not in repr(source)


def test_empty_initialization_and_same_key_promotion_preserve_access_state(bootstrap_world):
    store, tables, directory = bootstrap_world
    before = state(store, tables)
    current = initialize(store, '')
    assert state(store, tables) == before
    current = store.apply(current, current.desired.values.with_patch({'api_key': 'synthetic-bootstrap-A'}),
                          restart_required=False, actor='test')
    session(store, tables)
    before = state(store, tables)
    pending = store.apply(current, current.desired.values.with_patch({'fax_header': 'staged'}),
                          restart_required=True, actor='test')
    with InstallationLifecycle(directory) as lifecycle:
        store.promote_pending(pending, lifecycle=lifecycle)
    assert state(store, tables) == before


def test_access_lock_failure_does_not_publish_configuration_change(bootstrap_world):
    store, tables, _ = bootstrap_world
    current = initialize(store)
    before = state(store, tables)
    original = store.access_store.lock_on
    from api.app.access.types import AccessUnavailableError
    def unavailable(connection):
        original(connection)
        raise AccessUnavailableError()
    store.access_store.lock_on = unavailable
    with pytest.raises(RuntimeError, match='Cannot activate installation bootstrap state'):
        store.apply(current, current.desired.values.with_patch({'api_key': 'synthetic-bootstrap-B'}),
                    restart_required=False, actor='test')
    assert store.read() == current
    assert state(store, tables) == before


def test_uninitialized_or_corrupt_canonical_state_cannot_authenticate(bootstrap_world):
    from cryptography.fernet import Fernet
    from api.app.access.bootstrap import BootstrapCredentials
    store, _, _ = bootstrap_world
    empty = BootstrapCredentials(store, installation_key=Fernet.generate_key())
    with store.access_store.transaction() as connection:
        with pytest.raises(AccessUnavailableError):
            empty.authenticate_on(connection, 'synthetic-bootstrap-A')
    current = initialize(store)
    source = credentials(store)
    with store.engine.begin() as connection:
        connection.execute(store.revisions.update().where(store.revisions.c.id == current.active.id)
                           .values(envelope='synthetic-sensitive-invalid-envelope'))
    with store.access_store.transaction() as connection:
        with pytest.raises(AccessUnavailableError) as failure:
            source.authenticate_on(connection, 'synthetic-bootstrap-A')
    assert failure.value.__context__ is None
    assert 'synthetic' not in str(failure.value)


def test_bootstrap_secret_is_exact_and_fingerprint_is_installation_keyed(bootstrap_world):
    store, tables, _ = bootstrap_world
    initialize(store, ' synthetic-bootstrap-é ')
    source = credentials(store)
    with store.access_store.transaction() as connection:
        actor = source.authenticate_on(connection, ' synthetic-bootstrap-é ')
        for changed in ('synthetic-bootstrap-é', ' synthetic-bootstrap-e\u0301 '):
            with pytest.raises(AuthenticationError):
                source.authenticate_on(connection, changed)
        assert len(actor.credential.fingerprint) == 64
    import hashlib
    assert actor.credential.fingerprint != hashlib.sha256(' synthetic-bootstrap-é '.encode()).hexdigest()
    assert 'synthetic-bootstrap' not in repr(state(store, tables))


@pytest.mark.parametrize('mode', ['ordinary', 'pending', 'empty_initialization'])
def test_every_configuration_write_refuses_missing_access_state(bootstrap_world, mode):
    store, tables, _ = bootstrap_world
    current = None if mode == 'empty_initialization' else initialize(store)
    with store.engine.begin() as connection:
        connection.execute(tables['access_state'].delete())
        revisions = connection.execute(sa.select(store.revisions.c.id)).scalars().all()
    with pytest.raises(RuntimeError, match='Cannot activate installation bootstrap state'):
        if current is None:
            initialize(store, '')
        else:
            store.apply(current, current.desired.values.with_patch({'fax_header': 'changed'}),
                        restart_required=mode == 'pending', actor='test')
    with store.engine.connect() as connection:
        assert connection.execute(sa.select(store.revisions.c.id)).scalars().all() == revisions
    if current is not None:
        assert store.read() == current
