"""Internal configuration access on isolated SQL, never HTTP handlers."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from importlib import import_module
import json
from threading import Event, Thread

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_policy import NOW
from api.tests.test_access_configuration_writes import ConfigurationWorld
from api.app.access.catalog import PERMISSIONS
from api.app.access.mutation_types import MutationDeniedError, MutationReason
from api.app.access.policy import AccessControl
from api.app.access.store import AccessStore
from api.app.access.types import AuthenticationError, InvalidTransactionError, StaleCredentialError
from api.app.config_store import ConfigurationStore, ConfigurationConflict


def boundary_module():
    try:
        return import_module('api.app.access.configuration_access')
    except ModuleNotFoundError:
        pytest.fail('The required configuration access facade is missing')


@pytest.fixture
def world(database, tmp_path, monkeypatch):
    from api.app import config_store
    monkeypatch.setattr(config_store, '_utc_now', lambda: NOW)
    return ConfigurationWorld(database, tmp_path)


def access(world, *, clock=None):
    return boundary_module().AuthorizedConfiguration(world.configuration, world.control,
        clock=clock or (lambda: NOW))


def assert_denied_audit(world, reason, *, operation='settings.update', attributed=True):
    audit = world.audits()[-1]
    assert audit['operation'] == operation and audit['outcome'] == 'denied'
    assert audit['target_kind'] == 'installation' and audit['target_id'] == 'installation'
    assert audit['policy_version_before'] == audit['policy_version_after'] == 1
    assert json.loads(audit['details']) == {'reason': reason}
    assert audit['actor_principal_id'] == ('editor' if attributed else None)
    assert audit['actor_session_id'] == ('session-editor' if attributed else None)
    assert audit['actor_key_binding_id'] is None


def test_read_views_require_their_own_permission(world):
    world.grant(['settings:read'])
    facade = access(world)
    snapshot = facade.settings(world.actor)
    assert snapshot == world.initial
    # The snapshot is internal data. Safe transport projection is a separate owner.
    assert snapshot.desired.values.phaxio_api_key == 'synthetic-provider-secret'
    with pytest.raises(MutationDeniedError) as failure:
        facade.providers(world.actor)
    assert failure.value.reason == MutationReason.FORBIDDEN
    world.role('provider-reader', ['providers:read'])
    world.assignment('editor', 'provider-reader')
    assert facade.providers(world.actor) == world.initial
    assert world.audits() == []


@pytest.mark.parametrize('view,prepare,permission', [
    ('settings', 'prepare_settings_write', 'settings:write'),
    ('providers', 'prepare_provider_write', 'providers:write'),
])
def test_write_only_preflight_succeeds_without_read_or_duplicate_audit(world, view, prepare, permission):
    world.grant([permission])
    facade = access(world)
    with pytest.raises(MutationDeniedError):
        getattr(facade, view)(world.actor)
    assert getattr(facade, prepare)(world.actor, world.initial, world.initial.desired.id) is None
    assert world.audits() == []
    if view == 'settings':
        result = world.manager.patch_authorized(world.initial, {}, principal=world.actor, control=world.control)
    else:
        result = world.manager.patch_plugin_authorized(world.initial, 'phaxio',
            principal=world.actor, control=world.control)
    assert result == world.initial
    audit, = world.audits()
    assert audit['outcome'] == 'allowed' and json.loads(audit['details'])['changed'] is False


@pytest.mark.parametrize('prepare,permission,operation', [
    ('prepare_settings_write', 'settings:read', 'settings.update'),
    ('prepare_provider_write', 'providers:read', 'providers.configure'),
])
def test_read_only_grant_cannot_prepare_write(world, prepare, permission, operation):
    world.grant([permission])
    before = world.business()
    with pytest.raises(MutationDeniedError):
        getattr(access(world), prepare)(world.actor, world.initial)
    assert world.business() == before
    assert_denied_audit(world, 'forbidden', operation=operation)


def test_base_denial_precedes_editor_metadata_and_snapshot_read(world, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('A denied actor must not reach configuration metadata')
    monkeypatch.setattr(world.configuration, '_head', unexpected)
    with pytest.raises(MutationDeniedError):
        access(world).prepare_settings_write(world.actor, object(), 'synthetic-private-revision')
    assert_denied_audit(world, 'forbidden')
    assert 'synthetic-private' not in world.audits()[-1]['details']


@pytest.mark.parametrize('conflict', ['supplied_revision', 'generation', 'installation', 'desired'])
def test_preflight_checks_supplied_revision_and_canonical_expected_consistency(world, conflict):
    world.grant(['settings:write'])
    expected, supplied = world.initial, None
    if conflict == 'supplied_revision':
        supplied = 'synthetic-private-revision'
    elif conflict == 'generation':
        expected = replace(expected, generation=expected.generation + 1)
    elif conflict == 'installation':
        expected = replace(expected, installation_id='synthetic-other-installation')
    else:
        expected = replace(expected, active=replace(expected.active, id='synthetic-other-revision'))
    before = world.business()
    with pytest.raises(ConfigurationConflict):
        access(world).prepare_settings_write(world.actor, expected, supplied)
    assert world.business() == before
    assert_denied_audit(world, 'configuration_conflict')
    assert 'synthetic-' not in world.audits()[-1]['details']


@pytest.mark.parametrize('mutation', ['principal_disable', 'session_revoke', 'session_expire',
    'password_rotate', 'reset_required', 'grant_remove', 'key_rotate'])
def test_current_source_or_grant_change_precedes_revision_conflict(world, mutation):
    assignment = world.grant(['settings:write', 'settings:read'])
    facade = access(world)
    actor = world.key() if mutation == 'key_rotate' else world.actor
    if mutation != 'key_rotate':
        assert facade.settings(actor) == world.initial
    if mutation == 'principal_disable':
        world.update('access_principals', 'editor', enabled=0)
    elif mutation == 'session_revoke':
        world.update('access_sessions', 'session-editor', revoked_at=NOW)
    elif mutation == 'session_expire':
        world.update('access_sessions', 'session-editor', expires_at=NOW)
    elif mutation == 'password_rotate':
        world.update('access_users', 'editor', password_version=2)
    elif mutation == 'reset_required':
        world.update('access_users', 'editor', password_change_required=1)
    elif mutation == 'grant_remove':
        table = world.tables['access_assignments']
        with world.engine.begin() as connection:
            connection.execute(table.delete().where(table.c.id == assignment))
    else:
        world.update('access_key_bindings', 'editor-key', security_version=2)
    ordinary = mutation in {'reset_required', 'grant_remove'}
    error = MutationDeniedError if ordinary else StaleCredentialError
    with pytest.raises(error):
        facade.prepare_settings_write(actor, world.initial, 'synthetic-wrong-revision')
    reason = 'reset_required' if mutation == 'reset_required' else 'forbidden' if ordinary else 'credential_stale'
    assert_denied_audit(world, reason, attributed=ordinary)
    with pytest.raises(error):
        facade.settings(actor)
    assert len(world.audits()) == 1


def test_unverified_context_has_no_audit_attribution(world):
    world.grant(['settings:write'])
    with pytest.raises(StaleCredentialError):
        access(world).prepare_settings_write(object(), world.initial)
    assert_denied_audit(world, 'credential_stale', attributed=False)


@pytest.mark.parametrize('pending_enabled', [False, True])
def test_provider_preflight_refuses_disabled_active_feature_with_audit(world, pending_enabled):
    world.grant(['providers:write', 'settings:write'])
    disabled = world.manager.patch(world.initial, {'feature_v3_plugins': False}, actor='trusted-fixture')
    expected = world.manager.patch(disabled, {'feature_v3_plugins': True, 'enable_mcp_http': True},
        actor='trusted-fixture') if pending_enabled else disabled
    assert expected.active.values.feature_v3_plugins is False
    before = world.business()
    facade = access(world)
    with pytest.raises(MutationDeniedError) as failure:
        facade.prepare_provider_write(world.actor, expected, expected.desired.id)
    assert failure.value.reason == MutationReason.FORBIDDEN
    assert world.business() == before
    assert_denied_audit(world, 'forbidden', operation='providers.configure')
    assert facade.prepare_settings_write(world.actor, expected) is None
    assert len(world.audits()) == 1


def test_pending_feature_disable_does_not_disable_active_provider_admission(world):
    world.grant(['providers:write'])
    pending = world.manager.patch(world.initial, {'feature_v3_plugins': False, 'enable_mcp_http': True},
        actor='trusted-fixture')
    assert pending.active.values.feature_v3_plugins is True
    assert pending.desired.values.feature_v3_plugins is False
    assert access(world).prepare_provider_write(world.actor, pending, pending.desired.id) is None
    assert world.audits() == []


def test_key_session_ceiling_is_live_and_independent_for_read_and_write(world):
    world.grant(PERMISSIONS)
    actor = world.key(session=True, ceiling=('providers:read', 'settings:write'))
    facade = access(world)
    assert facade.providers(actor) == world.initial
    with pytest.raises(MutationDeniedError):
        facade.settings(actor)
    assert facade.prepare_settings_write(actor, world.initial) is None
    with pytest.raises(MutationDeniedError):
        facade.prepare_provider_write(actor, world.initial)
    assert json.loads(world.audits()[-1]['details']) == {'reason': 'forbidden'}
    assert world.audits()[-1]['actor_key_binding_id'] == 'editor-key'
    assert world.audits()[-1]['actor_session_id'] == 'key-session'
    table = world.tables['access_key_grants']
    with world.engine.begin() as connection:
        connection.execute(table.delete().where(table.c.key_binding_id == 'editor-key',
            table.c.permission_id == 'settings:write'))
    with pytest.raises(MutationDeniedError):
        facade.prepare_settings_write(actor, world.initial)
    assert len(world.audits()) == 2


def test_foreign_access_store_is_rejected_at_construction(world):
    foreign = AccessControl(AccessStore(world.engine))
    with pytest.raises(InvalidTransactionError):
        boundary_module().AuthorizedConfiguration(world.configuration, foreign)
    assert world.audits() == []


@pytest.mark.parametrize('changed_pair', ['control', 'configuration'])
def test_each_entry_point_rejects_replaced_store_pairing(world, changed_pair):
    world.grant(PERMISSIONS)
    facade = access(world)
    foreign = AccessStore(world.engine)
    if changed_pair == 'control':
        world.control.store = foreign
    else:
        world.configuration.access_store = foreign
    for method, args in [('settings', (world.actor,)), ('providers', (world.actor,)),
            ('prepare_settings_write', (world.actor, world.initial)),
            ('prepare_provider_write', (world.actor, world.initial))]:
        with pytest.raises(InvalidTransactionError):
            getattr(facade, method)(*args)
    assert world.audits() == []


def test_read_and_preflight_share_both_locks_before_clock_and_snapshot(world, monkeypatch):
    world.grant(['settings:read', 'settings:write'])
    connections, sampled = [], []
    real_lock, real_snapshot = world.store.lock_on, world.configuration._snapshot
    def lock(connection):
        world.configuration._require_lock_on(connection)
        result = real_lock(connection)
        connections.append(connection)
        return result
    def clock():
        connection = connections[-1]
        world.configuration._require_lock_on(connection)
        world.store.require_lock_on(connection)
        sampled.append(connection)
        return NOW
    def snapshot(connection, cipher, head):
        assert connection is sampled[-1]
        world.configuration._require_lock_on(connection)
        world.store.require_lock_on(connection)
        return real_snapshot(connection, cipher, head)
    monkeypatch.setattr(world.store, 'lock_on', lock)
    monkeypatch.setattr(world.configuration, '_snapshot', snapshot)
    facade = access(world, clock=clock)
    assert facade.settings(world.actor) == world.initial
    assert facade.prepare_settings_write(world.actor, world.initial) is None
    assert len(sampled) == 2 and all(connection.closed for connection in sampled)
    assert world.audits() == []


def test_waiting_preflight_samples_source_expiry_after_access_lock(world, monkeypatch):
    world.grant(['settings:write'])
    contender = ConfigurationStore(world.engine, world.configuration.key_path)
    facade = boundary_module().AuthorizedConfiguration(contender, AccessControl(contender.access_store),
        clock=lambda: moment[0])
    moment, started, errors = [NOW], Event(), []
    real_locked = contender._locked
    @contextmanager
    def observed():
        started.set()
        with real_locked() as connection:
            yield connection
    monkeypatch.setattr(contender, '_locked', observed)
    def run():
        try:
            facade.prepare_settings_write(world.actor, world.initial)
        except Exception as error:
            errors.append(error)
    with world.store.transaction():
        worker = Thread(target=run)
        worker.start()
        assert started.wait(5)
        moment[0] = NOW + timedelta(hours=2)
    worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], StaleCredentialError)
    assert_denied_audit(world, 'credential_stale', attributed=False)


def test_successful_preflight_is_not_authority_for_final_commit(world):
    assignment = world.grant(['settings:write'])
    assert access(world).prepare_settings_write(world.actor, world.initial) is None
    table = world.tables['access_assignments']
    with world.engine.begin() as connection:
        connection.execute(table.delete().where(table.c.id == assignment))
    before = world.business()
    with pytest.raises(MutationDeniedError):
        world.manager.patch_authorized(world.initial, {'fax_header': 'must-not-save'},
            principal=world.actor, control=world.control)
    assert world.business() == before and len(world.audits()) == 1
    assert_denied_audit(world, 'forbidden')


def test_late_snapshot_failure_is_not_classified_as_source_denial(world, monkeypatch):
    world.grant(['settings:write'])
    real_current = world.configuration._checked_current_on
    def unavailable(connection, expected):
        real_current(connection, expected)
        raise AuthenticationError()
    monkeypatch.setattr(world.configuration, '_checked_current_on', unavailable)
    with pytest.raises(AuthenticationError) as failure:
        access(world).prepare_settings_write(world.actor, world.initial)
    assert type(failure.value) is AuthenticationError
    assert world.audits() == []


def test_denial_audit_failure_rolls_back_instead_of_committing_partial_audit(world, monkeypatch):
    real_audit = world.configuration._audit_configuration_on
    def fail_after_insert(*args, **kwargs):
        real_audit(*args, **kwargs)
        raise StaleCredentialError()
    monkeypatch.setattr(world.configuration, '_audit_configuration_on', fail_after_insert)
    before = world.business()
    with pytest.raises(StaleCredentialError):
        access(world).prepare_settings_write(world.actor, world.initial)
    assert world.business() == before and world.audits() == []


def test_write_receipt_is_closed_for_noop_applied_and_pending(world):
    receipt = boundary_module().configuration_write_receipt
    assert receipt(world.initial, world.initial) == {'ok': True, 'changed': False, '_meta': {
        'active_revision_id': world.initial.active.id, 'desired_revision_id': world.initial.active.id,
        'generation': 1, 'apply_state': 'applied', 'restart_recommended': False}}
    world.assignment('editor', 'role_owner')
    applied = world.manager.patch_authorized(world.initial, {'fax_header': 'synthetic-private-header'},
        principal=world.actor, control=world.control)
    assert receipt(world.initial, applied) == {'ok': True, 'changed': True, '_meta': {
        'active_revision_id': applied.active.id, 'desired_revision_id': applied.active.id,
        'generation': 2, 'apply_state': 'applied', 'restart_recommended': False}}
    pending = world.manager.patch_authorized(applied, {'enable_mcp_http': True},
        principal=world.actor, control=world.control)
    result = receipt(applied, pending)
    assert result == {'ok': True, 'changed': True, '_meta': {
        'active_revision_id': applied.active.id, 'desired_revision_id': pending.pending.id,
        'generation': 3, 'apply_state': 'pending_restart', 'restart_recommended': True}}
    assert 'synthetic-' not in json.dumps(result)
    assert receipt(pending, pending)['changed'] is False
    assert receipt(pending, pending)['_meta']['apply_state'] == 'pending_restart'


def test_receipt_acknowledges_bootstrap_rotation_after_source_becomes_stale(world):
    from api.app.access.bootstrap import BootstrapCredentials
    from api.app.config_secrets import load_installation_key
    active = world.manager.patch(world.initial, {'api_key': 'synthetic-A'}, actor='trusted-fixture')
    bootstrap = BootstrapCredentials(world.configuration, installation_key=load_installation_key(
        world.configuration.key_path, allow_create=False))
    world.control = AccessControl(world.store, current_bootstrap_fingerprint_on=bootstrap.current_fingerprint_on)
    with world.store.transaction() as connection:
        actor = bootstrap.authenticate_on(connection, 'synthetic-A')
    facade = access(world)
    assert facade.prepare_settings_write(actor, active, active.desired.id) is None
    changed = world.manager.patch_authorized(active, {'api_key': 'synthetic-B'},
        principal=actor, control=world.control)
    assert boundary_module().configuration_write_receipt(active, changed) == {'ok': True,
        'changed': True, '_meta': {'active_revision_id': changed.active.id,
            'desired_revision_id': changed.active.id, 'generation': 3, 'apply_state': 'applied',
            'restart_recommended': False}}
    with pytest.raises(StaleCredentialError):
        facade.settings(actor)
    audit, = world.audits()
    assert audit['outcome'] == 'allowed' and audit['actor_principal_id'] == 'bootstrap'


@pytest.mark.parametrize('malformed', ['before', 'after', 'generation', 'revision'])
def test_receipt_rejects_unvalidated_snapshot_shape(world, malformed):
    before, after = world.initial, world.initial
    if malformed == 'before':
        before = object()
    elif malformed == 'after':
        after = {'generation': 1}
    elif malformed == 'generation':
        after = replace(after, generation=True)
    else:
        after = replace(after, active=object())
    with pytest.raises(ValueError):
        boundary_module().configuration_write_receipt(before, after)
