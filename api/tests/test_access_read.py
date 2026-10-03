"""Management read projections on migrated SQLite and PostgreSQL databases (service level)."""
from datetime import timedelta

import pytest

from api.tests.test_schema import database
from api.tests.test_access_policy import NOW
from api.tests.test_access_sessions import SessionWorld
from api.app.access.catalog import BUILTIN_ROLE_PERMISSIONS, PERMISSIONS
from api.app.access.mutation_types import (AssignmentValues, CustomRoleValues, GroupSubject, GroupValues,
    InboundRuleValues, IntegrationValues, MailboxValues, MutationDeniedError, MutationReason,
    PrincipalSubject, UserValues, VersionedEntity)
from api.app.access.read import AccessReads
from api.app.access.sessions import SessionDeniedError
from api.app.access.types import ResourceRef, ScopedPermission


@pytest.fixture
def rworld(database, tmp_path):
    world = SessionWorld(database, tmp_path)
    world.reads = AccessReads(world.store, world.control, world.sessions, clock=lambda: NOW)
    return world


def reason(call):
    with pytest.raises(MutationDeniedError) as caught:
        call()
    return caught.value.reason


def collect(method, *args, limit, **kwargs):
    """Follow every cursor; each item appears exactly once."""
    items, cursor, pages = [], None, 0
    while True:
        page = method(*args, cursor=cursor, limit=limit, **kwargs)
        assert set(page) == {'items', 'next_cursor'}
        assert len(page['items']) <= limit
        items.extend(page['items'])
        pages += 1
        cursor = page['next_cursor']
        if cursor is None:
            return items, pages
        assert isinstance(cursor, str)


def test_permission_catalogue_is_grouped_for_display(rworld):
    w = rworld
    items = w.reads.permissions(w.actor)['items']
    assert {item['permission'] for item in items} == PERMISSIONS
    assert {item['group'] for item in items} <= {'fax', 'inbound', 'identity', 'config', 'host', 'mailbox', 'audit'}
    by_name = {item['permission']: item for item in items}
    assert by_name['mailboxes:manage']['group'] == 'mailbox'
    assert by_name['host:terminal']['group'] == 'host'
    assert by_name['tunnels:pair']['group'] == 'config'
    assert by_name['users:manage']['group'] == 'identity'
    assert all(item['description'] for item in items)


def test_roles_list_builtin_and_custom_with_permissions_and_versions(rworld):
    w = rworld
    custom = w.call('create_custom_role', CustomRoleValues('Night desk', 'After hours', True, frozenset({'fax:read', 'fax:send'})))
    w.role('legacy-like', {'fax:read'})  # world helper roles are custom kind
    items, pages = collect(w.reads.roles, w.actor, limit=2)
    assert pages > 1 and len({item['id'] for item in items}) == len(items)
    by_id = {item['id']: item for item in items}
    for role_id, permissions in BUILTIN_ROLE_PERMISSIONS.items():
        assert by_id[role_id]['builtin'] is True and by_id[role_id]['permissions'] == sorted(permissions)
    night = by_id[custom.target.id]
    assert night == {'id': custom.target.id, 'name': 'Night desk', 'description': 'After hours', 'builtin': False,
                     'enabled': True, 'permissions': ['fax:read', 'fax:send'], 'version': 1}
    assert w.reads.role(w.actor, custom.target.id) == night
    assert reason(lambda: w.reads.role(w.actor, 'missing')) == MutationReason.INVALID_TARGET
    outsider = w.restricted('outsider', {'users:read'})
    assert reason(lambda: w.reads.roles(outsider)) == MutationReason.FORBIDDEN
    assert reason(lambda: w.reads.roles(w.actor, cursor='not-a-cursor')) == MutationReason.INVALID_INPUT
    for limit in (0, 201, '5'):
        assert reason(lambda: w.reads.roles(w.actor, limit=limit)) == MutationReason.INVALID_INPUT


