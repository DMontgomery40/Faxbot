"""Minimal console projection against isolated SQL, never HTTP or UI handlers."""
from contextlib import contextmanager
from datetime import timedelta, timezone
import importlib
import importlib.util
import json
from threading import Event, Thread
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_policy import World, NOW, key_context, ceiling
from api.app.access.policy import AccessControl
from api.app.access.store import AccessStore
from api.app.access.types import AuthenticationError, InvalidTransactionError, StaleCredentialError
from api.app.config_store import ConfigurationStore, ConfigurationNotInitialized, ConfigurationStoreError
from api.app.config_values import ConfigurationValues


def service_type():
    # Keep the initial RED an explicit missing-contract assertion, not a
    # collection error that could hide unrelated fixture failures.
    assert importlib.util.find_spec('api.app.access.context') is not None, 'ConsoleContext service is missing'
    return importlib.import_module('api.app.access.context').ConsoleContext


class ContextWorld(World):
    def __init__(self, database, tmp_path):
        super().__init__(database)
        self.configuration = ConfigurationStore(database, tmp_path / 'configuration.key')
        self.store = self.configuration.access_store
        self.tables = self.store.tables
        self.control = AccessControl(self.store)
        self.initial = self.configuration.initialize(ConfigurationValues.from_environment({
            'FAX_DISABLED': 'true', 'MAX_FILE_SIZE_MB': '23', 'INBOUND_ENABLED': 'true',
            'FEATURE_V3_PLUGINS': 'true', 'FEATURE_PLUGIN_INSTALL': 'false',
            'FAX_OUTBOUND_BACKEND': 'phaxio', 'FAX_INBOUND_BACKEND': 'sinch',
            'PHAXIO_API_KEY': 'synthetic-provider-key-do-not-project',
            'PHAXIO_API_SECRET': 'synthetic-provider-secret-do-not-project',
            'PHAXIO_CALLBACK_TOKEN': 'synthetic-callback-secret-do-not-project',
            'DATABASE_URL': 'postgresql://private-user:synthetic-password@private-host/private-database',
            'FAX_DATA_DIR': '/synthetic/private-artifacts',
            'FAXBOT_CONFIG_PATH': '/synthetic/private-plugin-config',
            'PUBLIC_API_URL': 'https://private-host.invalid/secret-callback-path',
        }), actor='synthetic-internal-fixture', plugins={'private': {'secret': 'synthetic-plugin-secret'}})
        self.actor = self.user('alice')

    def snapshot(self, actor=None, **options):
        options.setdefault('clock', lambda: NOW)
        return service_type()(self.configuration, self.control, **options).snapshot(actor or self.actor)

    def mailbox(self, identity='box'):
        self.insert('mailboxes', id=identity, label='private mailbox label')
        self.resource('mailbox-' + identity, 'mailbox', 'installation', 'installation', mailbox_id=identity)
        return 'mailbox-' + identity

    def inbound(self, identity='incoming', parent='legacy', parent_kind='legacy'):
        self.insert('inbound_faxes', id=identity, status='received', backend='sip', received_at=NOW)
        self.resource('resource-' + identity, 'inbound', parent, parent_kind, inbound_fax_id=identity)
        return 'resource-' + identity


@pytest.fixture
def world(database, tmp_path):
    return ContextWorld(database, tmp_path)


def test_unassigned_identity_has_only_closed_safe_context(world):
    assert world.snapshot() == {
        'policy_version': 1, 'active_revision_id': world.initial.active.id, 'generation': 1,
        'permissions': [], 'navigation': {'jobs': False, 'inbox': False, 'send': False},
        'send': None, 'inbound_enabled': None,
        'branding': {'docs_base': 'https://docs.faxbot.net/latest/', 'logo_path': '/admin/ui/faxbot_full_logo.png'},
        'provider_view': None,
    }


@pytest.mark.parametrize('scope', ['personal', 'mailbox', 'legacy', 'installation'])
def test_empty_valid_scopes_enable_metadata_navigation(world, scope):
    target = {'personal': 'personal-alice', 'mailbox': world.mailbox(),
              'legacy': 'legacy', 'installation': 'installation'}[scope]
    world.role('metadata', ['fax:read', 'inbound:list'])
    world.assignment('alice', 'metadata', target)
    assert world.snapshot()['navigation'] == {
        'jobs': scope != 'mailbox', 'inbox': scope != 'personal', 'send': False,
    }


