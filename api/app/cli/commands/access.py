"""Who can use Faxbot and what they may do: users, groups, roles, access, keys, sessions,
mailboxes, fax number routing and the security audit.

Every change sends the access policy version read from /auth/me immediately
before the request, plus the current version of the record being changed.
"""
from datetime import datetime, timezone

import typer

from . import rules
from .. import resolve, state
from ..client import segment
from ..errors import CliError
from ..output import local_time, text

users = typer.Typer(help='People who sign in to Faxbot, and the apps and devices that use their own key.', no_args_is_help=True)
integrations = typer.Typer(help='Apps and devices, such as scanners, that use Faxbot with their own key.',
                           no_args_is_help=True)
groups = typer.Typer(help='Groups of people who share the same access.', no_args_is_help=True)
members = typer.Typer(help='Add people to a group or remove them.', no_args_is_help=True)
roles = typer.Typer(help='Roles: named sets of permissions, such as Fax operator, that you give to people.', no_args_is_help=True)
access = typer.Typer(help='Who holds which role, and where.', no_args_is_help=True)
resources = typer.Typer(help="The places a role can apply to: the whole installation, a mailbox or a person's own faxes.",
                        no_args_is_help=True)
keys = typer.Typer(help='Keys that let apps, scripts, scanners and phones use Faxbot without a password.', no_args_is_help=True)
sessions = typer.Typer(help='People signed in to the console in a browser.', no_args_is_help=True)
mailboxes = typer.Typer(help='Mailboxes that hold received faxes.', no_args_is_help=True)
numbers = typer.Typer(help='Which mailbox receives faxes sent to each of your fax numbers.', no_args_is_help=True)
audit = typer.Typer(help='The security audit: access changes and sign-ins.', no_args_is_help=True)
owner = typer.Typer(help='Create an owner, who can do everything in Faxbot, using the installation key.', no_args_is_help=True)

IDS = typer.Option(False, '--ids', help='Also show internal ids.')
KIND_TEXT = {'user': 'user', 'integration': 'integration', 'bootstrap': 'installation key'}
SOURCE_TEXT = {'password': 'password', 'key': 'API key', 'bootstrap': 'installation key'}


groups.add_typer(members, name='members')


def _with_id(ids, item_id, row):
    return ([item_id] if ids else []) + row


def _columns(ids, columns):
    return (['ID'] if ids else []) + columns


def _enabled(enable, disable):
    if enable and disable:
        raise CliError('Choose --enable or --disable, not both.')
    return True if enable else False if disable else None


def _require_change(body):
    if not body:
        raise CliError('Nothing to change. Add at least one option; see --help.')


def _show_secret(label, value, after=None):
    out = state.out()
    out.secret(label, value)
    if after:
        out.line(after)


# -- me and owner enrollment ------------------------------------------------------

def me():
    """Show who your key belongs to and what it may do."""
    who = state.api().get('/auth/me')

    def human(out):
        principal = who['principal']
        out.fields([('Name', principal['display_name']), ('Kind', KIND_TEXT.get(principal['kind'], principal['kind'])),
                    ('Owner', who.get('is_owner')), ('Must change password', who.get('password_change_required')),
                    ('Can create the first owner', who.get('can_enroll_owner')),
                    ('Server', state.current().url), ('Profile', state.current().profile)])
        out.line('Permissions: ' + (', '.join(who['permissions']) or 'none'))
    state.out().result(who, human)


@owner.command('enroll')
def owner_enroll(login: str = typer.Option(..., '--login', help='Sign-in name for the new owner.'),
                 name: str = typer.Option(..., '--name', help="The owner's display name.")):
    """Create a named owner, with full control, and a temporary password shown once.

    Use the installation key or an existing owner's API key. This works for the first owner, and to regain access after faxbot admin recover-owner.
    """
    api = state.api()
    result = api.post('/auth/owner/enroll', json=api.with_policy({'login': login, 'display_name': name}))
    out = state.out()
    out.result(result, lambda o: o.line(f"Owner {result['user']['display_name']} created with sign-in name "
                                        f"{result['user']['login']}."))
    _show_secret('Temporary password (shown only once)', result['temporary_password'],
                 f'Next: sign in to the console at {api.url} with this name and password. Faxbot will ask '
                 'you to choose a new password.')


# -- users and integrations -------------------------------------------------------

def _user_rows(items, ids):
    return [_with_id(ids, item['id'], [item['display_name'], item.get('login'), KIND_TEXT.get(item['kind'], item['kind']),
                                       item['enabled'], local_time(item.get('last_login_at'), empty='never')])
            for item in items]


@users.command('list')
def users_list(kind: str = typer.Option('all', '--kind', help='user, integration or all.'),
               search: str = typer.Option(None, '--search', help='Only users whose name or sign-in name contains this text.'),
               limit: int = typer.Option(None, '--limit', min=1, help='Show at most this many.'),
               ids: bool = IDS):
    """List people and apps."""
    items = state.api().pages('/access/users', params={'kind': kind, 'q': search}, limit=limit)
    state.out().result(items, lambda out: out.table(
        _columns(ids, ['Name', 'Sign-in name', 'Kind', 'Enabled', 'Last sign-in']), _user_rows(items, ids),
        empty='No users.'))


