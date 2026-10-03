"""Policy seams against isolated, migrated SQLite and PostgreSQL databases."""
from datetime import datetime, timedelta
import uuid

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.access.store import AccessStore
from api.app.access.types import (PrincipalContext, PasswordSessionEvidence, ResourceRef,
                                 DecisionReason)
from api.app.access.policy import AccessControl

NOW = datetime(2026, 10, 3, 12, 0, 0)


class World:
    def __init__(self, database):
        self.engine = database
        upgrade_schema(database)
        self.store = AccessStore(database)
        self.control = AccessControl(self.store)
        self.tables = self.store.tables

    def insert(self, table_name, **values):
        table = self.tables[table_name]
        for field in ('created_at', 'updated_at'):
            if field in table.c:
                values.setdefault(field, NOW - timedelta(minutes=5))
        if 'version' in table.c:
            values.setdefault('version', 1)
        if 'enabled' in table.c:
            values.setdefault('enabled', 1)
        values.setdefault('id', uuid.uuid4().hex)
        with self.engine.begin() as connection:
            connection.execute(table.insert().values(**values))
        return values['id']

    def update(self, name, identity, **values):
        table = self.tables[name]
        with self.engine.begin() as connection:
            connection.execute(table.update().where(table.c.id == identity).values(**values))

    def user(self, identity):
        self.insert('access_principals', id=identity, kind='user', display_name=identity, security_version=1)
        self.insert('access_users', id=identity, login=identity, normalized_login=identity,
                    password_hash='synthetic-internal-policy-fixture', password_change_required=0, password_version=1)
        self.resource('personal-' + identity, 'personal', 'installation', 'installation', principal_id=identity)
        self.insert('access_sessions', id='session-' + identity, principal_id=identity, source_kind='password',
                    source_key_id=None, source_key_version=None, bootstrap_fingerprint=None,
                    token_hash=uuid.uuid4().hex, csrf_hash=uuid.uuid4().hex,
                    principal_security_version=1, password_version=1, last_used_at=NOW - timedelta(minutes=1),
                    expires_at=NOW + timedelta(hours=1), revoked_at=None)
        return PrincipalContext(identity, 1, PasswordSessionEvidence('session-' + identity, 1), 'principal:' + identity)

    def resource(self, identity, kind, parent, parent_kind, **links):
        return self.insert('access_resources', id=identity, kind=kind, parent_id=parent, parent_kind=parent_kind,
                           principal_id=None if 'principal_id' not in links else links.pop('principal_id'),
                           mailbox_id=None if 'mailbox_id' not in links else links.pop('mailbox_id'),
                           fax_job_id=None if 'fax_job_id' not in links else links.pop('fax_job_id'),
                           inbound_fax_id=None if 'inbound_fax_id' not in links else links.pop('inbound_fax_id'), **links)

    def outbound(self, identity, parent='legacy', parent_kind='legacy'):
        self.insert('fax_jobs', id=identity, to_number='+12025550123', file_name='synthetic.pdf',
                    tiff_path='/synthetic/unused.tiff', status='queued', backend='sip')
        self.resource('resource-' + identity, 'outbound', parent, parent_kind, fax_job_id=identity)
        return ResourceRef('resource-' + identity)

    def assignment(self, principal, role, resource='installation', group=None):
        return self.insert('access_assignments', principal_id=principal, group_id=group, role_id=role, resource_id=resource)

    def role(self, identity, permissions):
        self.insert('access_roles', id=identity, name=identity, normalized_name=identity, description='', kind='custom')
        for permission in permissions:
            self.insert('access_role_permissions', role_id=identity, permission_id=permission)
        return identity


@pytest.fixture
def world(database):
    return World(database)


def test_current_valid_identity_without_assignment_is_denied(world):
    actor = world.user('alice')
    fax = world.outbound('fax')
    result = world.control.authorize(actor, 'fax:read', fax, now=NOW)
    assert result.allowed is False
    assert result.reason == DecisionReason.FORBIDDEN
    assert result.policy_version == 1