def test_legacy_per_key_roles_are_not_listed_as_assignable_roles(rworld):
    w = rworld
    w.insert('access_roles', id='legacy-role', name='Legacy integration x', normalized_name='legacy integration x',
             description='', kind='legacy')
    assert 'legacy-role' not in {item['id'] for item in w.reads.roles(w.actor, limit=200)['items']}


def test_users_list_filters_and_principals_read_themselves(rworld):
    w = rworld
    created = w.call('create_user', UserValues('Case.User', 'Front Desk Clerk', True), w.codec.prepare_temporary_password())
    robot = w.call('create_integration', IntegrationValues('Scanner robot', True))
    items, _ = collect(w.reads.users, w.actor, limit=2)
    by_id = {item['id']: item for item in items}
    assert {'bootstrap', 'alice', created.target.id, robot.target.id} <= set(by_id)
    clerk = by_id[created.target.id]
    assert clerk == {'id': created.target.id, 'kind': 'user', 'login': 'Case.User', 'display_name': 'Front Desk Clerk',
                     'enabled': True, 'password_change_required': True, 'created_at': NOW, 'last_login_at': None, 'version': 1}
    assert by_id[robot.target.id]['login'] is None and by_id[robot.target.id]['password_change_required'] is None
    assert by_id['bootstrap']['kind'] == 'bootstrap'
    assert [i['id'] for i in w.reads.users(w.actor, kind='integration', limit=200)['items']] == [robot.target.id]
    assert [i['id'] for i in w.reads.users(w.actor, q='clerk')['items']] == [created.target.id]
    assert [i['id'] for i in w.reads.users(w.actor, q='case.u')['items']] == [created.target.id]
    assert w.reads.users(w.actor, q='100%')['items'] == []
    assert reason(lambda: w.reads.users(w.actor, kind='robots')) == MutationReason.INVALID_INPUT
    clerk_actor = w.restricted('clerk', {'fax:send'})
    assert [i['id'] for i in w.reads.users(clerk_actor)['items']] == ['clerk']
    assert w.reads.user(clerk_actor, 'clerk')['id'] == 'clerk'
    assert reason(lambda: w.reads.user(clerk_actor, 'alice')) == MutationReason.FORBIDDEN
    assert reason(lambda: w.reads.user(w.actor, 'missing')) == MutationReason.INVALID_TARGET


def test_last_login_comes_from_the_latest_password_session(rworld):
    w = rworld
    w.login('alice', now=NOW)
    assert w.reads.user(w.actor, 'alice')['last_login_at'] == NOW


def test_user_detail_lists_memberships_assignments_keys_and_effective_access(rworld):
    w = rworld
    bob = w.user('bob')
    group = w.call('create_group', GroupValues('Front desk', '', True))
    membership = w.call('add_membership', w.version('access_groups', group.target.id), w.version('access_principals', 'bob'))
    w.call('create_assignment', AssignmentValues(GroupSubject(w.version('access_groups', group.target.id)),
        w.version('access_roles', 'role_fax_viewer'), ResourceRef('installation')))
    w.call('create_assignment', AssignmentValues(PrincipalSubject(w.version('access_principals', 'bob')),
        w.version('access_roles', 'role_fax_operator'), ResourceRef('personal-bob')))
    key, _ = w.key('bob', ceiling=(ScopedPermission('fax:send', ResourceRef('personal-bob')),))
    detail = w.reads.user(w.actor, 'bob')
    assert detail['memberships'] == [{'membership_id': membership.target.id, 'group_id': group.target.id,
                                      'group_name': 'Front desk', 'version': 1}]
    [assignment] = detail['assignments']
    assert assignment['subject'] == {'kind': 'principal', 'id': 'bob', 'name': 'bob'}
    assert assignment['role'] == {'id': 'role_fax_operator', 'name': 'Fax Operator', 'builtin': True}
    assert assignment['resource'] == {'id': 'personal-bob', 'kind': 'personal', 'name': 'bob'}
    assert [k['id'] for k in detail['keys']] == [key.public_key_id]
    assert detail['effective'] == {'installation': sorted(BUILTIN_ROLE_PERMISSIONS['role_fax_viewer']),
                                   'personal': sorted(BUILTIN_ROLE_PERMISSIONS['role_fax_operator'])}
    # Bob reads himself, including his own keys, without users:read or keys:manage.
    own = w.reads.user(bob, 'bob')
    assert [k['id'] for k in own['keys']] == [key.public_key_id]
    # A users:read administrator without keys:manage sees no credentials.
    auditor = w.restricted('auditor', {'users:read'})
    assert w.reads.user(auditor, 'bob')['keys'] == []
    w.call('update_user', w.version('access_principals', 'bob'), UserValues('bob', 'bob', False))
    assert w.reads.user(w.actor, 'bob')['effective'] == {'installation': [], 'personal': []}
    assert w.reads.user(w.actor, 'bootstrap')['effective']['installation'] == sorted(PERMISSIONS)