@users.command('get')
def users_get(who: str = typer.Argument(..., help='Sign-in name, display name or id.'), ids: bool = IDS):
    """Show a person or app with their groups, roles, keys and what they may do."""
    user = resolve.user(state.api(), who)

    def human(out):
        out.fields(([('ID', user['id'])] if ids else []) + [
            ('Name', user['display_name']), ('Sign-in name', user.get('login')),
            ('Kind', KIND_TEXT.get(user['kind'], user['kind'])), ('Enabled', user['enabled']),
            ('Must change password', user.get('password_change_required')),
            ('Created', local_time(user.get('created_at'))), ('Last sign-in', local_time(user.get('last_login_at'), empty='never')),
            ('Groups', [m['group_name'] for m in user.get('memberships', [])]),
            ('Roles', [f"{a['role']['name']} on {a['resource']['name']}" for a in user.get('assignments', [])]),
            ('API keys', [k['id'] for k in user.get('keys', [])]),
            ('Permissions (whole installation)', user.get('effective', {}).get('installation')),
            ('Permissions (own faxes)', user.get('effective', {}).get('personal'))])
    state.out().result(user, human)


@users.command('add')
def users_add(login: str = typer.Argument(..., help='Sign-in name.'),
              name: str = typer.Option(..., '--name', help='Display name.'),
              disabled: bool = typer.Option(False, '--disabled', help='Add the person without letting them sign in yet.')):
    """Add a user. Their temporary password is shown once."""
    api = state.api()
    result = api.post('/access/users', json=api.with_policy({'login': login, 'display_name': name,
                                                              'enabled': not disabled}))
    state.out().result(result, lambda out: out.line(f"User {result['user']['display_name']} added."))
    _show_secret('Temporary password (shown only once)', result['temporary_password'],
                 'They choose a new password the first time they sign in.')


@users.command('update')
def users_update(who: str = typer.Argument(..., help='Sign-in name, display name or id.'),
                 name: str = typer.Option(None, '--name', help='New display name.'),
                 login: str = typer.Option(None, '--login', help='New sign-in name (users only).'),
                 enable: bool = typer.Option(False, '--enable', help='Switch on.'),
                 disable: bool = typer.Option(False, '--disable', help='Disable the user. This ends their sessions and stops their keys.')):
    """Change a person's or app's name, sign-in name, or whether they can use Faxbot."""
    api = state.api()
    body = {key: value for key, value in (('display_name', name), ('login', login),
                                          ('enabled', _enabled(enable, disable))) if value is not None}
    _require_change(body)
    user = resolve.user(api, who)
    result = api.patch('/access/users/' + segment(user['id']), json=api.with_policy({**body, 'version': user['version']}))
    state.out().result(result, lambda out: out.line(f"{result['user'].get('display_name') or who} updated."))


@users.command('reset-password')
def users_reset_password(who: str = typer.Argument(..., help='Sign-in name, display name or id.')):
    """Give a user a new temporary password, shown once. Their sessions end."""
    api = state.api()
    user = resolve.user(api, who)
    result = api.post(f"/access/users/{segment(user['id'])}/reset-password",
                      json=api.with_policy({'version': user['version']}))
    state.out().result(result, lambda out: out.line(f"Password reset for {user['display_name']}."))
    _show_secret('Temporary password (shown only once)', result['temporary_password'],
                 'They choose a new password the next time they sign in.')


@integrations.command('add')
def integrations_add(name: str = typer.Argument(..., help='Name, for example "Front desk scanner".'),
                     disabled: bool = typer.Option(False, '--disabled', help='Create the group disabled, so its roles do not apply yet.')):
    """Add an app or device, such as a scanner, that uses Faxbot with its own key. Next give it a role, then create its key."""
    api = state.api()
    result = api.post('/access/integrations', json=api.with_policy({'display_name': name, 'enabled': not disabled}))
    state.out().result(result, lambda out: out.line(f"Integration {result['integration'].get('display_name') or name} "
                                                    'added.'))


@integrations.command('list')
def integrations_list(ids: bool = IDS):
    """List the apps and devices, such as scanners, that use Faxbot with their own key."""
    items = state.api().pages('/access/users', params={'kind': 'integration'})
    state.out().result(items, lambda out: out.table(
        _columns(ids, ['Name', 'Sign-in name', 'Kind', 'Enabled', 'Last sign-in']), _user_rows(items, ids),
        empty='No integrations.'))


# -- groups ---------------------------------------------------------------------------

@groups.command('list')
def groups_list(ids: bool = IDS):
    """List groups."""
    items = state.api().pages('/access/groups')
    state.out().result(items, lambda out: out.table(
        _columns(ids, ['Name', 'Description', 'Enabled', 'Members']),
        [_with_id(ids, item['id'], [item['name'], item['description'], item['enabled'], item['member_count']])
         for item in items], empty='No groups.'))


