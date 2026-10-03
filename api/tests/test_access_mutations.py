"""Mutation contracts on isolated migrated real databases; no route acceptance."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import timedelta, timezone
import importlib
import inspect
import json

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_policy import World, NOW
from api.app.access.credentials import CredentialCodec
from api.app.access.catalog import PERMISSIONS, BUILTIN_ROLE_PERMISSIONS
from api.app.access.policy import AccessControl
from api.app.access.store import AccessStore
from api.app.access.types import (AccessUnavailableError, BootstrapEvidence,
    InvalidTransactionError, PrincipalContext, ResourceRef, ScopedPermission,
    StaleCredentialError)

try:
    M = importlib.import_module('api.app.access.mutations')
    T = importlib.import_module('api.app.access.mutation_types')
except ModuleNotFoundError:
    M = T = None

# Structurally valid bounded hash: counting usable Owners never invokes a KDF.
HASH = 'scrypt$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA$n=16384$r=8$p=1'


class MutationWorld(World):
    def __init__(self, engine):
        super().__init__(engine)
        self.codec = CredentialCodec()
        self.actor = self.user('alice')
        self.assignment('alice', 'role_owner')
        self.mutations = M.AccessMutations(self.store, self.control, self.codec)

    def user(self, identity):
        actor = super().user(identity)
        self.update('access_users', identity, password_hash=HASH)
        return actor

    def row(self, table, identity):
        with self.engine.connect() as c:
            t = self.tables[table]
            return dict(c.execute(sa.select(t).where(t.c.id == identity)).mappings().one())

    def rows(self, table):
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(sa.select(self.tables[table])).mappings()]

    def version(self, table, identity):
        return T.VersionedEntity(identity, self.row(table, identity)['version'])

    def policy(self):
        return self.row('access_state', 'state')['policy_version']

    def call(self, method, *args, actor=None, expected=None, now=NOW):
        return getattr(self.mutations, method)(actor or self.actor, *args,
            expected_policy_version=self.policy() if expected is None else expected, now=now)

    def restricted(self, identity, permissions):
        actor = self.user(identity)
        self.role('role-' + identity, permissions)
        self.assignment(identity, 'role-' + identity)
        return actor

    def group(self, identity, enabled=1):
        return self.insert('access_groups', id=identity, name=identity,
            normalized_name=identity, description='', enabled=enabled)

    def bootstrap(self):
        self.control = AccessControl(self.store,
            current_bootstrap_fingerprint_on=lambda c: 'f' * 64)
        self.mutations = M.AccessMutations(self.store, self.control, self.codec)
        return PrincipalContext('bootstrap', 1, BootstrapEvidence('f' * 64), 'key:env')

    def key(self, principal='alice', ceiling=None, expires=None):
        prepared = self.codec.prepare_new_key()
        value = T.KeyValues(self.version('access_principals', principal), 'safe name',
            'private-note', expires, ceiling if ceiling is not None else ())
        return self.call('issue_key', value, prepared), prepared


@pytest.fixture
def mworld(database):
    assert M is not None, 'Task 2B2 mutation module is not implemented'
    return MutationWorld(database)


def denied(world, reason, method, *args, **kwargs):
    before_ids = {r['id'] for r in world.rows('access_audit')}
    with pytest.raises(T.MutationDeniedError) as caught:
        world.call(method, *args, **kwargs)
    assert caught.value.reason == reason
    assert str(caught.value) == reason.value
    new = [r for r in world.rows('access_audit') if r['id'] not in before_ids]
    assert len(new) == 1
    audit = new[0]
    assert audit['outcome'] == 'denied'
    assert audit['policy_version_before'] == audit['policy_version_after']
    assert json.loads(audit['details']) == {'reason': reason.value}
    return audit


def test_mutation_module_exists():
    assert M is not None, 'Task 2B2 mutation module is not implemented'


def test_creations_are_atomic_versioned_and_inert(mworld):
    w = mworld
    prepared = w.codec.prepare_temporary_password()
    user = w.call('create_user', T.UserValues('  Case.User  ', 'Private name', True), prepared)
    p, u = w.row('access_principals', user.target.id), w.row('access_users', user.target.id)
    assert (p['kind'], p['version'], p['security_version']) == ('user', 1, 1)
    assert (u['login'], u['normalized_login'], u['password_change_required']) == ('Case.User', 'case.user', 1)
    assert w.codec.verify(prepared._temporary_secret_for_committed_adapter(), u['password_hash'])
    personal = [r for r in w.rows('access_resources') if r['principal_id'] == p['id']]
    assert len(personal) == 1 and personal[0]['enabled'] == 1
    assert not [r for r in w.rows('access_assignments') if r['principal_id'] == p['id']]
    integration = w.call('create_integration', T.IntegrationValues('Robot', False))
    assert w.row('access_principals', integration.target.id)['kind'] == 'integration'
    assert not [r for r in w.rows('api_keys') if r['owner'] == integration.target.id]
    group = w.call('create_group', T.GroupValues('  Ａlpha  ', 'description', True))
    assert w.row('access_groups', group.target.id)['normalized_name'] == 'alpha'
    role = w.call('create_custom_role', T.CustomRoleValues('Unused', '', False, frozenset({'host:terminal'})))
    assert w.row('access_roles', role.target.id)['kind'] == 'custom'
    assert [user.policy_version, integration.policy_version, group.policy_version, role.policy_version] == [2, 3, 4, 5]
    assert all(r.changed for r in [user, integration, group, role])
    for receipt in [user, integration, group, role]:
        assert set(asdict(receipt)) == {'target', 'policy_version', 'changed', 'related'}


def test_metadata_noops_do_not_bump_and_identity_edits_have_exact_effects(mworld):
    w = mworld
    w.user('bob')
    target = w.version('access_principals', 'bob')
    noop = w.call('update_user', target, T.UserValues('bob', 'bob', True))
    assert not noop.changed and noop.target == target and w.policy() == 1
    display = w.call('update_user', target, T.UserValues('bob', 'New display', True))
    assert display.target.version == 2 and w.row('access_principals', 'bob')['security_version'] == 1
    assert w.row('access_sessions', 'session-bob')['revoked_at'] is None
    key, _ = w.key('bob')
    changed = w.call('update_user', w.version('access_principals', 'bob'), T.UserValues('new.bob', 'New display', True))
    assert changed.target.version == 3 and w.row('access_principals', 'bob')['security_version'] == 2
    assert w.row('access_sessions', 'session-bob')['revoked_at'] == NOW
    assert w.row('access_key_bindings', key.target.id)['state'] == 'active'


def test_reset_and_disable_permanently_revoke_sources_then_enable_does_not_revive(mworld):
    w = mworld
    w.user('bob')
    key, _ = w.key('bob')
    prepared = w.codec.prepare_temporary_password()
    receipt = w.call('reset_password', w.version('access_principals', 'bob'), prepared)
    assert receipt.target.version == 2
    assert w.row('access_users', 'bob')['password_version'] == 2
    assert w.row('access_users', 'bob')['password_change_required'] == 1
    assert w.row('access_principals', 'bob')['security_version'] == 2
    binding = w.row('access_key_bindings', key.target.id)
    assert (binding['state'], binding['version'], binding['security_version']) == ('revoked', 2, 2)
    assert w.row('api_keys', key.target.id)['revoked_at'] == NOW
    assert w.row('access_sessions', 'session-bob')['revoked_at'] == NOW
    w.call('update_user', w.version('access_principals', 'bob'), T.UserValues('bob', 'bob', False))
    w.call('update_user', w.version('access_principals', 'bob'), T.UserValues('bob', 'bob', True))
    assert w.row('access_principals', 'bob')['security_version'] == 4
    assert w.row('access_key_bindings', key.target.id)['state'] == 'revoked'


def test_integration_disable_revokes_key_and_session_without_touching_faxes(mworld):
    w = mworld
    receipt = w.call('create_integration', T.IntegrationValues('Robot', True))
    key, _ = w.key(receipt.target.id)
    fax = w.outbound('accepted')
    before = w.row('fax_jobs', 'accepted')
    w.call('update_integration', receipt.target, T.IntegrationValues('Robot', False))
    assert w.row('access_key_bindings', key.target.id)['state'] == 'revoked'
    assert w.row('fax_jobs', 'accepted') == before
    assert w.row('access_resources', fax.id)['enabled'] == 1


@pytest.mark.parametrize('method', ['update_user', 'reset_password', 'issue_key'])
def test_identity_takeover_uses_full_dominance_and_protected_disabled_owner_edges(mworld, method):
    w = mworld
    actor = w.restricted('manager', {'users:manage', 'keys:manage'})
    w.user('bob')
    w.role('high', ['host:terminal'])
    w.assignment('bob', 'high')
    target = w.version('access_principals', 'bob')
    args = {'update_user': (target, T.UserValues('bob', 'rename', True)),
        'reset_password': (target, w.codec.prepare_temporary_password()),
        'issue_key': (T.KeyValues(target, None, None, None, ()), w.codec.prepare_new_key())}[method]
    denied(w, T.MutationReason.FORBIDDEN, method, *args, actor=actor)
    # Disabled Owner group edges stay protected against staged credential takeover.
    w.group('owners', enabled=0)
    w.insert('access_memberships', group_id='owners', principal_id='bob')
    w.assignment(None, 'role_owner', group='owners')
    actor = w.restricted('all-custom', PERMISSIONS)
    denied(w, T.MutationReason.OWNER_REQUIRED, method, *args, actor=actor)


def test_membership_authorizes_only_affected_edges_and_bumps_group(mworld):
    w = mworld
    actor = w.restricted('manager', {'groups:manage', 'fax:read'})
    w.user('bob')
    w.assignment('bob', 'role_host_operator')
    w.group('readers')
    w.role('read', ['fax:read'])
    w.assignment(None, 'read', group='readers')
    added = w.call('add_membership', w.version('access_groups', 'readers'), w.version('access_principals', 'bob'), actor=actor)
    assert added.target.version == 1 and added.related == (T.VersionedEntity('readers', 2),)
    removed = w.call('remove_membership', added.target, w.version('access_groups', 'readers'), actor=actor)
    assert removed.target == added.target and removed.related == (T.VersionedEntity('readers', 3),)
    assert not w.rows('access_memberships')


@pytest.mark.parametrize('action', ['role_expand', 'role_remove', 'role_enable', 'group_enable', 'membership_add', 'membership_remove', 'assignment_add', 'assignment_remove'])
def test_affected_edge_coverage_includes_disabled_roles_groups_and_old_edges(mworld, action):
    w = mworld
    actor = w.restricted('manager', {'roles:manage', 'groups:manage', 'grants:manage', 'fax:read'})
    w.user('bob')
    w.group('disabled', enabled=0)
    membership = w.insert('access_memberships', group_id='disabled', principal_id='bob')
    w.role('bounded', ['fax:document'])
    w.update('access_roles', 'bounded', enabled=0)
    assignment = w.assignment(None, 'bounded', group='disabled')
    actions = {
        'role_expand': ('update_custom_role', w.version('access_roles', 'bounded'), T.CustomRoleValues('bounded', '', False, frozenset({'fax:read', 'fax:document'}))),
        'role_remove': ('update_custom_role', w.version('access_roles', 'bounded'), T.CustomRoleValues('bounded', '', False, frozenset({'fax:read'}))),
        'role_enable': ('update_custom_role', w.version('access_roles', 'bounded'), T.CustomRoleValues('bounded', '', True, frozenset({'fax:document'}))),
        'group_enable': ('update_group', w.version('access_groups', 'disabled'), T.GroupValues('disabled', '', True)),
        'membership_add': ('add_membership', w.version('access_groups', 'disabled'), w.version('access_principals', 'alice')),
        'membership_remove': ('remove_membership', w.version('access_memberships', membership), w.version('access_groups', 'disabled')),
        'assignment_add': ('create_assignment', T.AssignmentValues(T.PrincipalSubject(w.version('access_principals', 'bob')), w.version('access_roles', 'bounded'), ResourceRef('installation'))),
        'assignment_remove': ('remove_assignment', w.version('access_assignments', assignment)),
    }
    before = w.policy()
    denied(w, T.MutationReason.FORBIDDEN, *actions[action], actor=actor)
    assert w.policy() == before and w.row('access_roles', 'bounded')['version'] == 1
    assert w.row('access_groups', 'disabled')['version'] == 1


def test_unassigned_role_is_inert_and_metadata_edit_of_assigned_role_does_not_require_coverage(mworld):
    w = mworld
    actor = w.restricted('manager', {'roles:manage', 'groups:manage'})
    role = w.call('create_custom_role', T.CustomRoleValues('Power', '', False, frozenset({'host:terminal'})), actor=actor)
    w.call('update_custom_role', role.target, T.CustomRoleValues('Power', '', True, frozenset({'host:actions'})), actor=actor)
    w.assignment('alice', role.target.id)
    w.call('update_custom_role', w.version('access_roles', role.target.id), T.CustomRoleValues('New Power', 'metadata', True, frozenset({'host:actions'})), actor=actor)
    w.group('disabled', 0)
    w.assignment(None, 'role_owner', group='disabled')
    w.call('update_group', w.version('access_groups', 'disabled'), T.GroupValues('Renamed', '', False), actor=actor)


def test_mixed_builtin_scope_projection_and_assignment_removal_receipt(mworld):
    w = mworld
    w.user('bob')
    receipt = w.call('create_assignment', T.AssignmentValues(T.PrincipalSubject(w.version('access_principals', 'bob')), w.version('access_roles', 'role_fax_operator'), ResourceRef('personal-bob')))
    assert receipt.related == (T.VersionedEntity('bob', 2),)
    with w.store.transaction() as c:
        projected = w.control.scope_projection_on(c, BUILTIN_ROLE_PERMISSIONS['role_fax_operator'], ResourceRef('personal-bob'))
        assert projected.inactive == frozenset({'inbound:list', 'inbound:read', 'inbound:document'})
    removed = w.call('remove_assignment', receipt.target)
    assert removed.target == receipt.target and removed.related == (T.VersionedEntity('bob', 3),)
    assert not [r for r in w.rows('access_assignments') if r['id'] == receipt.target.id]


@pytest.mark.parametrize('method', ['update_user', 'update_group', 'remove_membership', 'remove_assignment'])
def test_last_usable_named_owner_is_preserved(mworld, method):
    w = mworld
    bootstrap = w.bootstrap()
    owner_assignment = next(r for r in w.rows('access_assignments') if r['role_id'] == 'role_owner')
    if method in {'update_group', 'remove_membership'}:
        w.group('owners')
        membership = w.insert('access_memberships', group_id='owners', principal_id='alice')
        w.assignment(None, 'role_owner', group='owners')
        with w.engine.begin() as c:
            t = w.tables['access_assignments']
            c.execute(t.delete().where(t.c.id == owner_assignment['id']))
        args = ((w.version('access_groups', 'owners'), T.GroupValues('owners', '', False)) if method == 'update_group'
            else (w.version('access_memberships', membership), w.version('access_groups', 'owners')))
    else:
        args = ((w.version('access_principals', 'alice'), T.UserValues('alice', 'alice', False)) if method == 'update_user'
            else (T.VersionedEntity(owner_assignment['id'], 1),))
    denied(w, T.MutationReason.LAST_OWNER, method, *args, actor=bootstrap)
    assert w.row('access_principals', 'alice')['enabled'] == 1


def test_owner_count_rejects_unsupported_login_hash_integrations_and_custom_all_role(mworld):
    w = mworld
    bootstrap = w.bootstrap()
    w.user('bob')
    w.assignment('bob', 'role_owner')
    w.update('access_users', 'bob', password_hash='unsupported')
    robot = w.call('create_integration', T.IntegrationValues('Robot', True), actor=bootstrap)
    w.assignment(robot.target.id, 'role_owner')
    w.user('custom')
    w.role('everything', PERMISSIONS)
    w.assignment('custom', 'everything')
    denied(w, T.MutationReason.LAST_OWNER, 'update_user', w.version('access_principals', 'alice'), T.UserValues('alice', '', False), actor=bootstrap)
    w.update('access_users', 'bob', password_hash=HASH, login='bad login', normalized_login='bad login')
    denied(w, T.MutationReason.LAST_OWNER, 'update_user', w.version('access_principals', 'alice'), T.UserValues('alice', '', False), actor=bootstrap)
    w.update('access_users', 'bob', login='bob', normalized_login='bob', password_change_required=1)
    receipt = w.call('update_user', w.version('access_principals', 'alice'), T.UserValues('alice', '', False), actor=bootstrap)
    assert receipt.changed


def test_explicit_enrollment_from_zero_owner_and_recovery_password_counts(mworld):
    w = mworld
    bootstrap = w.bootstrap()
    with w.engine.begin() as c:
        t = w.tables['access_assignments']
        c.execute(t.delete())
    prepared = w.codec.prepare_temporary_password()
    owner = w.call('enroll_owner', T.OwnerEnrollment('new.owner', 'New Owner'), prepared, actor=bootstrap)
    assert w.row('access_users', owner.target.id)['password_change_required'] == 1
    assignment = [r for r in w.rows('access_assignments') if r['principal_id'] == owner.target.id]
    assert len(assignment) == 1 and (assignment[0]['role_id'], assignment[0]['resource_id']) == ('role_owner', 'installation')
    denied(w, T.MutationReason.LAST_OWNER, 'update_user', owner.target, T.UserValues('new.owner', '', False), actor=bootstrap)


@pytest.mark.parametrize('role_id', ['role_owner', 'role_fax_operator'])
def test_builtin_definitions_are_immutable(mworld, role_id):
    denied(mworld, T.MutationReason.INVALID_TARGET, 'update_custom_role', mworld.version('access_roles', role_id), T.CustomRoleValues('Changed', '', True, frozenset()))


def test_keys_rotate_with_stable_public_id_ceiling_and_replay_identity(mworld):
    w = mworld
    grants = (ScopedPermission('fax:read', ResourceRef('installation')),)
    receipt, prepared = w.key(ceiling=grants)
    assert isinstance(receipt, T.KeyMutationReceipt)
    assert (receipt.public_key_id, receipt.principal_id, receipt.ceiling) == (prepared.public_key_id, 'alice', grants)
    assert receipt.target.id == prepared.row_id
    old = w.row('api_keys', receipt.target.id)
    rotated_prepared = w.codec.prepare_key_rotation(receipt.public_key_id)
    rotated = w.call('rotate_key', receipt.target, rotated_prepared)
    assert (rotated.public_key_id, rotated.ceiling, rotated.target.version) == (receipt.public_key_id, grants, 2)
    assert w.row('api_keys', receipt.target.id)['key_id'] == old['key_id']
    assert w.row('api_keys', receipt.target.id)['key_hash'] != old['key_hash']
    assert w.row('access_key_bindings', receipt.target.id)['security_version'] == 2
    changed = w.call('update_key_metadata', rotated.target, T.KeyMetadata('Renamed', None, NOW + timedelta(hours=1)))
    assert changed.target.version == 3 and w.row('access_key_bindings', receipt.target.id)['security_version'] == 3
    revoked = w.call('revoke_key', changed.target)
    assert revoked.target.version == 4 and w.row('access_key_bindings', receipt.target.id)['state'] == 'revoked'
    noop = w.call('revoke_key', revoked.target)
    assert not noop.changed and noop.target == revoked.target
    assert 'key_hash' not in asdict(receipt) and 'token' not in repr(receipt)


@pytest.mark.parametrize('damage', ['expired', 'revoked', 'disabled', 'pending', 'id_mismatch'])
def test_rotation_refuses_ineligible_sources(mworld, damage):
    w = mworld
    w.user('bob')
    receipt, _ = w.key('bob')
    prepared = w.codec.prepare_key_rotation(receipt.public_key_id)
    if damage == 'expired':
        w.update('api_keys', receipt.target.id, expires_at=NOW)
    elif damage == 'revoked':
        w.update('api_keys', receipt.target.id, revoked_at=NOW)
    elif damage == 'disabled':
        w.update('access_principals', 'bob', enabled=0)
    elif damage == 'pending':
        w.update('access_key_bindings', receipt.target.id, state='pending_review')
    else:
        prepared = w.codec.prepare_key_rotation('0123456789ab')
    denied(w, T.MutationReason.INVALID_TARGET, 'rotate_key', receipt.target, prepared)
    assert w.row('access_key_bindings', receipt.target.id)['version'] == 1


def test_pending_approval_is_explicit_no_assignment_and_preserves_history(mworld):
    w = mworld
    w.user('bob')
    receipt, _ = w.key()
    w.update('access_key_bindings', receipt.target.id, state='pending_review')
    grants = (ScopedPermission('fax:read', ResourceRef('installation')),)
    approved = w.call('approve_pending_key', receipt.target, w.version('access_principals', 'bob'), grants)
    binding = w.row('access_key_bindings', receipt.target.id)
    assert (binding['principal_id'], binding['state'], binding['security_version']) == ('bob', 'active', 2)
    assert approved.principal_id == 'bob' and approved.ceiling == grants
    assert not [r for r in w.rows('access_assignments') if r['principal_id'] == 'bob']


def test_pending_cannot_rewrite_even_revoked_historical_session_owner(mworld):
    w = mworld
    w.user('bob')
    receipt, _ = w.key()
    w.insert('access_sessions', id='historical', principal_id='alice', source_kind='key',
        source_key_id=receipt.target.id, source_key_version=1, bootstrap_fingerprint=None,
        token_hash='h' * 64, csrf_hash='c' * 64, principal_security_version=1,
        password_version=None, last_used_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1), revoked_at=NOW)
    w.update('access_key_bindings', receipt.target.id, state='pending_review')
    denied(w, T.MutationReason.INVALID_TARGET, 'approve_pending_key', receipt.target, w.version('access_principals', 'bob'), ())
    assert w.row('access_sessions', 'historical')['principal_id'] == 'alice'
    assert w.row('access_key_bindings', receipt.target.id)['principal_id'] == 'alice'


@pytest.mark.parametrize('damage', ['reserved', 'unparseable', 'hash', 'expiry', 'revoked', 'principal'])
def test_pending_invalid_credentials_cannot_activate(mworld, damage):
    w = mworld
    w.user('bob')
    receipt, _ = w.key()
    w.update('access_key_bindings', receipt.target.id, state='pending_review')
    field = {'reserved': ('key_id', 'env'), 'unparseable': ('key_id', 'INVALID'),
        'hash': ('key_hash', 'unsupported'), 'expiry': ('expires_at', NOW),
        'revoked': ('revoked_at', NOW)}
    if damage == 'principal':
        w.update('access_principals', 'bob', enabled=0)
    else:
        name, value = field[damage]
        w.update('api_keys', receipt.target.id, **{name: value})
    denied(w, T.MutationReason.INVALID_TARGET, 'approve_pending_key', receipt.target, w.version('access_principals', 'bob'), ())


@pytest.mark.parametrize('bad', ['dict', 'bool_version', 'bool_policy', 'aware_now', 'bad_login', 'long_display', 'control_name', 'long_normalized', 'unknown_permission', 'mutable_permissions', 'duplicate_ceiling', 'invalid_scope', 'chosen_for_temporary', 'new_for_rotation', 'rotation_for_new', 'missing_password', 'missing_login'])
def test_exact_typed_bounded_inputs_deny_before_business_writes(mworld, bad):
    w = mworld
    base = ('create_integration', T.IntegrationValues('Robot', True))
    options = {}
    if bad == 'dict': base = ('create_integration', {'display_name': 'private-marker', 'enabled': True})
    elif bad == 'bool_version': base = ('update_user', T.VersionedEntity('alice', True), T.UserValues('alice', 'alice', True))
    elif bad == 'bool_policy': options['expected'] = True
    elif bad == 'aware_now': options['now'] = NOW.replace(tzinfo=timezone.utc)
    elif bad == 'bad_login': base = ('create_user', T.UserValues('bad login', '', True), w.codec.prepare_temporary_password())
    elif bad == 'long_display': base = ('create_integration', T.IntegrationValues('x' * 201, True))
    elif bad == 'control_name': base = ('create_group', T.GroupValues('private\nmarker', '', True))
    elif bad == 'long_normalized': base = ('create_group', T.GroupValues('ß' * 51, '', True))
    elif bad in {'unknown_permission', 'mutable_permissions'}:
        permissions = frozenset({'unknown'}) if bad == 'unknown_permission' else {'fax:read'}
        base = ('create_custom_role', T.CustomRoleValues('Role', '', True, permissions))
    elif bad in {'duplicate_ceiling', 'invalid_scope'}:
        grant = ScopedPermission('settings:read', ResourceRef('personal-alice')) if bad == 'invalid_scope' else ScopedPermission('fax:read', ResourceRef('installation'))
        grants = (grant,) if bad == 'invalid_scope' else (grant, grant)
        base = ('issue_key', T.KeyValues(w.version('access_principals', 'alice'), None, None, None, grants), w.codec.prepare_new_key())
    elif bad == 'missing_password': base = ('create_user', T.UserValues('bob', '', True), None)
    elif bad == 'missing_login': base = ('create_user', T.UserValues(None, '', True), w.codec.prepare_temporary_password())
    elif bad == 'chosen_for_temporary': base = ('create_user', T.UserValues('bob', '', True), w.codec.prepare_chosen_password('chosen-password-value'))
    elif bad == 'rotation_for_new': base = ('issue_key', T.KeyValues(w.version('access_principals', 'alice'), None, None, None, ()), w.codec.prepare_key_rotation('123456789abc'))
    elif bad == 'new_for_rotation':
        receipt, _ = w.key()
        base = ('rotate_key', receipt.target, w.codec.prepare_new_key())
    before = w.policy(), len(w.rows('access_principals')), len(w.rows('api_keys'))
    denied(w, T.MutationReason.INVALID_INPUT, *base, **options)
    assert (w.policy(), len(w.rows('access_principals')), len(w.rows('api_keys'))) == before


def test_duplicates_stale_versions_and_missing_relationships_are_safe(mworld):
    w = mworld
    w.user('bob')
    denied(w, T.MutationReason.DUPLICATE, 'create_user', T.UserValues(' BoB ', '', True), w.codec.prepare_temporary_password())
    with pytest.raises(T.StaleVersionError):
        w.call('update_user', T.VersionedEntity('bob', 2), T.UserValues('bob', '', True))
    with pytest.raises(T.StaleVersionError):
        w.call('create_integration', T.IntegrationValues('', True), expected=2)
    denied(w, T.MutationReason.INVALID_TARGET, 'update_user', T.VersionedEntity('absent', 1), T.UserValues('hidden-name', '', True))
    w.group('one'); w.group('two')
    member = w.insert('access_memberships', group_id='one', principal_id='bob')
    denied(w, T.MutationReason.INVALID_TARGET, 'remove_membership', w.version('access_memberships', member), w.version('access_groups', 'two'))
    assert w.row('access_memberships', member)['group_id'] == 'one'


def test_denial_audit_commits_and_invalid_evidence_has_null_actor_references(mworld):
    w = mworld
    actor = w.user('bob')
    audit = denied(w, T.MutationReason.FORBIDDEN, 'create_integration', T.IntegrationValues('private-marker', True), actor=actor)
    assert audit['actor_principal_id'] == 'bob' and audit['actor_session_id'] == 'session-bob'
    invalid = replace(actor, principal_id='forged-private-id')
    with pytest.raises(StaleCredentialError):
        w.call('create_integration', T.IntegrationValues('private-marker', True), actor=invalid)
    audit = w.rows('access_audit')[-1]
    assert all(audit[field] is None for field in ('actor_principal_id', 'actor_key_binding_id', 'actor_session_id'))
    for row in w.rows('access_audit'):
        assert len(row['details'].encode('utf8')) <= 2048
        assert not any(value in row['details'] for value in ('private-marker', 'forged-private-id', HASH))


def audit_failure(w):
    with w.engine.begin() as c:
        if w.engine.dialect.name == 'sqlite':
            c.exec_driver_sql("CREATE TRIGGER fail_access_audit BEFORE INSERT ON access_audit BEGIN SELECT RAISE(ABORT, 'private-storage-marker'); END")
        else:
            c.exec_driver_sql("CREATE FUNCTION fail_access_audit_fn() RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'private-storage-marker'; END $$")
            c.exec_driver_sql('CREATE TRIGGER fail_access_audit BEFORE INSERT ON access_audit FOR EACH ROW EXECUTE FUNCTION fail_access_audit_fn()')


@pytest.mark.parametrize('allowed', [True, False])
def test_audit_failure_rolls_back_all_writes_and_returns_fixed_failure(mworld, allowed):
    w = mworld
    actor = w.actor if allowed else w.user('bob')
    before = w.policy(), len(w.rows('access_principals')), len(w.rows('access_audit'))
    audit_failure(w)
    with pytest.raises(AccessUnavailableError) as caught:
        w.call('create_integration', T.IntegrationValues('private-marker', True), actor=actor)
    assert str(caught.value) == 'access_unavailable'
    assert caught.value.__context__ is None
    assert (w.policy(), len(w.rows('access_principals')), len(w.rows('access_audit'))) == before


def test_on_contract_requires_exact_existing_lock_and_leaves_commit_to_owner(mworld):
    w = mworld
    other = AccessStore(w.engine)
    with pytest.raises(InvalidTransactionError): M.AccessMutations(other, w.control, w.codec)
    with w.engine.begin() as c:
        with pytest.raises(InvalidTransactionError):
            w.mutations.create_integration_on(c, w.actor, T.IntegrationValues('', True), expected_policy_version=1, now=NOW)
    with w.store.transaction() as c:
        outcome = w.mutations.create_integration_on(c, w.actor, T.IntegrationValues('Robot', True), expected_policy_version=1, now=NOW)
        assert outcome.receipt and not outcome.reason
        c.rollback()
    assert w.policy() == 1 and len(w.rows('access_principals')) == 2
    # An aggregate with earlier writes must explicitly roll back on a denied outcome.
    with w.store.transaction() as c:
        outcome = w.mutations.create_integration_on(c, w.actor, T.IntegrationValues('First', True), expected_policy_version=1, now=NOW)
        denial = w.mutations.create_integration_on(c, w.actor, T.IntegrationValues('Second', True), expected_policy_version=1, now=NOW)
        assert denial.reason == T.MutationReason.STALE_VERSION
        c.rollback()
    assert w.policy() == 1 and len(w.rows('access_principals')) == 2


def test_post_commit_disclosure_is_owned_by_adapter_and_never_mutation_outcome(mworld, monkeypatch):
    w = mworld
    prepared = w.codec.prepare_new_key()
    original = type(prepared)._token_for_committed_adapter
    disclosures = []
    def disclose(self):
        disclosures.append(self.public_key_id)
        assert w.row('access_key_bindings', self.row_id)['state'] == 'active'
        return original(self)
    monkeypatch.setattr(type(prepared), '_token_for_committed_adapter', disclose)
    values = T.KeyValues(w.version('access_principals', 'alice'), None, None, None, ())
    receipt = w.call('issue_key', values, prepared)
    assert disclosures == [] and 'token' not in asdict(receipt)
    # This test is the owning adapter: the standalone call returned after commit.
    token = prepared._token_for_committed_adapter()
    assert disclosures == [prepared.public_key_id] and token.startswith('fbk_live_')
    denied_prepared = w.codec.prepare_new_key()
    actor = w.user('bob')
    denied(w, T.MutationReason.FORBIDDEN, 'issue_key', values, denied_prepared, actor=actor)
    assert disclosures == [prepared.public_key_id]
    rollback_prepared = w.codec.prepare_new_key()
    with w.store.transaction() as c:
        outcome = w.mutations.issue_key_on(c, w.actor, values, rollback_prepared,
            expected_policy_version=w.store.require_lock_on(c), now=NOW)
        assert outcome.receipt is not None and 'token' not in asdict(outcome)
        c.rollback()
    assert not [r for r in w.rows('api_keys') if r['id'] == rollback_prepared.row_id]
    assert disclosures == [prepared.public_key_id]


def test_unknown_commit_outcome_discards_disclosure_and_never_retries(mworld, monkeypatch):
    w = mworld
    prepared = w.codec.prepare_new_key()
    values = T.KeyValues(w.version('access_principals', 'alice'), None, None, None, ())
    calls = []
    real_transaction = w.store.transaction
    @contextmanager
    def unknown_commit():
        calls.append('transaction')
        with real_transaction() as c:
            yield c
        raise sa.exc.OperationalError('private-storage-marker', {}, Exception('private-storage-marker'))
    monkeypatch.setattr(w.store, 'transaction', unknown_commit)
    with pytest.raises(AccessUnavailableError) as caught:
        w.call('issue_key', values, prepared)
    assert calls == ['transaction'] and caught.value.__context__ is None
    assert w.row('access_key_bindings', prepared.row_id)['state'] == 'active'
    assert not hasattr(caught.value, 'receipt')


def test_active_legacy_key_metadata_does_not_infer_or_repair_credential_authority(mworld):
    w = mworld
    receipt, _ = w.key()
    w.update('api_keys', receipt.target.id, key_id='legacy-safe-id', key_hash='unsupported-import')
    changed = w.call('update_key_metadata', receipt.target, T.KeyMetadata('Safe metadata', None, None))
    assert changed.target.version == 2
    assert w.row('api_keys', receipt.target.id)['key_hash'] == 'unsupported-import'
    assert w.row('access_key_bindings', receipt.target.id)['security_version'] == 1
    assert w.row('access_key_bindings', receipt.target.id)['state'] == 'active'


@pytest.mark.parametrize('damage', ['expired', 'revoked'])
def test_metadata_cannot_revive_credential(mworld, damage):
    w = mworld
    receipt, _ = w.key()
    if damage == 'expired':
        w.update('api_keys', receipt.target.id, expires_at=NOW)
    else:
        w.call('revoke_key', receipt.target)
    denied(w, T.MutationReason.INVALID_TARGET, 'update_key_metadata', w.version('access_key_bindings', receipt.target.id), T.KeyMetadata(None, None, NOW + timedelta(days=1)))


def test_reset_required_actor_is_denied_with_verified_audit_identity(mworld):
    w = mworld
    w.update('access_users', 'alice', password_change_required=1)
    audit = denied(w, T.MutationReason.RESET_REQUIRED, 'create_integration', T.IntegrationValues('private-marker', True))
    assert audit['actor_principal_id'] == 'alice'
    assert w.policy() == 1


def test_role_group_and_membership_changes_leave_security_epochs_and_sessions_intact(mworld):
    w = mworld
    w.user('bob')
    group = w.call('create_group', T.GroupValues('Readers', '', True))
    role = w.call('create_custom_role', T.CustomRoleValues('Read', '', True, frozenset({'fax:read'})))
    assignment = w.call('create_assignment', T.AssignmentValues(T.GroupSubject(group.target), role.target, ResourceRef('installation')))
    member = w.call('add_membership', assignment.related[0], w.version('access_principals', 'bob'))
    w.call('update_custom_role', role.target, T.CustomRoleValues('Read', '', True, frozenset({'fax:document'})))
    assert w.row('access_principals', 'bob')['version'] == 1
    assert w.row('access_principals', 'bob')['security_version'] == 1
    assert w.row('access_sessions', 'session-bob')['revoked_at'] is None
    assert member.related[0].version == 3


def test_empty_projection_is_inert_but_invalid_ancestry_still_rejects(mworld):
    w = mworld
    actor = w.restricted('manager', {'roles:manage', 'grants:manage'})
    w.user('bob')
    role = w.call('create_custom_role', T.CustomRoleValues('Inert at personal', '', True, frozenset({'host:terminal'})), actor=actor)
    added = w.call('create_assignment', T.AssignmentValues(T.PrincipalSubject(w.version('access_principals', 'bob')), role.target, ResourceRef('personal-bob')), actor=actor)
    assert added.changed
    denied(w, T.MutationReason.INVALID_INPUT, 'create_assignment', T.AssignmentValues(T.PrincipalSubject(w.version('access_principals', 'bob')), role.target, ResourceRef('absent')), actor=actor)


@pytest.mark.parametrize('change', ['rotate', 'expiry', 'revoke', 'reset', 'disable'])
def test_key_source_sessions_are_revoked_on_credential_changes(mworld, change):
    w = mworld
    w.user('bob')
    receipt, _ = w.key('bob')
    w.insert('access_sessions', id='device-session', principal_id='bob', source_kind='key',
        source_key_id=receipt.target.id, source_key_version=1, bootstrap_fingerprint=None,
        token_hash='device-token-hash', csrf_hash='device-csrf-hash', principal_security_version=1,
        password_version=None, last_used_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(hours=1), revoked_at=None)
    if change == 'rotate':
        w.call('rotate_key', receipt.target, w.codec.prepare_key_rotation(receipt.public_key_id))
    elif change == 'expiry':
        w.call('update_key_metadata', receipt.target, T.KeyMetadata(None, None, NOW + timedelta(hours=2)))
    elif change == 'revoke':
        w.call('revoke_key', receipt.target)
    elif change == 'reset':
        w.call('reset_password', w.version('access_principals', 'bob'), w.codec.prepare_temporary_password())
    else:
        w.call('update_user', w.version('access_principals', 'bob'), T.UserValues('bob', 'bob', False))
    assert w.row('access_sessions', 'device-session')['revoked_at'] == NOW
    assert w.row('access_key_bindings', receipt.target.id)['security_version'] == 2


def test_pending_same_owner_revokes_existing_sessions_without_rewriting_them(mworld):
    w = mworld
    receipt, _ = w.key()
    w.insert('access_sessions', id='pending-session', principal_id='alice', source_kind='key',
        source_key_id=receipt.target.id, source_key_version=1, bootstrap_fingerprint=None,
        token_hash='pending-token-hash', csrf_hash='pending-csrf-hash', principal_security_version=1,
        password_version=None, last_used_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(hours=1), revoked_at=None)
    w.update('access_key_bindings', receipt.target.id, state='pending_review')
    w.call('approve_pending_key', receipt.target, w.version('access_principals', 'alice'), ())
    assert w.row('access_sessions', 'pending-session')['revoked_at'] == NOW
    assert w.row('access_sessions', 'pending-session')['principal_id'] == 'alice'
    assert w.row('access_sessions', 'pending-session')['source_key_version'] == 1


def test_audit_failure_restores_reset_password_keys_sessions_and_all_versions(mworld):
    w = mworld
    w.user('bob')
    receipt, _ = w.key('bob')
    targets = [('access_principals', 'bob'), ('access_users', 'bob'), ('access_sessions', 'session-bob'), ('api_keys', receipt.target.id), ('access_key_bindings', receipt.target.id)]
    before = [w.row(table, identity) for table, identity in targets]
    version = w.policy()
    prepared = w.codec.prepare_temporary_password()
    audit_failure(w)
    with pytest.raises(AccessUnavailableError):
        w.call('reset_password', w.version('access_principals', 'bob'), prepared)
    assert [w.row(table, identity) for table, identity in targets] == before
    assert w.policy() == version


def test_reset_advances_every_target_key_binding_even_previously_revoked(mworld):
    w = mworld
    w.user('bob')
    receipt, _ = w.key('bob')
    revoked = w.call('revoke_key', receipt.target)
    assert revoked.target.version == 2
    w.call('reset_password', w.version('access_principals', 'bob'), w.codec.prepare_temporary_password())
    binding = w.row('access_key_bindings', receipt.target.id)
    assert (binding['state'], binding['version'], binding['security_version']) == ('revoked', 3, 3)


@pytest.mark.parametrize('enabled_role', [True, False])
def test_disabled_owner_group_membership_stays_protected(mworld, enabled_role):
    w = mworld
    actor = w.restricted('custom-full', PERMISSIONS)
    w.user('bob')
    w.group('owners', enabled=0)
    w.assignment(None, 'role_owner', group='owners')
    if not enabled_role:
        w.update('access_roles', 'role_owner', enabled=0)
    denied(w, T.MutationReason.OWNER_REQUIRED, 'add_membership', w.version('access_groups', 'owners'), w.version('access_principals', 'bob'), actor=actor)
    assert not w.rows('access_memberships')


def test_all_connection_first_operations_require_lock_before_any_input_or_write(mworld):
    w = mworld
    names = {
        'create_user', 'update_user', 'create_integration', 'update_integration',
        'enroll_owner', 'reset_password', 'create_group', 'update_group',
        'add_membership', 'remove_membership', 'create_custom_role', 'update_custom_role',
        'create_assignment', 'remove_assignment', 'issue_key', 'update_key_metadata',
        'rotate_key', 'revoke_key', 'approve_pending_key',
    }
    assert {n[:-3] for n in vars(M.AccessMutations) if n.endswith('_on')} == names
    before = w.policy(), len(w.rows('access_audit'))
    with w.engine.begin() as c:
        for name in names:
            method = getattr(w.mutations, name + '_on')
            parameters = inspect.signature(method).parameters
            assert list(parameters)[:2] == ['connection', 'actor']
            assert parameters['expected_policy_version'].default is inspect.Parameter.empty
            assert parameters['now'].default is inspect.Parameter.empty
            args = [object() for p in list(parameters.values())[2:] if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD]
            with pytest.raises(InvalidTransactionError):
                method(c, w.actor, *args, expected_policy_version=1, now=NOW)
    assert (w.policy(), len(w.rows('access_audit'))) == before


def test_large_key_note_is_bounded_and_absent_from_audit_and_receipt(mworld):
    w = mworld
    prepared = w.codec.prepare_new_key()
    note = '😀' * 2000
    values = T.KeyValues(w.version('access_principals', 'alice'), 'n' * 100, note, None, ())
    receipt = w.call('issue_key', values, prepared)
    assert w.row('api_keys', receipt.target.id)['note'] == note
    assert 'note' not in asdict(receipt)
    assert all('😀' not in r['details'] and len(r['details'].encode('utf8')) <= 2048 for r in w.rows('access_audit'))
    denied(w, T.MutationReason.INVALID_INPUT, 'update_key_metadata', receipt.target, T.KeyMetadata(None, 'x' * 2001, None))
    assert w.row('access_key_bindings', receipt.target.id)['version'] == 1


def test_usable_key_actor_attribution_is_resolved_and_stale_key_attribution_is_null(mworld):
    w = mworld
    from api.app.access.proofs import CredentialProofs
    receipt, prepared = w.key(ceiling=tuple(ScopedPermission(p, ResourceRef('installation')) for p in sorted(PERMISSIONS)))
    proofs = CredentialProofs(w.store, w.codec)
    with w.engine.begin() as c:
        snapshot = proofs.key_snapshot_on(c, receipt.public_key_id)
    token = prepared._token_for_committed_adapter()
    proof = proofs.verify_key(snapshot, token[len('fbk_live_' + receipt.public_key_id + '_'):])
    with w.store.transaction() as c:
        actor = proofs.authenticate_key_on(c, proof, now=NOW)
    w.call('create_group', T.GroupValues('Group', '', True), actor=actor)
    audit = next(r for r in w.rows('access_audit') if r['operation'] == 'create_group')
    assert audit['actor_principal_id'] == 'alice' and audit['actor_key_binding_id'] == receipt.target.id
    assert audit['actor_session_id'] is None
    w.call('rotate_key', receipt.target, w.codec.prepare_key_rotation(receipt.public_key_id))
    before_ids = {r['id'] for r in w.rows('access_audit')}
    with pytest.raises(StaleCredentialError):
        w.call('create_group', T.GroupValues('Another', '', True), actor=actor)
    audit = next(r for r in w.rows('access_audit') if r['id'] not in before_ids)
    assert all(audit[f] is None for f in ('actor_principal_id', 'actor_key_binding_id', 'actor_session_id'))