def test_groups_list_and_detail_with_members(rworld):
    w = rworld
    w.user('bob')
    group = w.call('create_group', GroupValues('Billing', 'Billing staff', True))
    w.call('create_group', GroupValues('Archive', '', False))
    w.call('add_membership', w.version('access_groups', group.target.id), w.version('access_principals', 'bob'))
    items, _ = collect(w.reads.groups, w.actor, limit=1)
    assert [item['name'] for item in items] == ['Archive', 'Billing']
    billing = w.reads.group(w.actor, group.target.id)
    assert billing['member_count'] == 1 and billing['version'] == 2 and billing['enabled'] is True
    assert [(m['principal_id'], m['display_name'], m['kind'], m['version']) for m in billing['members']] == [('bob', 'bob', 'user', 1)]
    assert billing['assignments'] == []
    assert reason(lambda: w.reads.group(w.actor, 'missing')) == MutationReason.INVALID_TARGET
    assert reason(lambda: w.reads.groups(w.restricted('nobody', {'users:read'}))) == MutationReason.FORBIDDEN


def test_resources_list_containers_with_display_names_in_stable_order(rworld):
    w = rworld
    mailbox = w.call('create_mailbox', MailboxValues('Front Desk', True))
    items, _ = collect(w.reads.resources, w.actor, limit=2)
    assert [item['kind'] for item in items[:2]] == ['installation', 'legacy']
    assert items[0] == {'id': 'installation', 'kind': 'installation', 'name': 'Whole installation',
                        'parent_id': None, 'mailbox_id': None, 'principal_id': None}
    assert items[1]['name'] == 'Unassigned faxes'
    mailboxes = [item for item in items if item['kind'] == 'mailbox']
    assert [(m['name'], m['mailbox_id']) for m in mailboxes] == [('Front Desk', mailbox.target.id)]
    personal = {item['principal_id']: item for item in items if item['kind'] == 'personal'}
    assert personal['alice']['name'] == 'alice' and personal['alice']['parent_id'] == 'installation'
    assert {item['kind'] for item in w.reads.resources(w.actor, kind='mailbox')['items']} == {'mailbox'}
    assert reason(lambda: w.reads.resources(w.actor, kind='outbound')) == MutationReason.INVALID_INPUT


def test_assignments_filter_by_subject_and_resource_and_allow_self(rworld):
    w = rworld
    bob = w.user('bob')
    first = w.call('create_assignment', AssignmentValues(PrincipalSubject(w.version('access_principals', 'bob')),
        w.version('access_roles', 'role_fax_viewer'), ResourceRef('installation')))
    w.call('create_assignment', AssignmentValues(PrincipalSubject(w.version('access_principals', 'bob')),
        w.version('access_roles', 'role_fax_operator'), ResourceRef('personal-bob')))
    mine = w.reads.assignments(bob, subject_id='bob', limit=200)['items']
    assert {item['role']['id'] for item in mine} == {'role_fax_viewer', 'role_fax_operator'}
    scoped = w.reads.assignments(w.actor, subject_id='bob', resource_id='installation')['items']
    assert [item['id'] for item in scoped] == [first.target.id]
    assert scoped[0]['resource'] == {'id': 'installation', 'kind': 'installation', 'name': 'Whole installation'}
    assert scoped[0]['version'] == 1
    assert reason(lambda: w.reads.assignments(bob)) == MutationReason.FORBIDDEN
    assert reason(lambda: w.reads.assignments(bob, subject_id='alice')) == MutationReason.FORBIDDEN