@groups.command('get')
def groups_get(name: str = typer.Argument(..., help='Group name or id.'), ids: bool = IDS):
    """Show a group with its members and roles."""
    found = resolve.group(state.api(), name)

    def human(out):
        out.fields(([('ID', found['id'])] if ids else []) + [
            ('Name', found['name']), ('Description', found['description']), ('Enabled', found['enabled']),
            ('Members', [m['display_name'] for m in found.get('members', [])]),
            ('Roles', [f"{a['role']['name']} on {a['resource']['name']}" for a in found.get('assignments', [])])])
    state.out().result(found, human)


@groups.command('add')
def groups_add(name: str = typer.Argument(..., help='Group name.'),
               description: str = typer.Option('', '--description', help='What the group is for.'),
               disabled: bool = typer.Option(False, '--disabled', help='Create the group disabled, so its roles do not apply yet.')):
    """Add a group."""
    api = state.api()
    result = api.post('/access/groups', json=api.with_policy({'name': name, 'description': description,
                                                               'enabled': not disabled}))
    state.out().result(result, lambda out: out.line(f'Group {name} added.'))


@groups.command('update')
def groups_update(name: str = typer.Argument(..., help='Group name or id.'),
                  new_name: str = typer.Option(None, '--name', help='New name.'),
                  description: str = typer.Option(None, '--description', help='New description.'),
                  enable: bool = typer.Option(False, '--enable', help='Switch on.'),
                  disable: bool = typer.Option(False, '--disable', help='Disable the group; members lose its roles.')):
    """Rename a group, change its description or switch it on or off."""
    api = state.api()
    body = {key: value for key, value in (('name', new_name), ('description', description),
                                          ('enabled', _enabled(enable, disable))) if value is not None}
    _require_change(body)
    found = resolve.group(api, name)
    result = api.patch('/access/groups/' + segment(found['id']), json=api.with_policy({**body, 'version': found['version']}))
    state.out().result(result, lambda out: out.line(f"Group {result['group'].get('name') or name} updated."))


@members.command('add')
def members_add(group: str = typer.Argument(..., help='Group name or id.'),
                who: str = typer.Argument(..., help='Sign-in name, display name or id of the user or integration.')):
    """Add a person or app to a group."""
    api = state.api()
    found, person = resolve.group(api, group), resolve.user(api, who)
    result = api.post(f"/access/groups/{segment(found['id'])}/members", json=api.with_policy({
        'principal_id': person['id'], 'principal_version': person['version'], 'group_version': found['version']}))
    state.out().result(result, lambda out: out.line(f"{person['display_name']} added to {found['name']}."))


@members.command('remove')
def members_remove(group: str = typer.Argument(..., help='Group name or id.'),
                   who: str = typer.Argument(..., help='Sign-in name, display name or id.')):
    """Remove a user or integration from a group."""
    api = state.api()
    found, person = resolve.group(api, group), resolve.user(api, who)
    membership = next((m for m in found.get('members', []) if m['principal_id'] == person['id']), None)
    if membership is None:
        raise CliError(f"{person['display_name']} is not in {found['name']}.")
    result = api.post(f"/access/groups/{segment(found['id'])}/members/{segment(membership['membership_id'])}/remove",
                      json=api.with_policy({'membership_version': membership['version'],
                                            'group_version': found['version']}))
    state.out().result(result, lambda out: out.line(f"{person['display_name']} removed from {found['name']}."))


# -- roles ------------------------------------------------------------------------------

@roles.command('list')
def roles_list(ids: bool = IDS):
    """List roles."""
    items = state.api().pages('/access/roles')
    state.out().result(items, lambda out: out.table(
        _columns(ids, ['Name', 'Built in', 'Enabled', 'Permissions', 'Description']),
        [_with_id(ids, item['id'], [item['name'], item['builtin'], item['enabled'], len(item['permissions']),
                                    item.get('description')]) for item in items], empty='No roles.'))


@roles.command('get')
def roles_get(name: str = typer.Argument(..., help='Role name or id.'), ids: bool = IDS):
    """Show a role and its permissions."""
    found = resolve.role(state.api(), name)
    state.out().result(found, lambda out: out.fields(([('ID', found['id'])] if ids else []) + [
        ('Name', found['name']), ('Description', found.get('description')), ('Built in', found['builtin']),
        ('Enabled', found['enabled']), ('Permissions', found['permissions'])]))


@roles.command('permissions')
def roles_permissions():
    """List everything a role can allow."""
    items = state.api().get('/access/permissions')['items']
    state.out().result(items, lambda out: out.table(['Permission', 'Area', 'What it allows'],
        [[item['permission'], item['group'], item['description']] for item in items]))


PERMISSION = typer.Option(None, '--permission', '-p', help='A permission, for example fax:send. Repeat for more.')