def test_direct_and_group_permissions_union_without_document_inference(world):
    actor = world.user('alice')
    fax = world.outbound('fax')
    world.role('metadata', ['fax:read'])
    world.assignment('alice', 'metadata')
    assert world.control.authorize(actor, 'fax:read', fax, now=NOW).allowed
    assert not world.control.authorize(actor, 'fax:document', fax, now=NOW).allowed
    group = world.insert('access_groups', id='operators', name='operators', normalized_name='operators', description='')
    world.insert('access_memberships', group_id=group, principal_id='alice')
    world.role('document', ['fax:document'])
    world.assignment(None, 'document', group=group)
    with world.store.transaction() as connection:
        assert world.control.effective_access_on(connection, actor, fax, now=NOW) == frozenset({'fax:read', 'fax:document'})
        query = world.control.visible_resource_ids_on(connection, actor, 'fax:read', 'outbound', now=NOW)
        assert connection.execute(query).scalars().all() == [fax.id]


def test_delegation_requires_ancestor_scope_covering_future_resources(world):
    from api.app.access.types import ScopedPermission
    actor = world.user('alice')
    one = world.outbound('one', 'personal-alice', 'personal')
    two = world.outbound('two', 'personal-alice', 'personal')
    world.role('metadata', ['fax:read'])
    world.assignment('alice', 'metadata', one.id)
    world.assignment('alice', 'metadata', two.id)
    requested = (ScopedPermission('fax:read', ResourceRef('personal-alice')),)
    with world.store.transaction() as connection:
        assert not world.control.can_grant_on(connection, actor, requested, now=NOW).allowed
    world.assignment('alice', 'metadata', 'personal-alice')
    with world.store.transaction() as connection:
        assert world.control.can_grant_on(connection, actor, requested, now=NOW).allowed
        assert not world.control.can_grant_on(connection, actor, (ScopedPermission('fax:read', ResourceRef('installation')),), now=NOW).allowed
        assert not world.control.authorize_on(connection, actor, 'fax:read', ResourceRef('personal-alice'), now=NOW).allowed


def test_owner_protection_requires_builtin_assignment_even_for_disabled_target(world):
    from api.app.access.catalog import PERMISSIONS
    actor = world.user('alice')
    world.user('owner')
    world.assignment('owner', 'role_owner')
    world.update('access_principals', 'owner', enabled=0)
    world.role('custom-owner', PERMISSIONS)
    world.assignment('alice', 'custom-owner')
    with world.store.transaction() as connection:
        assert not world.control.is_complete_owner_on(connection, actor, now=NOW)
        assert world.control.dominates_principal_on(connection, actor, 'owner', now=NOW).reason == DecisionReason.OWNER_REQUIRED
    world.assignment('alice', 'role_owner')
    with world.store.transaction() as connection:
        assert world.control.is_complete_owner_on(connection, actor, now=NOW)
        assert world.control.dominates_principal_on(connection, actor, 'owner', now=NOW).allowed


def test_operation_targets_are_distinct_from_grant_scopes(world):
    actor = world.user('alice')
    world.user('bob')
    fax = world.outbound('fax', 'personal-alice', 'personal')
    world.assignment('alice', 'role_owner')
    for permission, resource, expected in [
        ('fax:send', ResourceRef('personal-alice'), True),
        ('fax:send', ResourceRef('personal-bob'), False),
        ('fax:send', ResourceRef('installation'), False),
        ('fax:read', ResourceRef('installation'), False),
        ('fax:document', fax, True),
        ('settings:write', fax, False),
        ('mailboxes:manage', ResourceRef('installation'), True),
    ]:
        assert world.control.authorize(actor, permission, resource, now=NOW).allowed is expected