def test_keys_show_public_id_ceiling_and_resolve_binding_for_mutations(rworld):
    w = rworld
    ceiling = (ScopedPermission('fax:read', ResourceRef('installation')),)
    receipt, _ = w.key('alice', ceiling=ceiling)
    [item] = w.reads.keys(w.actor)['items']
    assert item['id'] == receipt.public_key_id
    assert item['principal'] == {'id': 'alice', 'display_name': 'alice', 'kind': 'user'}
    assert item['ceiling'] == [{'permission': 'fax:read', 'resource_id': 'installation'}]
    assert (item['name'], item['note'], item['pending_review'], item['revoked_at'], item['version']) == (
        'safe name', 'private-note', False, None, 1)
    assert w.reads.key(w.actor, receipt.public_key_id) == item
    assert w.reads.key_binding_id(w.actor, receipt.public_key_id) == receipt.target.id
    w.call('revoke_key', VersionedEntity(receipt.target.id, 1))
    revoked = w.reads.key(w.actor, receipt.public_key_id)
    assert revoked['revoked_at'] == NOW and revoked['version'] == 2
    clerk = w.restricted('clerk', {'fax:send'})
    assert reason(lambda: w.reads.keys(clerk)) == MutationReason.FORBIDDEN
    assert reason(lambda: w.reads.key(clerk, receipt.public_key_id)) == MutationReason.FORBIDDEN
    assert w.reads.keys(clerk, principal_id='clerk')['items'] == []
    assert reason(lambda: w.reads.key(w.actor, 'ffffffffffff')) == MutationReason.INVALID_TARGET


def test_sessions_list_own_and_others_only_with_sessions_read(rworld):
    w = rworld
    _, _, actor = w.login('alice', now=NOW)
    own = w.reads.sessions(actor)
    assert any(item['current'] for item in own['items'])
    current = next(item for item in own['items'] if item['current'])
    assert current['principal'] == {'id': 'alice', 'display_name': 'alice'}
    assert current['source_kind'] == 'password' and current['revoked_at'] is None
    w.user('bob')
    others = w.reads.sessions(actor, principal_id='bob')['items']
    assert [item['session_id'] for item in others] == ['session-bob']
    assert others[0]['principal'] == {'id': 'bob', 'display_name': 'bob'} and others[0]['current'] is False
    clerk = w.restricted('clerk', {'fax:send'})
    with pytest.raises(SessionDeniedError):
        w.reads.sessions(clerk, principal_id='bob')


def test_session_pages_continue_with_an_opaque_cursor(rworld):
    w = rworld
    for minute in range(3):
        w.login('alice', now=NOW - timedelta(minutes=10 - minute))
    _, _, actor = w.login('alice', now=NOW - timedelta(minutes=1))
    items, pages = collect(w.reads.sessions, actor, limit=2)
    assert pages >= 2 and len({item['session_id'] for item in items}) == len(items) >= 4