@roles.command('add')
def roles_add(name: str = typer.Argument(..., help='Role name.'),
              permission: list[str] = PERMISSION,
              description: str = typer.Option('', '--description', help='What the role is for.'),
              disabled: bool = typer.Option(False, '--disabled', help='Create the group disabled, so its roles do not apply yet.')):
    """Add a role of your own, choosing what it allows. See faxbot admin access roles permissions for the choices."""
    if not permission:
        raise CliError('Add at least one --permission. See faxbot admin access roles permissions.')
    api = state.api()
    result = api.post('/access/roles', json=api.with_policy({'name': name, 'description': description,
        'permissions': sorted(set(permission)), 'enabled': not disabled}))
    state.out().result(result, lambda out: out.line(f'Role {name} added.'))


@roles.command('update')
def roles_update(name: str = typer.Argument(..., help='Role name or id.'),
                 new_name: str = typer.Option(None, '--name', help='New name.'),
                 description: str = typer.Option(None, '--description', help='New description.'),
                 permission: list[str] = typer.Option(None, '--permission', '-p',
                                                      help='Replace all permissions with these. Repeat for more.'),
                 add: list[str] = typer.Option(None, '--add', help='Add this permission. Repeat for more.'),
                 remove: list[str] = typer.Option(None, '--remove', help='Remove this permission. Repeat for more.'),
                 enable: bool = typer.Option(False, '--enable', help='Switch on.'),
                 disable: bool = typer.Option(False, '--disable', help='Disable the role everywhere it is assigned.')):
    """Change a custom role. Built-in roles cannot be changed."""
    api = state.api()
    found = resolve.role(api, name)
    body = {key: value for key, value in (('name', new_name), ('description', description),
                                          ('enabled', _enabled(enable, disable))) if value is not None}
    if permission or add or remove:
        chosen = set(permission) if permission else set(found['permissions'])
        body['permissions'] = sorted((chosen | set(add or [])) - set(remove or []))
    _require_change(body)
    result = api.patch('/access/roles/' + segment(found['id']), json=api.with_policy({**body, 'version': found['version']}))
    state.out().result(result, lambda out: out.line(f"Role {result['role'].get('name') or name} updated."))


# -- access (role assignments) and resources ---------------------------------------------

def _assignment_row(item, ids):
    subject = item['subject']
    kind = 'group' if subject['kind'] == 'group' else 'person'
    return _with_id(ids, item['id'], [subject.get('name'), kind, item['role']['name'], item['resource']['name']])


@access.command('list')
def access_list(who: str = typer.Option(None, '--who', help='Only grants for this user, integration or group.'),
                where: str = typer.Option(None, '--on', help='Only grants on this resource, such as installation or mailbox:NAME.'),
                ids: bool = IDS):
    """List who has which role, and where."""
    api = state.api()
    params = {}
    if who:
        params['subject_id'] = resolve.subject(api, who)[0]['id']
    if where:
        params['resource_id'] = resolve.resource(api, where)['id']
    items = api.pages('/access/assignments', params=params)
    state.out().result(items, lambda out: out.table(_columns(ids, ['Who', 'Kind', 'Role', 'Where']),
                                                    [_assignment_row(item, ids) for item in items],
                                                    empty='No role assignments.'))


WHERE = typer.Option('installation', '--on', help='Where the role applies: installation (the default), mailbox:NAME, '
                                                   'personal:USER, unassigned, or a resource from faxbot admin access resources list.')


@access.command('grant')
def access_grant(who: str = typer.Argument(..., help='User, integration or group. Prefix with group: or user: if '
                                                     'names overlap.'),
                 role: str = typer.Argument(..., help='Role name, for example "Fax operator".'),
                 where: str = WHERE):
    """Give a person, app or group a role."""
    api = state.api()
    subject, subject_name = resolve.subject(api, who)
    found_role, place = resolve.role(api, role), resolve.resource(api, where)
    result = api.post('/access/assignments', json=api.with_policy({
        'subject': subject, 'role': {'id': found_role['id'], 'version': found_role['version']},
        'resource_id': place['id']}))
    state.out().result(result, lambda out: out.line(f"{subject_name} now has {found_role['name']} on {place['name']}."))


@access.command('revoke')
def access_revoke(who: str = typer.Argument(..., help='User, integration or group.'),
                  role: str = typer.Argument(..., help='Role name.'),
                  where: str = WHERE):
    """Take a role away from a user, integration or group."""
    api = state.api()
    subject, subject_name = resolve.subject(api, who)
    found_role, place = resolve.role(api, role), resolve.resource(api, where)
    items = api.pages('/access/assignments', params={'subject_id': subject['id'], 'resource_id': place['id']})
    match = next((item for item in items if item['role']['id'] == found_role['id']
                  and item['subject']['id'] == subject['id']), None)
    if match is None:
        raise CliError(f"{subject_name} does not have {found_role['name']} on {place['name']}.")
    result = api.post(f"/access/assignments/{segment(match['id'])}/remove",
                      json=api.with_policy({'version': match['version']}))
    state.out().result(result, lambda out: out.line(f"{found_role['name']} on {place['name']} removed from "
                                                    f'{subject_name}.'))