def test_group_scope_enables_empty_inbox_without_global_permission(world):
    target = world.mailbox()
    world.role('inbox-list', ['inbound:list'])
    world.insert('access_groups', id='operators', name='operators', normalized_name='operators', description='')
    world.insert('access_memberships', group_id='operators', principal_id='alice')
    world.assignment(None, 'inbox-list', target, group='operators')
    result = world.snapshot()
    assert result['navigation'] == {'jobs': False, 'inbox': True, 'send': False}
    assert result['permissions'] == []
    assert result['inbound_enabled'] is True
    world.update('access_groups', 'operators', enabled=0)
    assert world.snapshot()['navigation']['inbox'] is False


@pytest.mark.parametrize('kind', ['outbound', 'inbound'])
def test_direct_fax_scope_enables_only_its_metadata_navigation(world, kind):
    target = world.outbound('only-fax').id if kind == 'outbound' else world.inbound()
    world.role('metadata', ['fax:read', 'inbound:list'])
    world.assignment('alice', 'metadata', target)
    result = world.snapshot()
    assert result['navigation'] == {'jobs': kind == 'outbound', 'inbox': kind == 'inbound', 'send': False}
    assert result['permissions'] == []
    assert target not in json.dumps(result)


def test_document_only_grants_never_enable_metadata_navigation(world):
    world.outbound('only-fax')
    world.inbound()
    world.role('documents', ['fax:document', 'inbound:document', 'inbound:read'])
    world.assignment('alice', 'documents')
    result = world.snapshot()
    assert result['navigation'] == {'jobs': False, 'inbox': False, 'send': False}
    assert result['send'] is result['inbound_enabled'] is result['provider_view'] is None


@pytest.mark.parametrize('invalid', ['role', 'container', 'root', 'wrong-parent', 'wrong-link'])
def test_invalid_scope_ancestry_cannot_enable_empty_navigation(world, invalid):
    world.role('metadata', ['fax:read'])
    world.assignment('alice', 'metadata', 'personal-alice')
    if invalid == 'role':
        world.update('access_roles', 'metadata', enabled=0)
    elif invalid == 'wrong-parent':
        world.user('bob')
        changes = {'parent_id': 'personal-bob', 'parent_kind': 'personal'}
    elif invalid == 'wrong-link':
        world.insert('mailboxes', id='box', label='private malformed-scope fixture')
        changes = {'mailbox_id': 'box'}
    else:
        world.update('access_resources', 'installation' if invalid == 'root' else 'personal-alice', enabled=0)
    if invalid in {'wrong-parent', 'wrong-link'}:
        # Exercise policy defense against damaged storage in this isolated DB.
        resource = world.tables['access_resources']
        with world.engine.begin() as connection:
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('PRAGMA ignore_check_constraints = ON')
            else:
                connection.exec_driver_sql('ALTER TABLE access_resources DROP CONSTRAINT ck_access_resources_shape')
            connection.execute(resource.update().where(resource.c.id == 'personal-alice').values(**changes))
            if connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('PRAGMA ignore_check_constraints = OFF')
    assert world.snapshot()['navigation']['jobs'] is False


def test_send_is_current_own_personal_authority_and_exposes_only_active_limits(world):
    world.role('send', ['fax:send'])
    world.assignment('alice', 'send', 'personal-alice')
    result = world.snapshot()
    assert result['permissions'] == ['fax:send']
    assert result['navigation'] == {'jobs': False, 'inbox': False, 'send': True}
    assert result['send'] == {'fax_disabled': True, 'max_file_size_mb': 23}
    bob = world.user('bob')
    world.assignment('bob', 'send', 'personal-alice')
    assert world.snapshot(bob)['send'] is None


def test_send_does_not_assume_a_personal_resource_id_format(world):
    world.user('bob')
    # Move Alice's empty personal container to an independently chosen stable ID.
    r = world.tables['access_resources']
    with world.engine.begin() as connection:
        connection.execute(r.delete().where(r.c.id == 'personal-alice'))
    world.resource('independent-personal-id', 'personal', 'installation', 'installation', principal_id='alice')
    world.role('send', ['fax:send'])
    world.assignment('alice', 'send', 'independent-personal-id')
    assert world.snapshot()['navigation']['send'] is True


@pytest.mark.parametrize('session', [False, True])
def test_key_ceiling_intersects_empty_scopes_and_current_assignments(world, session):
    actor = key_context(world, session=session)
    world.assignment('alice', 'role_owner')
    ceiling(world, 'fax:read', 'personal-alice')
    result = world.snapshot(actor)
    assert result['navigation'] == {'jobs': True, 'inbox': False, 'send': False}
    assert result['permissions'] == []
    assert result['send'] is result['inbound_enabled'] is result['provider_view'] is None
    assignments = world.tables['access_assignments']
    with world.engine.begin() as connection:
        connection.execute(assignments.delete().where(assignments.c.principal_id == 'alice'))
    assert world.snapshot(actor)['navigation']['jobs'] is False