def test_mailboxes_and_rules_report_ids_versions_and_bindings(rworld):
    w = rworld
    front = w.call('create_mailbox', MailboxValues('Front Desk', True)).target
    w.call('create_mailbox', MailboxValues('Night', False))
    rule = w.call('create_inbound_rule', InboundRuleValues('+15551230001', front.id)).target
    w.insert('inbound_rules', id='legacy-rule', to_number='+15550000000', mailbox_label='Gone')
    renamed = w.call('update_mailbox', front, MailboxValues('Reception', True)).target
    items, _ = collect(w.reads.mailboxes, w.actor, limit=1)
    by_label = {item['label']: item for item in items}
    reception = by_label['Reception']
    assert reception['id'] == front.id and reception['version'] == renamed.version == 2
    assert reception['enabled'] is True and reception['rule_count'] == 1 and reception['resource_id']
    assert by_label['Night']['enabled'] is False and by_label['Night']['rule_count'] == 0
    assert w.reads.mailbox(w.actor, front.id) == reception
    rules = {item['id']: item for item in w.reads.inbound_rules(w.actor, limit=200)['items']}
    assert rules[rule.id] == {'id': rule.id, 'to_number': '+15551230001', 'mailbox_id': front.id,
                              'mailbox_label': 'Reception', 'version': 1}
    assert rules['legacy-rule'] == {'id': 'legacy-rule', 'to_number': '+15550000000', 'mailbox_id': None,
                                    'mailbox_label': 'Gone', 'version': 0}
    assert w.reads.inbound_rule(w.actor, rule.id) == rules[rule.id]
    reader = w.restricted('reader', {'mailboxes:read'})
    assert len(w.reads.mailboxes(reader)['items']) == 2
    assert reason(lambda: w.reads.mailboxes(w.restricted('clerk', {'fax:send'}))) == MutationReason.FORBIDDEN
    assert reason(lambda: w.reads.mailbox(w.actor, 'missing')) == MutationReason.INVALID_TARGET


def test_audit_page_is_newest_first_with_names_and_filters(rworld):
    w = rworld
    mailbox = w.call('create_mailbox', MailboxValues('Front Desk', True), now=NOW + timedelta(seconds=1)).target
    w.call('create_group', GroupValues('Billing', '', True), now=NOW + timedelta(seconds=2))
    outsider = w.restricted('outsider', {'users:read'})
    with pytest.raises(MutationDeniedError):
        w.call('create_group', GroupValues('Nope', '', True), actor=outsider)
    page = w.reads.audit(w.actor, limit=200)
    operations = [item['operation'] for item in page['items']]
    assert operations.index('create_group') < operations.index('create_mailbox')
    created = next(item for item in page['items'] if item['operation'] == 'create_mailbox')
    assert created['actor'] == {'id': 'alice', 'display_name': 'alice'}
    assert created['credential_kind'] == 'session' and created['outcome'] == 'allowed'
    assert created['target'] == {'kind': 'mailbox', 'id': mailbox.id, 'name': 'Front Desk'}
    assert created['details'] == {'changed': True}
    denied = w.reads.audit(w.actor, actor_id='outsider')['items']
    assert [(item['operation'], item['outcome']) for item in denied] == [('create_group', 'denied')]
    assert all(item['operation'] == 'create_mailbox' for item in w.reads.audit(w.actor, operation='create_mailbox')['items'])
    assert [item['id'] for item in w.reads.audit(w.actor, target_id=mailbox.id)['items']] == [created['id']]
    migrated = [item for item in page['items'] if item['operation'] == 'access_migration']
    assert migrated and all(item['outcome'] == 'allowed' and item['actor'] is None
                            and item['credential_kind'] == 'system' for item in migrated)
    items, pages = collect(w.reads.audit, w.actor, limit=2)
    assert pages > 1 and [item['id'] for item in items] == [item['id'] for item in page['items']]
    assert reason(lambda: w.reads.audit(outsider)) == MutationReason.FORBIDDEN


def test_me_extras_report_owner_status_and_grantable_permissions(rworld):
    w = rworld
    assert w.reads.me_extras(w.actor) == {'is_owner': True, 'grantable': {'installation': sorted(PERMISSIONS)}}
    manager = w.restricted('manager', {'users:read', 'users:manage'})
    assert w.reads.me_extras(manager) == {'is_owner': False, 'grantable': {'installation': ['users:manage', 'users:read']}}