KIND_PLACE = {'installation': 'whole installation', 'legacy': 'unassigned faxes', 'mailbox': 'mailbox',
              'personal': 'personal faxes'}


@resources.command('list')
def resources_list(kind: str = typer.Option(None, '--kind', help='Resource type: installation, legacy, mailbox or personal.'),
                   ids: bool = IDS):
    """List the places a role can apply to."""
    items = state.api().pages('/access/resources', params={'kind': kind})
    state.out().result(items, lambda out: out.table(_columns(ids, ['Name', 'Kind']),
        [_with_id(ids, item['id'], [item['name'], KIND_PLACE.get(item['kind'], item['kind'])]) for item in items]))


# -- keys --------------------------------------------------------------------------------------

def _expiry(value):
    """An expiry date or time; local time unless it names a time zone."""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise CliError('Give the expiry as a date like 2027-01-31 or a date and time like 2027-01-31T17:00.') from None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc).isoformat()


def _ceiling(api, permissions, where):
    place = resolve.resource(api, where)
    return [{'permission': permission, 'resource_id': place['id']} for permission in sorted(set(permissions))]


def _key_rows(items):
    return [[item['id'], item.get('name'), item['principal'].get('display_name'),
             'needs review' if item.get('pending_review') else 'revoked' if item.get('revoked_at') else 'active',
             local_time(item.get('expires_at'), empty='never'), local_time(item.get('last_used_at'), empty='never')]
            for item in items]


@keys.command('list')
def keys_list(who: str = typer.Option(None, '--for', help='Only keys of this user or integration.')):
    """List keys and who they belong to. A key itself is shown only once, when it is created."""
    api = state.api()
    params = {'principal_id': resolve.user(api, who)['id']} if who else {}
    items = api.pages('/access/keys', params=params)
    state.out().result(items, lambda out: out.table(['Key ID', 'Name', 'Belongs to', 'State', 'Expires', 'Last used'],
                                                    _key_rows(items), empty='No API keys.'))


@keys.command('create')
def keys_create(permission: list[str] = typer.Option(..., '--permission', '-p',
                                                     help='What the key may do, for example fax:send. Repeat for more.'),
                who: str = typer.Option(None, '--for', help='User or integration the key belongs to. Default: you.'),
                where: str = WHERE,
                name: str = typer.Option(None, '--name', help='A name to recognise the key by.'),
                note: str = typer.Option(None, '--note', help='A note about where the key is used.'),
                expires: str = typer.Option(None, '--expires', help='Expiry date, for example 2027-01-31.')):
    """Create a key. It is shown once, so store it straight away."""
    api = state.api()
    if who:
        principal = resolve.user(api, who)
    else:
        mine = api.get('/auth/me')['principal']
        principal = {'id': mine['id'], 'version': mine['version']}
    body = {'principal': {'id': principal['id'], 'version': principal['version']}, 'name': name, 'note': note,
            'expires_at': _expiry(expires) if expires else None, 'ceiling': _ceiling(api, permission, where)}
    result = api.post('/access/keys', json=api.with_policy(body))
    state.out().result(result, lambda out: out.line(f"API key {result['key']['id']} created."))
    _show_secret('API key (shown only once)', result['token'])


@keys.command('rotate')
def keys_rotate(key_id: str = typer.Argument(..., help='Key ID or key name.')):
    """Replace a key with a new one. The old one stops working at once; the new one is shown once."""
    api = state.api()
    found = resolve.key(api, key_id)
    result = api.post(f"/access/keys/{segment(found['id'])}/rotate", json=api.with_policy({'version': found['version']}))
    state.out().result(result, lambda out: out.line(f"API key {found['id']} replaced."))
    _show_secret('New API key (shown only once)', result['token'])


@keys.command('revoke')
def keys_revoke(key_id: str = typer.Argument(..., help='Key ID or key name.')):
    """Revoke a key permanently."""
    api = state.api()
    found = resolve.key(api, key_id)
    result = api.post(f"/access/keys/{segment(found['id'])}/revoke", json=api.with_policy({'version': found['version']}))
    state.out().result(result, lambda out: out.line(f"API key {found['id']} revoked."))


@keys.command('approve')
def keys_approve(key_id: str = typer.Argument(..., help='Key ID of a key that needs review.'),
                 who: str = typer.Option(..., '--for', help='User or integration the key will belong to.'),
                 permission: list[str] = typer.Option(..., '--permission', '-p',
                                                      help='A permission for the key, for example fax:send. Repeat for more.'),
                 where: str = WHERE):
    """Approve an older key that is waiting for review: choose whose it is and what it may do."""
    api = state.api()
    found, principal = resolve.key(api, key_id), resolve.user(api, who)
    result = api.post(f"/access/keys/{segment(found['id'])}/approve", json=api.with_policy({
        'principal': {'id': principal['id'], 'version': principal['version']},
        'ceiling': _ceiling(api, permission, where), 'version': found['version']}))
    # People know a key by its name; the key ID stays in --json output.
    sentence = f"API key {found['name']} approved." if found.get('name') else 'API key approved.'
    state.out().result(result, lambda out: out.line(sentence))