@pytest.mark.parametrize('disabled', ['actor', 'role', 'group', 'container', 'root'])
def test_disablements_deny_current_authority_and_visibility(world, disabled):
    from api.app.access.types import StaleCredentialError
    actor = world.user('alice')
    fax = world.outbound('fax', 'personal-alice', 'personal')
    world.insert('access_groups', id='team', name='team', normalized_name='team', description='')
    world.insert('access_memberships', principal_id='alice', group_id='team')
    world.assignment(None, 'role_fax_viewer', 'personal-alice', group='team')
    table, identity = {
        'actor': ('access_principals', 'alice'), 'role': ('access_roles', 'role_fax_viewer'),
        'group': ('access_groups', 'team'), 'container': ('access_resources', 'personal-alice'),
        'root': ('access_resources', 'installation'),
    }[disabled]
    world.update(table, identity, enabled=0)
    with world.store.transaction() as connection:
        if disabled == 'actor':
            with pytest.raises(StaleCredentialError):
                world.control.visible_resource_ids_on(connection, actor, 'fax:read', 'outbound', now=NOW)
        else:
            assert not world.control.authorize_on(connection, actor, 'fax:read', fax, now=NOW).allowed
            assert connection.execute(world.control.visible_resource_ids_on(connection, actor, 'fax:read', 'outbound', now=NOW)).scalars().all() == []


def test_disabled_creator_does_not_hide_their_enabled_tree_from_an_owner(world):
    world.user('creator')
    owner = world.user('owner')
    fax = world.outbound('fax', 'personal-creator', 'personal')
    world.assignment('owner', 'role_owner')
    world.update('access_principals', 'creator', enabled=0)
    assert world.control.authorize(owner, 'fax:read', fax, now=NOW).allowed


def test_inbound_list_metadata_never_implies_detail_or_document(world):
    actor = world.user('alice')
    world.insert('mailboxes', id='box', label='Arbitrary renamed label')
    world.resource('mailbox-box', 'mailbox', 'installation', 'installation', mailbox_id='box')
    world.insert('inbound_faxes', id='incoming', status='received', backend='sip', received_at=NOW)
    world.resource('resource-incoming', 'inbound', 'mailbox-box', 'mailbox', inbound_fax_id='incoming')
    world.role('list-only', ['inbound:list'])
    world.assignment('alice', 'list-only', 'mailbox-box')
    with world.store.transaction() as connection:
        for permission, expected in [('inbound:list', True), ('inbound:read', False), ('inbound:document', False)]:
            assert world.control.authorize_on(connection, actor, permission, ResourceRef('resource-incoming'), now=NOW).allowed is expected
        assert connection.execute(world.control.visible_resource_ids_on(connection, actor, 'inbound:list', 'inbound', now=NOW)).scalars().all() == ['resource-incoming']


def test_unknown_permission_and_resource_deny_after_source_revalidation(world):
    from api.app.access.types import StaleCredentialError
    actor = world.user('alice')
    world.assignment('alice', 'role_owner')
    for permission, resource in [('unknown', ResourceRef('installation')), ('fax:read', ResourceRef('absent')), ('fax:read', {'id': 'installation'})]:
        assert not world.control.authorize(actor, permission, resource, now=NOW).allowed
    world.update('access_sessions', 'session-alice', revoked_at=NOW)
    with pytest.raises(StaleCredentialError):
        world.control.authorize(actor, 'unknown', ResourceRef('absent'), now=NOW)


def key_context(world, principal='alice', *, session=False):
    from api.app.access.types import KeyEvidence, KeySessionEvidence
    world.insert('api_keys', id='binding', key_id='public-key', key_hash='unused-synthetic-proof', scopes='*', revoked_at=None, expires_at=NOW + timedelta(hours=4))
    world.insert('access_key_bindings', id='binding', principal_id=principal, state='active', security_version=1, revoked_at=None)
    if session:
        world.insert('access_sessions', id='key-session', principal_id=principal, source_kind='key',
            source_key_id='binding', source_key_version=1, bootstrap_fingerprint=None, token_hash=uuid.uuid4().hex,
            csrf_hash=uuid.uuid4().hex, principal_security_version=1, password_version=None,
            last_used_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(hours=1), revoked_at=None)
    evidence = KeySessionEvidence('key-session', 'binding', 1) if session else KeyEvidence('binding', 1)
    return PrincipalContext(principal, 1, evidence, 'key:public-key')


