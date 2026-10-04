"""Find users, groups, roles and other records by the names people use, not internal ids."""
import re

from .client import segment
from .errors import EXIT_FAILURE, EXIT_NOT_FOUND, CliError

_HEX_ID = re.compile(r'[0-9a-f]{32}')


def _one(matches, what, value, hint):
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise CliError(f"No {what} matches '{value}'. {hint}", EXIT_NOT_FOUND)
    raise CliError(f"More than one {what} matches '{value}'. Use the id instead ({hint.rstrip('.')}, "
                   f"with --ids).", EXIT_FAILURE)


def _same(left, right):
    return isinstance(left, str) and isinstance(right, str) and left.strip().casefold() == right.strip().casefold()


def user(api, who):
    """A user or integration by login, display name or id, with its current version."""
    if _HEX_ID.fullmatch(who) or who == 'bootstrap':
        return api.get('/access/users/' + segment(who))
    items = api.pages('/access/users', params={'q': who[:100]})
    matches = [item for item in items if _same(item.get('login'), who)]
    if not matches:
        matches = [item for item in items if _same(item.get('display_name'), who)]
    found = _one(matches, 'user or integration', who, "See 'faxbot access users list'.")
    return api.get('/access/users/' + segment(found['id']))


def group(api, name):
    if _HEX_ID.fullmatch(name):
        return api.get('/access/groups/' + segment(name))
    items = api.pages('/access/groups')
    found = _one([item for item in items if _same(item['name'], name) or item['id'] == name],
                 'group', name, "See 'faxbot access groups list'.")
    return api.get('/access/groups/' + segment(found['id']))


def role(api, name):
    items = api.pages('/access/roles')
    return _one([item for item in items if item['id'] == name or _same(item['name'], name)],
                'role', name, "See 'faxbot access roles list'.")


def mailbox(api, label):
    items = api.pages('/access/mailboxes')
    return _one([item for item in items if item['id'] == label or _same(item['label'], label)],
                'mailbox', label, "See 'faxbot numbers mailboxes list'.")


def _digits(number):
    return re.sub(r'[^0-9]', '', number or '')


def inbound_rule(api, number):
    items = api.pages('/access/inbound-rules')
    matches = [item for item in items if item['id'] == number]
    if not matches and _digits(number):
        matches = [item for item in items if _digits(item['to_number']) == _digits(number)]
    return _one(matches, 'fax number rule', number, "See 'faxbot numbers list'.")


def key(api, key_id):
    items = api.pages('/access/keys')
    matches = [item for item in items if item['id'] == key_id]
    if not matches:
        matches = [item for item in items if _same(item.get('name'), key_id)]
    return _one(matches, 'API key', key_id, "See 'faxbot access keys list'.")


def resource(api, reference):
    """Where access applies: installation, unassigned, mailbox:NAME, personal:USER, a name or an id."""
    value = reference.strip()
    if value.casefold() in {'installation', 'whole installation'}:
        return {'id': 'installation', 'kind': 'installation', 'name': 'Whole installation'}
    kind, _, name = value.partition(':')
    items = api.pages('/access/resources')
    if value.casefold() in {'unassigned', 'unassigned faxes', 'legacy'}:
        matches = [item for item in items if item['kind'] == 'legacy']
    elif name and kind.casefold() == 'mailbox':
        matches = [item for item in items if item['kind'] == 'mailbox' and _same(item['name'], name)]
    elif name and kind.casefold() == 'personal':
        person = user(api, name)
        matches = [item for item in items if item['kind'] == 'personal' and item.get('principal_id') == person['id']]
    else:
        matches = [item for item in items if item['id'] == value] or [
            item for item in items if _same(item['name'], value)]
    return _one(matches, 'place', reference, "See 'faxbot access resources list'.")


def subject(api, who):
    """A user, integration or group that can hold a role, as an API subject reference."""
    kind, _, name = who.partition(':')
    if name and kind in {'group', 'user', 'integration'}:
        if kind == 'group':
            found = group(api, name)
            return {'kind': 'group', 'id': found['id'], 'version': found['version']}, found['name']
        found = user(api, name)
        return {'kind': 'principal', 'id': found['id'], 'version': found['version']}, found['display_name']
    try:
        found = user(api, who)
    except CliError as error:
        if error.exit_code != EXIT_NOT_FOUND:
            raise
        found_group = group(api, who)
        return {'kind': 'group', 'id': found_group['id'], 'version': found_group['version']}, found_group['name']
    return {'kind': 'principal', 'id': found['id'], 'version': found['version']}, found['display_name']