@keys.command('update')
def keys_update(key_id: str = typer.Argument(..., help='Key ID or key name.'),
                name: str = typer.Option(None, '--name', help='New name.'),
                note: str = typer.Option(None, '--note', help='New note.'),
                expires: str = typer.Option(None, '--expires', help='New expiry date.'),
                no_expiry: bool = typer.Option(False, '--no-expiry', help='Remove the expiry date.')):
    """Change a key's name, note or expiry date."""
    api = state.api()
    body = {key: value for key, value in (('name', name), ('note', note)) if value is not None}
    if expires and no_expiry:
        raise CliError('Choose --expires or --no-expiry, not both.')
    if expires:
        body['expires_at'] = _expiry(expires)
    if no_expiry:
        body['expires_at'] = None
    _require_change(body)
    found = resolve.key(api, key_id)
    result = api.patch('/access/keys/' + segment(found['id']), json=api.with_policy({**body, 'version': found['version']}))
    state.out().result(result, lambda out: out.line(f"API key {found['id']} updated."))


# -- sessions ---------------------------------------------------------------------------------

@sessions.command('list')
def sessions_list(who: str = typer.Option(None, '--for', help="Another user's sessions. Default: yours."),
                  ids: bool = IDS):
    """List people signed in to the console."""
    api = state.api()
    params = {'principal_id': resolve.user(api, who)['id']} if who else {}
    items = api.pages('/access/sessions', params=params)
    state.out().result(items, lambda out: out.table(
        _columns(ids, ['Who', 'Signed in with', 'Started', 'Last used', 'Ends', 'State']),
        [_with_id(ids, item['session_id'], [item['principal'].get('display_name'),
                                            SOURCE_TEXT.get(item['source_kind'], item['source_kind']),
                                            local_time(item['created_at']), local_time(item['last_used_at']),
                                            local_time(item['expires_at']),
                                            'ended' if item.get('revoked_at') else 'active'])
         for item in items], empty='No sessions.'))


@sessions.command('revoke')
def sessions_revoke(session_id: str = typer.Argument(..., help="Session id from 'faxbot admin access sessions list --ids'.")):
    """End a session."""
    api = state.api()
    result = api.post(f'/access/sessions/{segment(session_id)}/revoke', json=api.with_policy({}))
    state.out().result(result, lambda out: out.line('Session ended.' if result.get('changed') else
                                                    'That session had already ended.'))


# -- mailboxes and fax number rules -----------------------------------------------------------

@mailboxes.command('list')
def mailboxes_list(ids: bool = IDS):
    """List mailboxes."""
    items = state.api().pages('/access/mailboxes')
    state.out().result(items, lambda out: out.table(_columns(ids, ['Mailbox', 'Enabled', 'Fax numbers']),
        [_with_id(ids, item['id'], [item['label'], item['enabled'], item['rule_count']]) for item in items],
        empty='No mailboxes.'))


@mailboxes.command('add')
def mailboxes_add(label: str = typer.Argument(..., help='Mailbox name, for example "Front desk".'),
                  disabled: bool = typer.Option(False, '--disabled', help='Create the group disabled, so its roles do not apply yet.')):
    """Add a mailbox."""
    api = state.api()
    result = api.post('/access/mailboxes', json=api.with_policy({'label': label, 'enabled': not disabled}))
    state.out().result(result, lambda out: out.line(f'Mailbox {label} added.'))


@mailboxes.command('update')
def mailboxes_update(mailbox: str = typer.Argument(..., help='Mailbox name or id.'),
                     label: str = typer.Option(None, '--name', help='New name.'),
                     enable: bool = typer.Option(False, '--enable', help='Switch on.'),
                     disable: bool = typer.Option(False, '--disable', help='Switch off.')):
    """Rename a mailbox or switch it on or off."""
    api = state.api()
    body = {key: value for key, value in (('label', label), ('enabled', _enabled(enable, disable))) if value is not None}
    _require_change(body)
    found = resolve.mailbox(api, mailbox)
    result = api.patch('/access/mailboxes/' + segment(found['id']), json=api.with_policy({**body, 'version': found['version']}))
    state.out().result(result, lambda out: out.line(f"Mailbox {result['mailbox'].get('label') or mailbox} updated."))


# The providers whose numbers Your numbers lists, as the console names them.
CARRIERS = {'sip': 'Carrier trunk', 'humblefax': 'HumbleFax', 'efax': 'eFax', 'signalwire': 'SignalWire',
            'freeswitch': 'FreeSWITCH'}
NO_MAILBOX = 'No mailbox: received faxes are visible to people with access to everything.'