def ceiling(world, permission, resource='installation'):
    world.insert('access_key_grants', key_binding_id='binding', permission_id=permission, resource_id=resource)


@pytest.mark.parametrize('session', [False, True])
def test_key_ceiling_intersects_current_assignments_without_laundering(world, session):
    from api.app.access.types import ScopedPermission
    world.user('alice')
    actor = key_context(world, session=session)
    fax = world.outbound('fax', 'personal-alice', 'personal')
    other = world.outbound('other')
    world.assignment('alice', 'role_owner')
    ceiling(world, 'fax:read', 'personal-alice')
    assert world.control.authorize(actor, 'fax:read', fax, now=NOW).allowed
    assert not world.control.authorize(actor, 'fax:read', other, now=NOW).allowed
    assert not world.control.authorize(actor, 'fax:document', fax, now=NOW).allowed
    with world.store.transaction() as connection:
        assert not world.control.is_complete_owner_on(connection, actor, now=NOW)
        assert not world.control.can_grant_on(connection, actor, (ScopedPermission('fax:read', ResourceRef('installation')),), now=NOW).allowed
        assert world.control.can_grant_on(connection, actor, (ScopedPermission('fax:read', ResourceRef('personal-alice')),), now=NOW).allowed


@pytest.mark.parametrize('change', ['key_version', 'binding_revoked', 'api_revoked', 'expiry_equal', 'principal_version', 'pending', 'ownership'])
def test_changed_key_source_rejects_old_proof(world, change):
    from api.app.access.types import StaleCredentialError
    world.user('alice')
    world.user('bob')
    actor = key_context(world)
    world.assignment('alice', 'role_owner')
    ceiling(world, 'settings:read')
    changes = {
        'key_version': ('access_key_bindings', 'binding', {'security_version': 2}),
        'binding_revoked': ('access_key_bindings', 'binding', {'state': 'revoked', 'revoked_at': NOW}),
        'api_revoked': ('api_keys', 'binding', {'revoked_at': NOW}),
        'expiry_equal': ('api_keys', 'binding', {'expires_at': NOW}),
        'principal_version': ('access_principals', 'alice', {'security_version': 2}),
        'pending': ('access_key_bindings', 'binding', {'state': 'pending_review'}),
        'ownership': ('access_key_bindings', 'binding', {'principal_id': 'bob'}),
    }
    table, identity, values = changes[change]
    world.update(table, identity, **values)
    with pytest.raises(StaleCredentialError):
        world.control.authorize(actor, 'settings:read', ResourceRef('installation'), now=NOW)


@pytest.mark.parametrize('change', ['revoked', 'expiry_equal', 'idle_equal', 'absolute', 'too_long', 'password_version', 'principal_version', 'stored_epoch'])
def test_password_session_freshness_is_current_and_bounded(world, change):
    from api.app.access.types import StaleCredentialError
    actor = world.user('alice')
    world.assignment('alice', 'role_owner')
    changes = {
        'revoked': ('access_sessions', 'session-alice', {'revoked_at': NOW}),
        'expiry_equal': ('access_sessions', 'session-alice', {'expires_at': NOW}),
        'idle_equal': ('access_sessions', 'session-alice', {'created_at': NOW - timedelta(hours=1), 'last_used_at': NOW - timedelta(minutes=30)}),
        'absolute': ('access_sessions', 'session-alice', {'created_at': NOW - timedelta(hours=12, seconds=1)}),
        'too_long': ('access_sessions', 'session-alice', {'expires_at': NOW + timedelta(hours=12)}),
        'password_version': ('access_users', 'alice', {'password_version': 2}),
        'principal_version': ('access_principals', 'alice', {'security_version': 2}),
        'stored_epoch': ('access_sessions', 'session-alice', {'principal_security_version': 2}),
    }
    table, identity, values = changes[change]
    world.update(table, identity, **values)
    with world.store.transaction() as connection:
        for method in [
            lambda: world.control.authorize_on(connection, actor, 'settings:read', ResourceRef('installation'), now=NOW),
            lambda: world.control.effective_access_on(connection, actor, ResourceRef('installation'), now=NOW),
            lambda: world.control.visible_resource_ids_on(connection, actor, 'fax:read', 'outbound', now=NOW),
            lambda: world.control.can_grant_on(connection, actor, (), now=NOW),
            lambda: world.control.dominates_principal_on(connection, actor, 'alice', now=NOW),
            lambda: world.control.is_complete_owner_on(connection, actor, now=NOW),
        ]:
            with pytest.raises(StaleCredentialError):
                method()