def test_nonoverlapping_assignment_and_key_ceiling_have_no_scope(world):
    actor = key_context(world)
    world.role('metadata', ['fax:read'])
    world.assignment('alice', 'metadata', 'personal-alice')
    ceiling(world, 'fax:read', 'legacy')
    assert world.snapshot(actor)['navigation']['jobs'] is False


def test_reset_required_identity_suppresses_every_ordinary_capability(world):
    world.assignment('alice', 'role_owner')
    world.mailbox()
    world.update('access_users', 'alice', password_change_required=1)
    result = world.snapshot()
    assert result['permissions'] == []
    assert result['navigation'] == {'jobs': False, 'inbox': False, 'send': False}
    assert result['send'] is result['inbound_enabled'] is result['provider_view'] is None
    assert result['active_revision_id'] == world.initial.active.id
    assert result['generation'] == result['policy_version'] == 1


def test_provider_read_projection_is_closed_and_never_exposes_private_configuration(world):
    world.role('provider-reader', ['providers:read'])
    world.assignment('alice', 'provider-reader')
    result = world.snapshot()
    assert result['permissions'] == ['providers:read']
    assert result['provider_view'] == {'plugins_enabled': True, 'install_enabled': False,
        'active_outbound': 'phaxio', 'active_inbound': 'sinch'}
    assert set(result) == {'policy_version', 'active_revision_id', 'generation', 'permissions',
        'navigation', 'send', 'inbound_enabled', 'branding', 'provider_view'}
    encoded = json.dumps(result)
    for private in ('synthetic-', 'private-', 'alice', 'mailbox', 'environment', 'profiles',
                    'api_key', 'callback', 'database_url', 'desired_revision', 'installation_id'):
        assert private not in encoded


def test_installation_permissions_are_current_and_scoped_provider_read_is_not_global(world):
    world.role('scoped-provider-reader', ['providers:read'])
    world.assignment('alice', 'scoped-provider-reader', 'personal-alice')
    assert world.snapshot()['provider_view'] is None
    world.role('global-configuration-reader', ['settings:read'])
    world.assignment('alice', 'global-configuration-reader')
    assert world.snapshot()['permissions'] == ['settings:read']
    assert world.snapshot()['provider_view'] is None


def test_pending_configuration_does_not_supply_active_context(world):
    world.assignment('alice', 'role_owner')
    pending = world.configuration.apply(world.initial, world.initial.active.values.with_patch({
        'fax_disabled': False, 'max_file_size_mb': 99, 'inbound_enabled': False,
        'feature_v3_plugins': False, 'feature_plugin_install': True,
        'outbound_backend': 'sinch', 'inbound_backend': 'phaxio',
    }), restart_required=True, actor='synthetic-internal-fixture')
    result = world.snapshot()
    assert result['active_revision_id'] == world.initial.active.id
    assert result['generation'] == pending.generation == 2
    assert result['send'] == {'fax_disabled': True, 'max_file_size_mb': 23}
    assert result['inbound_enabled'] is True
    assert result['provider_view'] == {'plugins_enabled': True, 'install_enabled': False,
        'active_outbound': 'phaxio', 'active_inbound': 'sinch'}


def test_ordinary_active_configuration_edit_is_reflected_without_cached_authority(world):
    world.assignment('alice', 'role_owner')
    first = world.snapshot()
    current = world.configuration.apply(world.initial, world.initial.active.values.with_patch({
        'fax_disabled': False, 'max_file_size_mb': 17,
    }), restart_required=False, actor='synthetic-internal-fixture')
    state = world.tables['access_state']
    with world.engine.begin() as connection:
        connection.execute(state.update().where(state.c.id == 'state').values(policy_version=2))
    result = world.snapshot()
    assert result['policy_version'] == 2
    assert result['active_revision_id'] == current.active.id != first['active_revision_id']
    assert result['generation'] == 2
    assert result['send'] == {'fax_disabled': False, 'max_file_size_mb': 17}


def test_clock_and_active_reads_share_both_existing_locks_on_one_connection(world, monkeypatch):
    real_head, real_revision = world.configuration._head, world.configuration._revision
    connections, events = [], []
    real_lock = world.store.lock_on

    def lock_on(connection):
        world.configuration._require_lock_on(connection)
        result = real_lock(connection)
        connections.append(connection)
        events.append('access')
        return result

    def clock():
        assert events == ['access']
        world.configuration._require_lock_on(connections[0])
        world.store.require_lock_on(connections[0])
        events.append('clock')
        return NOW

    def head(connection):
        assert connection is connections[0]
        assert events[:2] == ['access', 'clock']
        world.configuration._require_lock_on(connection)
        world.store.require_lock_on(connection)
        events.append('head')
        return real_head(connection)

    def revision(connection, cipher, installation, identity):
        assert connection is connections[0]
        assert identity == world.initial.active.id
        world.configuration._require_lock_on(connection)
        world.store.require_lock_on(connection)
        events.append('revision')
        return real_revision(connection, cipher, installation, identity)

    monkeypatch.setattr(world.store, 'lock_on', lock_on)
    monkeypatch.setattr(world.configuration, '_head', head)
    monkeypatch.setattr(world.configuration, '_revision', revision)
    assert world.snapshot(clock=clock)['active_revision_id'] == world.initial.active.id
    assert events == ['access', 'clock', 'head', 'revision']