def comparable_number(value):
    """A fax number in one form for comparing, as the console does: +1 for ten US/Canadian digits."""
    text = (value or '').strip()
    digits = ''.join(character for character in text if character.isdigit())
    if not digits:
        return ''
    if text.startswith('+') or (len(digits) == 11 and digits.startswith('1')):
        return '+' + digits
    return '+1' + digits if len(digits) == 10 else digits


def carried_numbers(settings):
    """The numbers each provider carries, from the settings document: [(number, provider, in use)]."""
    hybrid = settings.get('hybrid') or {}
    backend = (settings.get('backend') or {}).get('type') or ''
    sending = hybrid.get('outbound_backend') or backend
    receiving = (hybrid.get('inbound_backend') or backend) if (settings.get('inbound') or {}).get('enabled') else ''
    extra = [route.strip() for route in ((settings.get('routing') or {}).get('outbound_routes') or '').split(',')]
    humblefax = settings.get('humblefax') or {}
    found = [*[('sip', number) for number in ((settings.get('sip') or {}).get('trunk') or {}).get('dids') or []],
             ('humblefax', humblefax.get('from_number')),
             *[('humblefax', number) for number in humblefax.get('account_numbers') or []],
             ('efax', (settings.get('efax') or {}).get('caller_id')),
             ('signalwire', (settings.get('signalwire') or {}).get('from_fax')),
             ('freeswitch', (settings.get('fs') or {}).get('caller_id_number'))]
    result = []
    for provider, number in found:
        number = comparable_number(number)
        if number and (number, provider) not in [(item[0], item[1]) for item in result]:
            result.append((number, provider, provider in {sending, receiving} or provider in extra))
    return result


def _email_text(connectors, number):
    if connectors is None:
        return '-'
    covering = [item for item in connectors if item.get('enabled') and (
        item.get('match_number') is None or comparable_number(item['match_number']) == comparable_number(number))]
    if not covering:
        return 'Not emailed'
    recipients = list(dict.fromkeys(address for item in covering for address in item.get('recipients') or []))
    return 'Emailed to ' + ', '.join(recipients) if recipients else 'Emailed'


@numbers.command('list')
def numbers_list(ids: bool = IDS):
    """List your fax numbers: who provides each one, the mailbox its faxes go to, and whether they are emailed."""
    from ..errors import CliError
    api = state.api()
    rules = api.pages('/access/inbound-rules')
    try:
        carried = carried_numbers(api.get('/admin/settings'))
    except CliError:
        carried = []  # without settings:read, only the numbers that have a mailbox
    try:
        connectors = api.get('/intake/connectors')['connectors']
    except CliError:
        connectors = None
    # One row per number rule (a number can have several, one per subaddress or sender); a carried number joins
    # the first rule for it, or gets a row of its own.
    rows, first = {}, {}
    for rule in rules:
        rows[rule['id']] = {'number': rule['to_number'], 'rule': rule, 'providers': []}
        if rule['to_number']:
            first.setdefault(comparable_number(rule['to_number']) or rule['to_number'], rule['id'])
    for number, provider, in_use in carried:
        row = rows.setdefault(first.get(number, number), {'number': number, 'rule': None, 'providers': []})
        row['providers'].append({'provider': provider, 'name': CARRIERS[provider], 'in_use': in_use})
    # --json rows keep the keys an inbound rule had (id, to_number, mailbox_id, mailbox_label, version),
    # None for a number with no mailbox rule, next to the new ones.
    found = [{'id': None, 'to_number': row['number'], 'mailbox_id': None, 'mailbox_label': None, 'version': None,
              **(row['rule'] or {}), **row, 'mailbox': (row['rule'] or {}).get('mailbox_label'),
              'email': _email_text(connectors, row['number'])} for row in rows.values()]

    def provided(row):
        return ', '.join(item['name'] + ('' if item['in_use'] else ' (not in use now)')
                         for item in row['providers']) or '-'

    def mailbox_text(row):
        """The mailbox, and the rule in words when it has receiving options, as Numbers shows it."""
        from .rules import RECEIVING_OPTION_KEYS, Names, receiving_sentence  # `rules` here is the list of number rules
        rule = row['rule']
        if not rule:
            return NO_MAILBOX
        off = rule.get('enabled') is False
        options = [key for key in RECEIVING_OPTION_KEYS if key != 'enabled' and rule.get(key) not in (None, False, [])]
        if not options and not off:
            return row['mailbox']
        names = {item['id']: item['name'] for item in connectors or []}
        return ('Off: ' if off else '') + receiving_sentence(rule, Names(), names)

    state.out().result(found, lambda out: out.table(
        _columns(ids, ['Fax number', 'Provided by', 'Mailbox', 'Email delivery']),
        [_with_id(ids, (row['rule'] or {}).get('id') or '-', [row['number'], provided(row), mailbox_text(row),
                                                             row['email']]) for row in found],
        empty='No fax numbers yet.'))