def test_session_source_and_replay_namespace_cannot_be_claimed_by_context(world):
    from api.app.access.types import KeySessionEvidence, StaleCredentialError
    actor = world.user('alice')
    world.assignment('alice', 'role_owner')
    key = key_context(world, session=True)
    ceiling(world, 'settings:read')
    forged = [
        PrincipalContext('alice', 1, actor.credential, 'key:public-key'),
        PrincipalContext('alice', 1, key.credential, 'principal:alice'),
        PrincipalContext('alice', 1, KeySessionEvidence('session-alice', 'binding', 1), 'key:public-key'),
        PrincipalContext('alice', 1, PasswordSessionEvidence('key-session', 1), 'principal:alice'),
    ]
    for context in forged:
        with pytest.raises(StaleCredentialError):
            world.control.authorize(context, 'settings:read', ResourceRef('installation'), now=NOW)


@pytest.mark.parametrize('delegated', [False, True])
def test_reset_required_users_have_no_ordinary_authority_or_visibility(world, delegated):
    from api.app.access.types import ScopedPermission
    actor = world.user('alice')
    fax = world.outbound('fax')
    world.assignment('alice', 'role_owner')
    if delegated:
        actor = key_context(world)
        for permission in ('settings:read', 'fax:read'):
            ceiling(world, permission)
    world.update('access_users', 'alice', password_change_required=1)
    with world.store.transaction() as connection:
        assert world.control.authorize_on(connection, actor, 'fax:read', fax, now=NOW).reason == DecisionReason.RESET_REQUIRED
        assert world.control.effective_access_on(connection, actor, fax, now=NOW) == frozenset()
        assert connection.execute(world.control.visible_resource_ids_on(connection, actor, 'fax:read', 'outbound', now=NOW)).scalars().all() == []
        assert not world.control.can_grant_on(connection, actor, (ScopedPermission('fax:read', ResourceRef('installation')),), now=NOW).allowed
        assert not world.control.is_complete_owner_on(connection, actor, now=NOW)
        assert not world.control.dominates_principal_on(connection, actor, 'alice', now=NOW).allowed


@pytest.mark.parametrize('session', [False, True])
def test_bootstrap_requires_live_callback_and_tracks_rotation_and_clear(world, session):
    from api.app.access.types import BootstrapEvidence, StaleCredentialError
    current = ['a' * 64]
    def fingerprint_on(connection):
        assert connection.engine is world.engine
        assert connection.in_transaction()
        return current[0]
    control = AccessControl(world.store, current_bootstrap_fingerprint_on=fingerprint_on)
    identity = 'bootstrap-session' if session else None
    if session:
        world.insert('access_sessions', id=identity, principal_id='bootstrap', source_kind='bootstrap',
            source_key_id=None, source_key_version=None, bootstrap_fingerprint=current[0],
            token_hash=uuid.uuid4().hex, csrf_hash=uuid.uuid4().hex, principal_security_version=1,
            password_version=None, last_used_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(hours=1), revoked_at=None)
    actor = PrincipalContext('bootstrap', 1, BootstrapEvidence(current[0], identity), 'key:env')
    with world.store.transaction() as connection:
        assert control.authorize_on(connection, actor, 'owner:recover', ResourceRef('installation'), now=NOW).allowed
        assert control.is_complete_owner_on(connection, actor, now=NOW)
    with pytest.raises(StaleCredentialError):
        world.control.authorize(actor, 'settings:read', ResourceRef('installation'), now=NOW)
    for value in ['b' * 64, None]:
        current[0] = value
        with pytest.raises(StaleCredentialError):
            control.authorize(actor, 'settings:read', ResourceRef('installation'), now=NOW)


