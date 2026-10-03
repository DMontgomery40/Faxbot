"""Canonical human edits on isolated SQL transactions, never HTTP handlers."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from threading import Event, Thread

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_policy import World, NOW
from api.app.config_activation import ConfigurationManager, ConfigurationActivationError
from api.app.config_bootstrap import default_plugin_state
from api.app.config_profiles import ConfigurationDocument
from api.app.config_store import (ConfigurationStore, ConfigurationConflict,
    ConfigurationStoreError, ConfigurationCommitUncertain)
from api.app.provider_catalog import ProviderCatalog, ProviderDefinition
from api.app.access.catalog import PERMISSIONS
from api.app.access.policy import AccessControl
from api.app.access.types import (InvalidTransactionError, StaleCredentialError,
    KeyEvidence, KeySessionEvidence, PrincipalContext)
from api.app.access.mutation_types import MutationDeniedError, MutationReason


def catalog(*, drift=False):
    definitions = {identity: ProviderDefinition(identity, ConfigurationDocument({
        'requires_ami': identity == 'sip', 'supports_inbound': True,
        'needs_storage': True, 'declaration_revision': 2 if drift else 1}), None, 'cloud')
        for identity in ('phaxio', 'sinch', 'sip')}
    definitions['custom'] = ProviderDefinition('custom', ConfigurationDocument({
        'requires_ami': False, 'supports_inbound': True}), ConfigurationDocument({
        'id': 'custom', 'actions': {'send_fax': {'url': 'https://synthetic.invalid/send'}},
        'config_schema': {'type': 'object'}}), 'cloud')
    return ProviderCatalog(definitions)


class ConfigurationWorld(World):
    def __init__(self, database, tmp_path):
        super().__init__(database)
        self.configuration = ConfigurationStore(database, tmp_path / 'configuration.key')
        self.store = self.configuration.access_store
        self.tables = self.store.tables
        self.control = AccessControl(self.store)
        self.catalog = catalog()
        self.manager = ConfigurationManager(self.configuration, catalog_loader=lambda values: self.catalog)
        self.initial = self.manager.initialize({'FAXBOT_CONFIG_PATH': str(tmp_path / 'absent.json'),
            'FEATURE_V3_PLUGINS': 'true', 'PHAXIO_API_KEY': 'synthetic-provider-secret'})
        self.actor = self.user('editor')

    def grant(self, permissions):
        role = self.role('editor-role', permissions)
        return self.assignment(self.actor.principal_id, role)

    def business(self):
        with self.engine.connect() as connection:
            return {table.name: [dict(row) for row in connection.execute(sa.select(table)).mappings()]
                for table in (self.configuration.state, self.configuration.revisions, self.configuration.profiles)}

    def audits(self):
        with self.engine.connect() as connection:
            table = self.tables['access_audit']
            return [dict(row) for row in connection.execute(sa.select(table).where(
                table.c.operation.in_(['settings.update', 'providers.configure']))).mappings()]

    def key(self, *, session=False, ceiling=('settings:write',)):
        self.insert('api_keys', id='editor-key', key_id='synthetic-public-id', name='synthetic',
            owner=None, scopes='[]', key_hash='synthetic-not-used-by-policy',
            expires_at=NOW + timedelta(hours=1), revoked_at=None)
        self.insert('access_key_bindings', id='editor-key', principal_id='editor', state='active',
            security_version=1, revoked_at=None)
        for permission in ceiling:
            self.insert('access_key_grants', key_binding_id='editor-key', permission_id=permission,
                resource_id='installation')
        evidence = KeyEvidence('editor-key', 1)
        if session:
            self.insert('access_sessions', id='key-session', principal_id='editor', source_kind='key',
                source_key_id='editor-key', source_key_version=1, bootstrap_fingerprint=None,
                token_hash='synthetic-key-session-hash', csrf_hash='synthetic-key-csrf-hash',
                principal_security_version=1, password_version=None,
                created_at=NOW - timedelta(minutes=5), last_used_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=30), revoked_at=None)
            evidence = KeySessionEvidence('key-session', 'editor-key', 1)
        return PrincipalContext('editor', 1, evidence, 'key:synthetic-public-id')


@pytest.fixture
def world(database, tmp_path, monkeypatch):
    from api.app import config_store
    monkeypatch.setattr(config_store, '_utc_now', lambda: NOW, raising=False)
    return ConfigurationWorld(database, tmp_path)


def patch(world, changes, *, expected=None, actor=None):
    method = getattr(world.manager, 'patch_authorized', None)
    assert callable(method), 'Canonical human settings edits need the required authorized interface'
    return method(expected or world.initial, changes, principal=actor or world.actor, control=world.control)


def test_denial_commits_safe_audit_without_a_candidate(world):
    before = world.business()
    with pytest.raises(MutationDeniedError) as failure:
        patch(world, {'fax_header': 'synthetic-secret-like-header'})
    assert failure.value.reason == MutationReason.FORBIDDEN
    assert world.business() == before
    audit, = world.audits()
    assert audit['outcome'] == 'denied' and audit['actor_principal_id'] == 'editor'
    assert json.loads(audit['details']) == {'reason': 'forbidden'}
    assert 'synthetic-secret' not in audit['details']


def test_noop_still_needs_operation_and_success_audits_once(world):
    with pytest.raises(MutationDeniedError):
        patch(world, {})
    world.grant(['settings:write'])
    before = world.business()
    assert patch(world, {}) == world.initial
    assert world.business() == before
    audit = world.audits()[-1]
    assert json.loads(audit['details'])['changed'] is False
    assert audit['policy_version_before'] == audit['policy_version_after'] == 1
    assert len(world.audits()) == 2


def test_header_derivation_is_ordinary_and_updates_captured_profile(world):
    world.grant(['settings:write'])
    changed = patch(world, {'fax_header': 'new header', 'fax_station_id': 'new station'})
    assert changed.generation == world.initial.generation + 1
    profile = world.configuration.read_profile(changed.active.profile_id('outbound'))
    assert profile.configuration.settings['fax_header'] == 'new header'
    assert profile.configuration.settings['fax_station_id'] == 'new station'
    audit, = world.audits()
    assert audit['policy_version_before'] == audit['policy_version_after'] == 1
    assert json.loads(audit['details'])['changed'] is True
    row = next(row for row in world.business()['configuration_revisions'] if row['id'] == changed.active.id)
    assert row['actor'] == 'principal:editor'


@pytest.mark.parametrize('permissions', [[], ['settings:write'], ['settings:write', 'providers:write'],
    ['settings:write', 'providers:write', 'owner:recover']])
def test_mixed_changes_require_union_and_immutable_owner(world, permissions):
    world.grant(permissions)
    before = world.business()
    with pytest.raises(MutationDeniedError):
        patch(world, {'fax_header': 'mixed', 'phaxio_api_key': 'new synthetic secret', 'api_key': 'new bootstrap'})
    assert world.business() == before
    assert len(world.audits()) == 1


def test_custom_all_permissions_role_is_not_complete_owner(world):
    world.grant(PERMISSIONS)
    with pytest.raises(MutationDeniedError) as failure:
        patch(world, {'api_key': 'synthetic-bootstrap'})
    assert failure.value.reason == MutationReason.OWNER_REQUIRED
    world.assignment('editor', 'role_owner')
    changed = patch(world, {'api_key': 'synthetic-bootstrap'})
    assert changed.active.values.api_key == 'synthetic-bootstrap'
    audit = world.audits()[-1]
    assert audit['policy_version_before'] == 1 and audit['policy_version_after'] == 2


def test_plugin_native_security_mapping_keeps_owner_guard(world):
    world.grant(['providers:write', 'owner:recover'])
    with pytest.raises(MutationDeniedError) as failure:
        world.manager.patch_plugin_authorized(world.initial, 'phaxio', settings={'verify_signature': False},
            principal=world.actor, control=world.control)
    assert failure.value.reason == MutationReason.OWNER_REQUIRED
    world.assignment('editor', 'role_owner')
    changed = world.manager.patch_plugin_authorized(world.initial, 'phaxio', settings={
        'verify_signature': False, 'api_key': None}, principal=world.actor, control=world.control)
    assert changed.active.values.phaxio_verify_signature is False
    assert changed.active.values.phaxio_api_key == 'synthetic-provider-secret'


def test_plugin_settings_and_role_enablement_need_provider_authority(world):
    world.grant(['settings:write'])
    before = world.business()
    with pytest.raises(MutationDeniedError):
        world.manager.patch_plugin_authorized(world.initial, 'custom', settings={
            'arbitrary-private-key': {'credentials': 'synthetic-plugin-secret'}},
            principal=world.actor, control=world.control)
    assert world.business() == before
    assert 'arbitrary-private-key' not in world.audits()[-1]['details']
    with pytest.raises(MutationDeniedError):
        world.manager.patch_plugin_authorized(world.initial, 'local', enabled=False,
            principal=world.actor, control=world.control)


def test_profile_only_catalog_drift_requires_provider_authority(world):
    world.grant(['settings:write'])
    world.catalog = catalog(drift=True)
    before = world.business()
    with pytest.raises(MutationDeniedError):
        patch(world, {})
    assert world.business() == before
    world.role('provider-role', ['providers:write'])
    world.assignment('editor', 'provider-role')
    changed = patch(world, {})
    assert changed.generation == 2
    profile = world.configuration.read_profile(changed.active.profile_id('outbound'))
    assert profile.configuration.traits['declaration_revision'] == 2


def test_plugin_preparation_loads_one_catalog_for_baseline_and_candidate(world):
    world.grant(['providers:write'])
    calls = []
    def load(values):
        calls.append(values)
        assert len(calls) == 1, 'A second catalog read could silently change the baseline'
        return world.catalog
    world.manager.catalog_loader = load
    changed = world.manager.patch_plugin_authorized(world.initial, 'custom', settings={'nested': {'a': 1}},
        principal=world.actor, control=world.control)
    assert changed.desired.plugins.as_dict()['settings']['custom'] == {'nested': {'a': 1}}
    assert len(calls) == 1


@pytest.mark.parametrize('mutation', ['disable', 'session_revoke', 'session_expire', 'reset',
    'remove_grant', 'disable_role', 'key_rotate', 'key_revoke'])
def test_prepared_actor_is_revalidated_before_noop_or_conflict(world, mutation):
    assignment = world.grant(['settings:write'])
    actor = world.key() if mutation.startswith('key_') else world.actor
    newer = world.manager.patch(world.initial, {'fax_header': 'winning editor'}, actor='trusted-fixture')
    if mutation == 'disable':
        world.update('access_principals', 'editor', enabled=0)
    elif mutation == 'session_revoke':
        world.update('access_sessions', 'session-editor', revoked_at=NOW)
    elif mutation == 'session_expire':
        world.update('access_sessions', 'session-editor', expires_at=NOW)
    elif mutation == 'reset':
        world.update('access_users', 'editor', password_change_required=1)
    elif mutation == 'remove_grant':
        with world.engine.begin() as connection:
            table = world.tables['access_assignments']
            connection.execute(table.delete().where(table.c.id == assignment))
    elif mutation == 'disable_role':
        world.update('access_roles', 'editor-role', enabled=0)
    elif mutation == 'key_rotate':
        world.update('access_key_bindings', 'editor-key', security_version=2)
    else:
        world.update('access_key_bindings', 'editor-key', revoked_at=NOW, state='revoked')
    before = world.business()
    error = MutationDeniedError if mutation in {'reset', 'remove_grant', 'disable_role'} else StaleCredentialError
    with pytest.raises(error):
        patch(world, {}, actor=actor)
    assert world.business() == before and world.configuration.read() == newer
    audit, = world.audits()
    if error is StaleCredentialError:
        assert audit['actor_principal_id'] is None
        assert audit['actor_key_binding_id'] is None
        assert audit['actor_session_id'] is None


def test_key_session_ceiling_is_current_and_not_principal_authority(world):
    world.grant(PERMISSIONS)
    world.assignment('editor', 'role_owner')
    actor = world.key(session=True)
    before = world.business()
    with pytest.raises(MutationDeniedError):
        patch(world, {'phaxio_api_key': 'synthetic-key-change'}, actor=actor)
    assert world.business() == before


def test_editor_conflict_is_audited_without_rebase(world):
    world.grant(['settings:write'])
    winner = world.manager.patch(world.initial, {'fax_header': 'winner'}, actor='trusted-fixture')
    before = world.business()
    with pytest.raises(ConfigurationConflict):
        patch(world, {'fax_header': 'loser'})
    assert world.business() == before and world.configuration.read() == winner
    assert json.loads(world.audits()[-1]['details']) == {'reason': 'configuration_conflict'}


@pytest.mark.parametrize('boundary', ['audit', 'after_profile', 'after_profile_stale'])
def test_allowed_candidate_and_profile_roll_back_with_failed_audit_or_late_write(world, monkeypatch, boundary):
    world.grant(['settings:write', 'providers:write'])
    before = world.business()
    if boundary == 'audit':
        def fail(*args, **kwargs):
            raise sa.exc.DatabaseError('synthetic-audit-failure', {}, Exception('private-backend-marker'))
        monkeypatch.setattr(world.configuration, '_audit_configuration_on', fail)
    else:
        original = world.configuration._select_profiles
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            if boundary == 'after_profile_stale':
                raise StaleCredentialError()
            raise ConfigurationStoreError('synthetic-after-profile-failure')
        monkeypatch.setattr(world.configuration, '_select_profiles', fail)
    expected = StaleCredentialError if boundary == 'after_profile_stale' else ConfigurationStoreError
    with pytest.raises(expected):
        patch(world, {'phaxio_api_key': 'synthetic-new-account'})
    assert world.business() == before and world.audits() == []


def test_wrong_control_store_cannot_supply_authority(world):
    world.grant(['settings:write'])
    wrong = AccessControl(World(world.engine).store)
    with pytest.raises(InvalidTransactionError):
        world.manager.patch_authorized(world.initial, {}, principal=world.actor, control=wrong)
    assert world.audits() == []


def test_private_seam_requires_configuration_and_same_store_access_locks(world):
    world.grant(['settings:write'])
    kwargs = dict(principal=world.actor, control=world.control, operation='settings.update',
        restart_required=False, providers={}, plugins=world.initial.desired.plugins,
        baseline_providers={})
    with world.store.transaction() as connection:
        with pytest.raises(InvalidTransactionError):
            world.configuration._apply_authorized_on(connection, world.initial, world.initial.desired.values, **kwargs)
    if world.engine.dialect.name == 'postgresql':
        for boundary in ('ROLLBACK;BEGIN', 'COMMIT;BEGIN', 'SELECT 1;ROLLBACK;BEGIN',
                         'ROLLBACK/*boundary*/;BEGIN/*boundary*/'):
            with world.configuration._locked() as connection:
                world.store.lock_on(connection)
                connection.exec_driver_sql(boundary)
                with pytest.raises(InvalidTransactionError):
                    world.configuration._require_lock_on(connection)
    with world.configuration._locked() as connection:
        world.configuration._require_lock_on(connection)
        with pytest.raises(InvalidTransactionError):
            world.configuration._apply_authorized_on(connection, world.initial, world.initial.desired.values, **kwargs)
        world.store.lock_on(connection)
        with connection.begin_nested():
            with pytest.raises(InvalidTransactionError):
                world.configuration._apply_authorized_on(connection, world.initial, world.initial.desired.values, **kwargs)
        connection.exec_driver_sql('ROLLBACK')
        connection.exec_driver_sql('BEGIN')
        with pytest.raises(InvalidTransactionError):
            world.configuration._require_lock_on(connection)
        with pytest.raises(InvalidTransactionError):
            world.configuration._apply_authorized_on(connection, world.initial, world.initial.desired.values, **kwargs)


def test_waiting_writer_samples_clock_after_both_locks(world, monkeypatch):
    world.grant(['settings:write'])
    contender = ConfigurationStore(world.engine, world.configuration.key_path)
    manager = ConfigurationManager(contender, catalog_loader=lambda values: world.catalog)
    control = AccessControl(contender.access_store)
    started, errors = Event(), []
    original = contender._locked
    @contextmanager
    def observed():
        started.set()
        with original() as connection:
            yield connection
    monkeypatch.setattr(contender, '_locked', observed)
    def run():
        try:
            manager.patch_authorized(world.initial, {}, principal=world.actor, control=control)
        except Exception as exc:
            errors.append(exc)
    with world.configuration._locked():
        worker = Thread(target=run)
        worker.start()
        assert started.wait(5)
        from api.app import config_store
        monkeypatch.setattr(config_store, '_utc_now', lambda: NOW + timedelta(hours=2))
    worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], StaleCredentialError)
    assert json.loads(world.audits()[-1]['details']) == {'reason': 'credential_stale'}


def test_unknown_operation_is_rejected_without_audit(world):
    world.grant(PERMISSIONS)
    with pytest.raises(ConfigurationStoreError):
        world.configuration.apply_authorized(world.initial, world.initial.desired.values,
            principal=world.actor, control=world.control, operation='owner-controlled-arbitrary-operation',
            restart_required=False, providers={}, plugins=default_plugin_state(world.initial.desired.values),
            baseline_providers={})
    assert world.audits() == []


def test_authentication_and_future_plugin_document_keys_are_owner_protected():
    from api.app.access.configuration import configuration_candidate_requirements
    from api.app.config_values import ConfigurationValues
    values = ConfigurationValues.from_environment({})
    state = default_plugin_state(values)
    for candidate in ({**state, 'new-private-installation-key': 'synthetic-value'},
                      {**state, 'roles': {**state['roles'], 'auth': {'enabled': True}}}):
        requirement = configuration_candidate_requirements(values, values,
            ConfigurationDocument(state), ConfigurationDocument(candidate), profile_drift=False)
        assert requirement.permissions == frozenset({'owner:recover'})
        assert requirement.requires_complete_owner
        assert 'new-private-installation-key' not in repr(requirement)
        assert 'synthetic-value' not in repr(requirement)


def test_explicit_inherited_credential_override_is_a_provider_change(world):
    world.grant(['settings:write'])
    inherited = world.initial.desired.values.sinch_api_key
    assert inherited == 'synthetic-provider-secret'
    before = world.business()
    with pytest.raises(MutationDeniedError):
        patch(world, {'sinch_api_key': inherited})
    assert world.business() == before


@pytest.mark.parametrize('boundary', ['commit', 'cleanup'])
def test_uncertain_commit_or_cleanup_never_returns_success_or_retries(world, monkeypatch, boundary):
    world.grant(['settings:write'])
    calls = []
    original = world.configuration._insert
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(world.configuration, '_insert', counted)
    with monkeypatch.context() as temporary:
        if boundary == 'commit':
            original_commit = world.engine.dialect.do_commit
            def uncertain(connection):
                original_commit(connection)
                raise sa.exc.DatabaseError('synthetic-uncertain-commit', {}, Exception('private-backend'))
            temporary.setattr(world.engine.dialect, 'do_commit', uncertain)
        else:
            original_connect = world.engine.connect
            def connect():
                connection = original_connect()
                close = connection.close
                def uncertain_close():
                    close()
                    if calls:
                        raise sa.exc.DatabaseError('synthetic-uncertain-cleanup', {}, Exception('private-backend'))
                connection.close = uncertain_close
                return connection
            temporary.setattr(world.engine, 'connect', connect)
        with pytest.raises(ConfigurationCommitUncertain):
            patch(world, {'fax_header': 'durable-but-no-receipt'})
    assert calls == [1]
    assert world.configuration.read().generation == 2
    assert len(world.audits()) == 1


@pytest.mark.parametrize('mutation', ['group_disable', 'grant_remove', 'key_rotate'])
def test_access_only_mutation_commits_before_waiting_configuration_decision(world, monkeypatch, mutation):
    world.insert('access_groups', id='editor-group', name='editor group', normalized_name='editor group', description='')
    world.insert('access_memberships', principal_id='editor', group_id='editor-group')
    world.role('group-role', ['settings:write'])
    assignment = world.assignment(None, 'group-role', group='editor-group')
    actor = world.key() if mutation == 'key_rotate' else world.actor
    contender = ConfigurationStore(world.engine, world.configuration.key_path)
    control = AccessControl(contender.access_store)
    manager = ConfigurationManager(contender, catalog_loader=lambda values: world.catalog)
    before = world.business()
    started, outcomes = Event(), []
    original = contender._locked
    @contextmanager
    def observed():
        started.set()
        with original() as connection:
            yield connection
    monkeypatch.setattr(contender, '_locked', observed)
    def run():
        try:
            manager.patch_authorized(world.initial, {'fax_header': 'losing-after-revocation'}, principal=actor, control=control)
        except Exception as error:
            outcomes.append(error)
    with world.store.transaction() as connection:
        worker = Thread(target=run)
        worker.start()
        assert started.wait(5)
        if mutation == 'group_disable':
            table = world.tables['access_groups']
            connection.execute(table.update().where(table.c.id == 'editor-group').values(enabled=0))
        elif mutation == 'grant_remove':
            table = world.tables['access_assignments']
            connection.execute(table.delete().where(table.c.id == assignment))
        else:
            table = world.tables['access_key_bindings']
            connection.execute(table.update().where(table.c.id == 'editor-key').values(security_version=2))
    worker.join(5)
    assert not worker.is_alive()
    expected = StaleCredentialError if mutation == 'key_rotate' else MutationDeniedError
    assert len(outcomes) == 1 and isinstance(outcomes[0], expected)
    assert world.business() == before and len(world.audits()) == 1


def test_restricted_owner_key_cannot_recover_bootstrap(world):
    world.grant(PERMISSIONS)
    world.assignment('editor', 'role_owner')
    actor = world.key(ceiling=('settings:write', 'owner:recover'))
    with pytest.raises(MutationDeniedError) as failure:
        patch(world, {'api_key': 'synthetic-bootstrap'}, actor=actor)
    assert failure.value.reason == MutationReason.OWNER_REQUIRED


def test_pending_edits_compare_desired_and_keep_active_bootstrap_until_activation(world):
    world.assignment('editor', 'role_owner')
    activated = patch(world, {'api_key': 'synthetic-A'})
    from api.app.access.bootstrap import BootstrapCredentials
    from api.app.config_secrets import load_installation_key
    bootstrap = BootstrapCredentials(world.configuration, installation_key=load_installation_key(
        world.configuration.key_path, allow_create=False))
    world.control = AccessControl(world.store, current_bootstrap_fingerprint_on=bootstrap.current_fingerprint_on)
    with world.store.transaction() as connection:
        old_context = bootstrap.authenticate_on(connection, 'synthetic-A')
    staged = patch(world, {'enable_mcp_http': True, 'api_key': 'synthetic-B'}, expected=activated)
    assert staged.pending is not None
    assert staged.active.values.api_key == 'synthetic-A' and staged.desired.values.api_key == 'synthetic-B'
    assert patch(world, {}, expected=staged, actor=old_context) == staged
    ordinary = world.user('ordinary')
    world.role('ordinary-role', ['settings:write'])
    world.assignment('ordinary', 'ordinary-role')
    edited = patch(world, {'fax_header': 'pending ordinary edit'}, expected=staged, actor=ordinary)
    assert edited.active == staged.active and edited.pending is not None
    assert edited.desired.values.api_key == 'synthetic-B'
    changed = patch(world, {'enable_mcp_http': False}, expected=edited)
    assert changed.pending is None and changed.active.values.api_key == 'synthetic-B'
    restored = patch(world, {'api_key': 'synthetic-A'}, expected=changed)
    before = world.business()
    with pytest.raises(StaleCredentialError):
        patch(world, {}, expected=restored, actor=old_context)
    assert world.business() == before
    assert world.audits()[-1]['actor_principal_id'] is None


def test_maintenance_path_edit_is_rejected_before_catalog_filesystem_read(world):
    def unexpected(values):
        pytest.fail('Rejected maintenance paths must not reach catalog loading')
    world.manager.catalog_loader = unexpected
    with pytest.raises(ConfigurationActivationError):
        patch(world, {'providers_dir': '/synthetic-untrusted-path'})
    with pytest.raises(ConfigurationActivationError):
        world.manager.patch(world.initial, {'providers_dir': '/synthetic-untrusted-path'}, actor='trusted-fixture')


@pytest.mark.parametrize('category', ['installation', 'settings'])
def test_canonical_json_type_change_cannot_bypass_document_permissions(world, category):
    world.grant(['settings:write'])
    original = world.initial.desired.plugins.as_dict()
    if category == 'installation':
        original['future-installation-option'] = 1
    else:
        original['settings']['custom'] = {'private-option': 1}
    seeded = world.configuration.apply(world.initial, world.initial.desired.values,
        restart_required=False, actor='trusted-fixture', plugins=original)
    replacement = seeded.desired.plugins.as_dict()
    if category == 'installation':
        replacement['future-installation-option'] = True
    else:
        replacement['settings']['custom']['private-option'] = True
    providers = {role: world.configuration.read_profile(identity).configuration
        for role, identity in seeded.desired.profiles}
    before = world.business()
    with pytest.raises(MutationDeniedError):
        world.configuration.apply_authorized(seeded, seeded.desired.values,
            principal=world.actor, control=world.control, operation='settings.update',
            restart_required=False, providers=providers, plugins=replacement, baseline_providers=providers)
    assert world.business() == before