@numbers.command('add')
def numbers_add(number: str = typer.Argument(..., help='Your fax number, as faxes arrive on it.'),
                mailbox: str = typer.Option(..., '--mailbox', help='Mailbox that receives faxes sent to this number.'),
                account: str = rules.NUMBER_ACCOUNT, sender: list[str] = rules.NUMBER_FROM, days: str = rules.NUMBER_DAYS,
                between: str = rules.NUMBER_BETWEEN, email: str = rules.NUMBER_EMAIL, no_email: bool = rules.NUMBER_NO_EMAIL,
                urgent: bool = rules.NUMBER_URGENT, keep_days: int = rules.NUMBER_KEEP,
                position: int = rules.NUMBER_POSITION, any_number: bool = rules.NUMBER_ANY,
                subaddress: str = rules.NUMBER_SUBADDRESS, site: str = rules.NUMBER_SITE,
                forwarded_from: str = rules.NUMBER_FORWARDED, forwarded_unsigned: bool = rules.NUMBER_FORWARDED_UNSIGNED):
    """Send faxes that arrive on a number to a mailbox, optionally only some of them and with their own email and urgency."""
    api = state.api()
    found = resolve.mailbox(api, mailbox)
    options = rules.receiving_options(api, account=account, from_numbers=sender, days=days, between=between, email=email,
                                      no_email=no_email, urgent=urgent, keep_days=keep_days, position=position,
                                      any_number=any_number, subaddress=subaddress, site=site,
                                      forwarded_from=forwarded_from, forwarded_unsigned=forwarded_unsigned)
    result = api.post('/access/inbound-rules',
                      json=api.with_policy({'to_number': number, 'mailbox_id': found['id'], **options}))
    # A rule with options takes only some faxes: say which, as the rule reads on Numbers.
    saved = {'to_number': number, **options, **(result.get('rule') or {}), 'mailbox_label': found['label']}
    said = (rules.receiving_sentence(saved, rules.Names()) if options
            else f"Faxes to {number} now go to {found['label']}.")
    state.out().result(result, lambda out: out.line(said))


@numbers.command('update')
def numbers_update(number: str = typer.Argument(..., help='Fax number of the rule to change.'),
                   new_number: str = typer.Option(None, '--number', help='New fax number.'),
                   mailbox: str = typer.Option(None, '--mailbox', help='New mailbox.'),
                   account: str = rules.NUMBER_ACCOUNT, sender: list[str] = rules.NUMBER_FROM,
                   days: str = rules.NUMBER_DAYS, between: str = rules.NUMBER_BETWEEN, email: str = rules.NUMBER_EMAIL,
                   no_email: bool = rules.NUMBER_NO_EMAIL, urgent: bool = rules.NUMBER_URGENT,
                   keep_days: int = rules.NUMBER_KEEP, position: int = rules.NUMBER_POSITION,
                   any_number: bool = rules.NUMBER_ANY, subaddress: str = rules.NUMBER_SUBADDRESS,
                   site: str = rules.NUMBER_SITE, forwarded_from: str = rules.NUMBER_FORWARDED,
                   forwarded_unsigned: bool = rules.NUMBER_FORWARDED_UNSIGNED):
    """Change a fax number's mailbox, the number itself, or which of its faxes the rule takes and how."""
    api = state.api()
    body = rules.receiving_options(api, account=account, from_numbers=sender, days=days, between=between, email=email,
                                   no_email=no_email, urgent=urgent, keep_days=keep_days, position=position,
                                   any_number=any_number, subaddress=subaddress, site=site,
                                   forwarded_from=forwarded_from, forwarded_unsigned=forwarded_unsigned)
    if new_number:
        body['to_number'] = new_number
    if mailbox:
        body['mailbox_id'] = resolve.mailbox(api, mailbox)['id']
    _require_change(body)
    rule = resolve.inbound_rule(api, number)
    result = api.patch('/access/inbound-rules/' + segment(rule['id']), json=api.with_policy({**body, 'version': rule['version']}))
    state.out().result(result, lambda out: out.line(f"Rule for {result['rule'].get('to_number') or number} updated."))


# -- audit ---------------------------------------------------------------------------------------

@audit.command('list')
def audit_list(limit: int = typer.Option(50, '--limit', min=1, help='How many entries to show.'),
               who: str = typer.Option(None, '--who', help='Only actions by this user or integration.'),
               operation: str = typer.Option(None, '--operation', help='Only this kind of action, for example '
                                                                       'create_user.'),
               ids: bool = IDS):
    """Show the security log of access changes and sign-ins, newest first."""
    api = state.api()
    params = {'operation': operation}
    if who:
        params['actor_id'] = resolve.user(api, who)['id']
    items = api.pages('/access/audit', params=params, limit=limit)
    state.out().result(items, lambda out: out.table(_columns(ids, ['When', 'Who', 'Signed in with', 'Action', 'Target',
                                                                   'Outcome']),
        [_with_id(ids, item['id'], [local_time(item['at']), (item.get('actor') or {}).get('display_name') or 'Faxbot',
                                    item['credential_kind'], item['operation'],
                                    text((item.get('target') or {}).get('name')), item['outcome']])
         for item in items], empty='No audit entries.'))