def test_owner_via_enabled_group_requires_all_root_credential_ceilings(world):
    from api.app.access.catalog import PERMISSIONS
    world.user('alice')
    actor = key_context(world)
    world.insert('access_groups', id='owners', name='owners', normalized_name='owners', description='')
    world.insert('access_memberships', group_id='owners', principal_id='alice')
    world.assignment(None, 'role_owner', group='owners')
    for permission in PERMISSIONS - {'owner:recover'}:
        ceiling(world, permission)
    with world.store.transaction() as connection:
        assert not world.control.is_complete_owner_on(connection, actor, now=NOW)
    ceiling(world, 'owner:recover')
    with world.store.transaction() as connection:
        assert world.control.is_complete_owner_on(connection, actor, now=NOW)
    world.update('access_groups', 'owners', enabled=0)
    with world.store.transaction() as connection:
        assert not world.control.is_complete_owner_on(connection, actor, now=NOW)


def test_dominance_uses_disabled_targets_full_group_authority_not_key_ceiling(world):
    actor = world.user('alice')
    world.user('target')
    key_context(world, principal='target')
    world.role('low', ['users:manage', 'fax:read'])
    world.assignment('alice', 'low')
    world.insert('access_groups', id='high', name='high', normalized_name='high', description='')
    world.insert('access_memberships', group_id='high', principal_id='target')
    world.role('high-role', ['fax:document'])
    world.assignment(None, 'high-role', group='high')
    world.update('access_principals', 'target', enabled=0)
    with world.store.transaction() as connection:
        assert not world.control.dominates_principal_on(connection, actor, 'target', now=NOW).allowed
    world.update('access_groups', 'high', enabled=0)
    with world.store.transaction() as connection:
        assert world.control.dominates_principal_on(connection, actor, 'target', now=NOW).allowed


def test_root_send_authority_can_delegate_while_execution_stays_creator_bound(world):
    from api.app.access.types import ScopedPermission
    actor = world.user('alice')
    world.user('bob')
    world.role('sender', ['fax:send', 'settings:write', 'mailboxes:manage'])
    world.assignment('alice', 'sender', 'personal-alice')
    with world.store.transaction() as connection:
        assert not world.control.can_grant_on(connection, actor, (ScopedPermission('fax:send', ResourceRef('personal-bob')),), now=NOW).allowed
        assert not world.control.can_grant_on(connection, actor, (ScopedPermission('settings:write', ResourceRef('personal-alice')),), now=NOW).allowed
        assert not world.control.authorize_on(connection, actor, 'settings:write', ResourceRef('installation'), now=NOW).allowed
    world.assignment('alice', 'sender')
    with world.store.transaction() as connection:
        assert world.control.can_grant_on(connection, actor, (ScopedPermission('fax:send', ResourceRef('personal-bob')),), now=NOW).allowed
        assert not world.control.authorize_on(connection, actor, 'fax:send', ResourceRef('personal-bob'), now=NOW).allowed


def test_visibility_reads_live_assignments_before_pagination(world):
    actor = world.user('alice')
    world.role('reader', ['fax:read'])
    for identity in ('a-hidden', 'b-visible', 'c-visible'):
        resource = world.outbound(identity)
        if identity != 'a-hidden':
            world.assignment('alice', 'reader', resource.id)
    with world.store.transaction() as connection:
        query = world.control.visible_resource_ids_on(connection, actor, 'fax:read', 'outbound', now=NOW)
        assert connection.execute(query.order_by('id').limit(1)).scalars().all() == ['resource-b-visible']
        assignment = world.tables['access_assignments']
        connection.execute(assignment.delete().where(assignment.c.resource_id == 'resource-b-visible'))
        assert connection.execute(query.order_by('id').limit(1)).scalars().all() == ['resource-c-visible']
        users = world.tables['access_users']
        connection.execute(users.update().where(users.c.id == 'alice').values(password_change_required=1))
        assert connection.execute(query).scalars().all() == []