@pytest.mark.parametrize('change', ['revoke', 'expire'])
def test_source_is_revalidated_and_clock_sampled_after_waiting_for_locks(world, monkeypatch, change):
    world.assignment('alice', 'role_owner')
    attempted = Event()
    original = world.configuration._locked
    current_time = [NOW]
    result, errors = [], []

    @contextmanager
    def locked():
        attempted.set()
        with original() as connection:
            yield connection

    monkeypatch.setattr(world.configuration, '_locked', locked)
    context = service_type()(world.configuration, world.control, clock=lambda: current_time[0])

    def reader():
        try:
            result.append(context.snapshot(world.actor))
        except BaseException as error:
            errors.append(error)

    with world.store.transaction() as connection:
        thread = Thread(target=reader, daemon=True)
        thread.start()
        assert attempted.wait(5), 'Reader did not reach the configuration lock'
        if change == 'revoke':
            sessions = world.tables['access_sessions']
            connection.execute(sessions.update().where(sessions.c.id == 'session-alice').values(revoked_at=NOW))
        else:
            current_time[0] = NOW + timedelta(hours=1)
    thread.join(10)
    assert not thread.is_alive(), 'Reader remained blocked after the mutation committed'
    assert result == []
    assert len(errors) == 1 and isinstance(errors[0], StaleCredentialError)


def test_replaced_access_store_is_rejected_even_on_same_engine(world):
    wrong = AccessControl(AccessStore(world.engine))
    with pytest.raises(InvalidTransactionError):
        service_type()(world.configuration, wrong, clock=lambda: NOW).snapshot(world.actor)


def test_store_pairing_is_rechecked_when_snapshot_runs(world):
    context = service_type()(world.configuration, world.control, clock=lambda: NOW)
    world.control.store = AccessStore(world.engine)
    with pytest.raises(InvalidTransactionError):
        context.snapshot(world.actor)


@pytest.mark.parametrize('now', [NOW.replace(tzinfo=timezone.utc), '2026-10-03', None])
def test_non_naive_utc_clock_is_rejected_before_context_disclosure(world, now):
    with pytest.raises(AuthenticationError):
        world.snapshot(clock=lambda: now)


def test_missing_configuration_has_no_admin_fallback(world):
    with world.engine.begin() as connection:
        connection.execute(world.configuration.state.delete())
    with pytest.raises(ConfigurationNotInitialized):
        world.snapshot()


@pytest.mark.parametrize('url', ['https://user:synthetic-secret@docs.example.invalid/latest/',
    'https://docs.example.invalid/latest/?token=synthetic-secret', 'javascript:synthetic-secret',
    '//docs.example.invalid/latest/', 'https:///latest/', 'https://docs.example.invalid:bad/latest/',
    'https://docs.example.invalid/\nsynthetic-secret', 'https://docs.example.invalid\\@evil.invalid/',
    '', None, 'https://docs.example.invalid/' + 'a' * 2048])
def test_invalid_docs_base_fails_with_fixed_nonsecret_error(url):
    configuration = SimpleNamespace(access_store=object())
    control = SimpleNamespace(store=configuration.access_store)
    with pytest.raises(ConfigurationStoreError) as failure:
        service_type()(configuration, control, docs_base=url)
    assert str(failure.value) == 'Invalid console documentation URL.'
    assert 'synthetic-secret' not in str(failure.value)


@pytest.mark.parametrize('url', ['http://localhost:8080/docs/', 'https://docs.example.invalid/latest/'])
def test_safe_docs_base_is_passed_without_fetching_or_exposing_configuration(world, url):
    assert world.snapshot(docs_base=url)['branding'] == {
        'docs_base': url, 'logo_path': '/admin/ui/faxbot_full_logo.png',
    }


def test_legitimate_default_active_values_supply_the_send_policy(world):
    world.role('send', ['fax:send'])
    world.assignment('alice', 'send', 'personal-alice')
    current = world.configuration.apply(world.initial, ConfigurationValues.from_environment({}),
        restart_required=False, actor='synthetic-internal-fixture')
    result = world.snapshot()
    assert result['active_revision_id'] == current.active.id
    assert result['send'] == {'fax_disabled': False, 'max_file_size_mb': 10}