@pytest.mark.parametrize('change', ['role', 'key', 'session', 'principal', 'resource'])
def test_visibility_contains_live_source_and_policy_predicates(world, change):
    world.user('alice')
    actor = key_context(world, session=True)
    fax = world.outbound('fax')
    world.assignment('alice', 'role_fax_viewer')
    ceiling(world, 'fax:read')
    table, identity, values = {
        'role': ('access_roles', 'role_fax_viewer', {'enabled': 0}),
        'key': ('access_key_bindings', 'binding', {'security_version': 2}),
        'session': ('access_sessions', 'key-session', {'revoked_at': NOW}),
        'principal': ('access_principals', 'alice', {'security_version': 2}),
        'resource': ('access_resources', fax.id, {'enabled': 0}),
    }[change]
    with world.store.transaction() as connection:
        query = world.control.visible_resource_ids_on(connection, actor, 'fax:read', 'outbound', now=NOW)
        assert connection.execute(query).scalars().all() == [fax.id]
        target = world.tables[table]
        connection.execute(target.update().where(target.c.id == identity).values(**values))
        assert connection.execute(query).scalars().all() == []


@pytest.mark.parametrize('damage', ['missing_parent', 'wrong_parent_kind', 'extra_depth', 'missing_fax', 'unknown_kind'])
def test_corrupt_or_unknown_ancestry_fails_closed_on_actual_rows(world, damage):
    actor = world.user('alice')
    fax = world.outbound('fax', 'personal-alice', 'personal')
    world.assignment('alice', 'role_owner')
    values = {
        'missing_parent': {'parent_id': 'absent'},
        'wrong_parent_kind': {'parent_kind': 'mailbox'},
        'extra_depth': {'parent_id': fax.id, 'parent_kind': 'outbound'},
        'missing_fax': {'fax_job_id': 'absent'},
        'unknown_kind': {'kind': 'mystery'},
    }[damage]
    with world.engine.connect() as connection:
        if world.engine.dialect.name == 'sqlite':
            connection.exec_driver_sql('PRAGMA foreign_keys = OFF')
            connection.exec_driver_sql('PRAGMA ignore_check_constraints = ON')
            connection.commit()
        else:
            for name in ('fk_access_resources_parent', 'fk_access_resources_fax_job', 'ck_access_resources_shape'):
                connection.exec_driver_sql('ALTER TABLE access_resources DROP CONSTRAINT ' + name)
        resource = world.tables['access_resources']
        connection.execute(resource.update().where(resource.c.id == fax.id).values(**values))
        connection.commit()
        if world.engine.dialect.name == 'sqlite':
            connection.exec_driver_sql('PRAGMA foreign_keys = ON')
            connection.exec_driver_sql('PRAGMA ignore_check_constraints = OFF')
            connection.commit()
    with world.store.transaction() as connection:
        assert not world.control.authorize_on(connection, actor, 'fax:read', fax, now=NOW).allowed
        assert connection.execute(world.control.visible_resource_ids_on(connection, actor, 'fax:read', 'outbound', now=NOW)).scalars().all() == []


@pytest.mark.parametrize('bad', ['dictionary', 'epoch_bool', 'evidence_dict', 'session_id_type', 'long_id', 'aware_time'])
def test_malformed_trusted_objects_fail_closed_with_safe_authentication_error(world, bad):
    from dataclasses import replace
    from datetime import timezone
    from api.app.access.types import AuthenticationError
    actor = world.user('alice')
    value = {
        'dictionary': {'principal_id': 'alice'},
        'epoch_bool': replace(actor, principal_security_version=True),
        'evidence_dict': replace(actor, credential={'session_id': 'session-alice'}),
        'session_id_type': replace(actor, credential=PasswordSessionEvidence(123, 1)),
        'long_id': replace(actor, principal_id='private-input' * 100),
        'aware_time': actor,
    }[bad]
    now = NOW.replace(tzinfo=timezone.utc) if bad == 'aware_time' else NOW
    with pytest.raises(AuthenticationError) as caught:
        world.control.authorize(value, 'fax:read', ResourceRef('absent'), now=now)
    assert str(caught.value) == 'unauthenticated'
    assert 'private-input' not in repr(value)


def test_cross_store_revocation_is_seen_by_subsequent_operation(world):
    from api.app.access.types import StaleCredentialError
    world.user('alice')
    actor = key_context(world, session=True)
    world.assignment('alice', 'role_fax_viewer')
    ceiling(world, 'fax:read')
    fax = world.outbound('fax')
    other = AccessControl(AccessStore(world.engine))
    assert other.authorize(actor, 'fax:read', fax, now=NOW).allowed
    with world.store.transaction() as connection:
        binding = world.tables['access_key_bindings']
        connection.execute(binding.update().where(binding.c.id == 'binding').values(state='revoked', revoked_at=NOW))
    with pytest.raises(StaleCredentialError):
        other.authorize(actor, 'fax:read', fax, now=NOW)


def test_policy_reads_do_not_touch_session_idle_audit_or_policy_versions(world):
    actor = world.user('alice')
    world.assignment('alice', 'role_owner')
    fax = world.outbound('fax')
    with world.store.transaction() as connection:
        before = connection.execute(sa.text("SELECT last_used_at FROM access_sessions WHERE id='session-alice'")).scalar_one()
        audits = connection.execute(sa.text('SELECT COUNT(*) FROM access_audit')).scalar_one()
        world.control.authorize_on(connection, actor, 'fax:read', fax, now=NOW)
        world.control.effective_access_on(connection, actor, fax, now=NOW)
        world.control.is_complete_owner_on(connection, actor, now=NOW)
        assert world.store.require_lock_on(connection) == 1
        assert connection.execute(sa.text("SELECT last_used_at FROM access_sessions WHERE id='session-alice'")).scalar_one() == before
        assert connection.execute(sa.text('SELECT COUNT(*) FROM access_audit')).scalar_one() == audits
        state = world.tables['access_state']
        connection.execute(state.update().values(policy_version=2))
        assert world.control.authorize_on(connection, actor, 'fax:read', fax, now=NOW).policy_version == 2


def test_live_visibility_cannot_be_manufactured_from_mailbox_labels_or_scopes(world):
    actor = world.user('alice')
    world.insert('mailboxes', id='box', label='Owner', allowed_scopes='*')
    world.resource('mailbox-box', 'mailbox', 'installation', 'installation', mailbox_id='box')
    world.insert('inbound_faxes', id='incoming', status='received', backend='sip', mailbox_label='Owner', received_at=NOW)
    world.resource('resource-incoming', 'inbound', 'mailbox-box', 'mailbox', inbound_fax_id='incoming')
    with world.store.transaction() as connection:
        assert connection.execute(world.control.visible_resource_ids_on(connection, actor, 'inbound:list', 'inbound', now=NOW)).scalars().all() == []


def test_every_connection_first_method_requires_the_callers_existing_lock(world):
    from api.app.access.types import InvalidTransactionError
    actor = world.user('alice')
    world.assignment('alice', 'role_owner')
    with world.engine.begin() as connection:
        for method in [
            lambda: world.control.authorize_on(connection, actor, 'settings:read', ResourceRef('installation'), now=NOW),
            lambda: world.control.effective_access_on(connection, actor, ResourceRef('installation'), now=NOW),
            lambda: world.control.visible_resource_ids_on(connection, actor, 'fax:read', 'outbound', now=NOW),
            lambda: world.control.can_grant_on(connection, actor, (), now=NOW),
            lambda: world.control.dominates_principal_on(connection, actor, 'alice', now=NOW),
            lambda: world.control.is_complete_owner_on(connection, actor, now=NOW),
        ]:
            with pytest.raises(InvalidTransactionError):
                method()
        world.store.lock_on(connection)
        state = world.tables['access_state']
        connection.execute(state.update().values(policy_version=2))
        assert world.control.authorize_on(connection, actor, 'settings:read', ResourceRef('installation'), now=NOW).policy_version == 2
        connection.rollback()
    with world.store.transaction() as connection:
        assert world.store.require_lock_on(connection) == 1
