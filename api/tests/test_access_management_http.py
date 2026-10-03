"""Access management HTTP contract (/access and the /auth additions) through the real application.

Key clients use X-API-Key over HTTPS; browser clients log in with a password
and send the session cookie with Origin and X-CSRF-Token. The bootstrap key
passes every check, so denials use a real user created through the API.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.access.catalog import PERMISSIONS
from app.config_values import ConfigurationValues

BOOTSTRAP = 'synthetic-management-bootstrap-key'
ORIGIN = 'https://testserver'
COOKIE = '__Host-faxbot_session'
B = {'X-API-Key': BOOTSTRAP}
PASSWORD = 'synthetic-chosen-password-1'


def _environment(monkeypatch, tmp_path):
    for name in ConfigurationValues.environment_keys():
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        'DATABASE_URL': f"sqlite:///{tmp_path / 'management.db'}",
        'FAX_DATA_DIR': str(tmp_path / 'faxdata'),
        'FAXBOT_INSTALLATION_KEY_PATH': str(tmp_path / 'installation.key'),
        'FAXBOT_CONFIG_PATH': str(tmp_path / 'absent-legacy.json'),
        'FAXBOT_PROVIDERS_DIR': str(tmp_path / 'providers'),
        'FAX_DISABLED': 'true',
        'FAX_BACKEND': 'phaxio',
        'FAX_OUTBOUND_BACKEND': 'phaxio',
        'INBOUND_ENABLED': 'false',
        'REQUIRE_API_KEY': 'true',
        'API_KEY': BOOTSTRAP,
        'PUBLIC_API_URL': ORIGIN,
        'FAXBOT_CONSOLE_ORIGINS': ORIGIN,
        'MAX_REQUESTS_PER_MINUTE': '0',
        'ENABLE_PERSISTED_SETTINGS': 'false',
        'ENABLE_MCP_SSE': 'false',
        'ENABLE_MCP_HTTP': 'false',
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def client(monkeypatch, tmp_path):
    _environment(monkeypatch, tmp_path)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


class Browser:
    """One cookie session; the shared client's cookie jar is never used."""
    def __init__(self, client, token):
        self.client, self.token, self.csrf = client, token, None

    def headers(self, csrf=True, **extra):
        return {'Cookie': f'{COOKIE}={self.token}', **({'X-CSRF-Token': self.csrf} if csrf else {}), **extra}

    def get(self, path, **kwargs):
        return self.client.get(path, headers=self.headers(csrf=False), **kwargs)

    def post(self, path, json=None, csrf=True, **kwargs):
        return self.client.post(path, json=json, headers=self.headers(csrf=csrf), **kwargs)

    def patch(self, path, json=None, csrf=True):
        return self.client.patch(path, json=json, headers=self.headers(csrf=csrf))

    def refresh(self):
        me = self.get('/auth/me')
        assert me.status_code == 200, me.text
        self.csrf = me.json()['csrf_token']
        return me.json()


def _session_token(response):
    for name, value in response.headers.multi_items():
        if name.lower() == 'set-cookie' and value.startswith(COOKIE + '='):
            return value.split(';', 1)[0].split('=', 1)[1]
    raise AssertionError('no session cookie')


def login(client, user_login, password):
    client.cookies.clear()
    response = client.post('/auth/login', json={'login': user_login, 'password': password})
    assert response.status_code == 200, response.text
    client.cookies.clear()
    browser = Browser(client, _session_token(response))
    browser.refresh()
    return browser


def policy_version(client):
    return client.get('/auth/me', headers=B).json()['policy_version']


def create_user(client, user_login, display_name=None, headers=B):
    response = client.post('/access/users', headers=headers, json={'login': user_login,
        'display_name': display_name or user_login.title(), 'enabled': True,
        'expected_policy_version': policy_version(client)})
    assert response.status_code == 200, response.text
    return response.json()


def ready_user(client, user_login, role=None):
    """A user who has logged in and replaced the temporary password, optionally holding a role."""
    created = create_user(client, user_login)
    if role is not None:
        assign(client, created['user']['id'], role)
    browser = login(client, user_login, created['temporary_password'])
    changed = browser.post('/auth/password', {'current_password': created['temporary_password'], 'password': PASSWORD})
    assert changed.status_code == 200, changed.text
    browser.token = _session_token(changed)
    me = browser.refresh()
    assert me['password_change_required'] is False
    return browser, me['principal']


def user_version(client, principal_id):
    return client.get(f'/access/users/{principal_id}', headers=B).json()['version']


def assign(client, principal_id, role_id, resource_id='installation'):
    roles = {role['id']: role for role in client.get('/access/roles', headers=B).json()['items']}
    response = client.post('/access/assignments', headers=B, json={
        'subject': {'kind': 'principal', 'id': principal_id, 'version': user_version(client, principal_id)},
        'role': {'id': role_id, 'version': roles[role_id]['version']}, 'resource_id': resource_id,
        'expected_policy_version': policy_version(client)})
    assert response.status_code == 200, response.text
    return response.json()


def enroll_owner(client, owner_login='owner'):
    response = client.post('/auth/owner/enroll', headers=B, json={'login': owner_login, 'display_name': 'Owner',
        'expected_policy_version': policy_version(client)})
    assert response.status_code == 200, response.text
    return response.json()


# -- /auth additions ------------------------------------------------------------

def test_auth_me_reports_owner_status_and_what_the_actor_may_grant(client):
    me = client.get('/auth/me', headers=B).json()
    assert me['is_owner'] is False and me['can_enroll_owner'] is True
    assert me['grantable'] == {'installation': sorted(PERMISSIONS)}

    enrolled = enroll_owner(client)
    assert enrolled['user']['kind'] == 'user' and enrolled['user']['login'] == 'owner'
    assert enrolled['user']['password_change_required'] is True
    assert enrolled['policy_version'] == me['policy_version'] + 1
    fresh = login(client, 'owner', enrolled['temporary_password']).refresh()
    # A temporary password grants nothing until it is replaced.
    assert (fresh['is_owner'], fresh['can_enroll_owner'], fresh['grantable']) == (False, False, {'installation': []})

    owner = login(client, 'owner', enrolled['temporary_password'])
    changed = owner.post('/auth/password', {'current_password': enrolled['temporary_password'], 'password': PASSWORD})
    assert changed.status_code == 200, changed.text
    owner.token = _session_token(changed)
    me = owner.refresh()
    # can_enroll_owner drives the first-owner prompt, so it is off once a named Owner exists.
    assert me['is_owner'] is True and me['can_enroll_owner'] is False
    assert me['grantable']['installation'] == sorted(PERMISSIONS)
    assert client.get('/auth/me', headers=B).json()['can_enroll_owner'] is False
    # A complete Owner may still enroll another Owner through the API.
    second = owner.post('/auth/owner/enroll', {'login': 'second', 'display_name': 'Second',
                                               'expected_policy_version': policy_version(client)})
    assert second.status_code == 200, second.text

    plain, _ = ready_user(client, 'plain')
    me = plain.refresh()
    assert (me['is_owner'], me['can_enroll_owner'], me['grantable']) == (False, False, {'installation': []})


def test_owner_enrollment_requires_bootstrap_or_complete_owner(client):
    assert client.post('/auth/owner/enroll', json={'login': 'x', 'display_name': 'X',
        'expected_policy_version': 1}).status_code == 401
    stale = client.post('/auth/owner/enroll', headers=B, json={'login': 'owner', 'display_name': 'Owner',
        'expected_policy_version': policy_version(client) + 5})
    assert stale.status_code == 409
    bad = client.post('/auth/owner/enroll', headers=B, json={'login': 'has space', 'display_name': 'Owner',
        'expected_policy_version': policy_version(client)})
    assert bad.status_code == 400
    enroll_owner(client)
    duplicate = client.post('/auth/owner/enroll', headers=B, json={'login': 'OWNER', 'display_name': 'Again',
        'expected_policy_version': policy_version(client)})
    assert duplicate.status_code == 400 and duplicate.json()['detail'] == 'That name is already in use.'
    admin, _ = ready_user(client, 'admin', role='role_administrator')
    assert admin.refresh()['can_enroll_owner'] is False
    denied = admin.post('/auth/owner/enroll', {'login': 'second', 'display_name': 'Second',
        'expected_policy_version': policy_version(client)})
    assert denied.status_code == 403
    # The bootstrap credential stays the recovery path after named Owners exist.
    assert enroll_owner(client, 'second')['user']['login'] == 'second'


def test_auth_sessions_lists_another_principal_only_with_sessions_read(client):
    plain, principal = ready_user(client, 'plain')
    other, other_principal = ready_user(client, 'other')
    listed = client.get('/auth/sessions', headers=B, params={'principal_id': principal['id']})
    assert listed.status_code == 200, listed.text
    assert listed.json()['items'] and all(not item['current'] for item in listed.json()['items'])
    assert plain.get('/auth/sessions', params={'principal_id': other_principal['id']}).status_code == 403
    own = plain.get('/auth/sessions', params={'principal_id': principal['id']}).json()['items']
    assert any(item['current'] for item in own)


# -- reads, validation and transport ---------------------------------------------------

def test_permission_catalogue_and_list_validation(client):
    catalogue = client.get('/access/permissions', headers=B)
    assert catalogue.status_code == 200
    assert {item['permission'] for item in catalogue.json()['items']} == PERMISSIONS
    assert client.get('/access/permissions').status_code == 401
    assert client.get('/access/permissions', headers={'X-API-Key': 'fbk_live_000000000000_wrong'}).status_code == 401
    for params in ({'limit': 0}, {'limit': 201}, {'limit': 'many'}, {'cursor': 'not-a-cursor'}, {'kind': 'robots'}):
        response = client.get('/access/users', headers=B, params=params)
        assert response.status_code == 400, (params, response.text)
        assert response.json() == {'detail': 'Invalid access request.'}
    assert response.headers['cache-control'] == 'no-store'
    for body in ('{not json', '[]'):
        malformed = client.post('/access/groups', headers={**B, 'Content-Type': 'application/json'}, content=body)
        assert malformed.status_code == 400
    extra = client.post('/access/groups', headers=B, json={'name': 'g', 'expected_policy_version': 1, 'surprise': 1})
    assert extra.status_code == 400
    page = client.get('/access/users', headers=B, params={'limit': 1})
    assert page.status_code == 200 and set(page.json()) == {'items', 'next_cursor'}


def test_plain_user_sees_itself_and_is_forbidden_from_categories(client):
    plain, principal = ready_user(client, 'plain')
    for path in ('/access/roles', '/access/groups', '/access/resources', '/access/assignments', '/access/keys',
                 '/access/mailboxes', '/access/inbound-rules', '/access/audit'):
        assert plain.get(path).status_code == 403, path
    users = plain.get('/access/users').json()['items']
    assert [user['id'] for user in users] == [principal['id']]
    detail = plain.get(f"/access/users/{principal['id']}").json()
    assert detail['effective'] == {'installation': [], 'personal': []}
    assert plain.get(f"/access/assignments?subject_id={principal['id']}").status_code == 200
    assert plain.get(f"/access/keys?principal_id={principal['id']}").json()['items'] == []
    assert plain.get('/access/permissions').status_code == 200


def test_browser_mutations_need_exact_origin_and_csrf(client):
    admin, _ = ready_user(client, 'admin', role='role_administrator')
    body = {'name': 'Front desk', 'description': '', 'enabled': True, 'expected_policy_version': policy_version(client)}
    assert admin.post('/access/groups', body, csrf=False).status_code == 403
    no_origin = client.post('/access/groups', json=body, headers={**admin.headers(), 'Origin': 'https://evil.example'})
    assert no_origin.status_code == 403
    created = admin.post('/access/groups', body)
    assert created.status_code == 200, created.text
    assert created.json()['group']['name'] == 'Front desk'
    assert admin.get('/access/groups').json()['items'][0]['id'] == created.json()['group']['id']


# -- users and integrations ---------------------------------------------------------------

def test_user_lifecycle(client):
    before = policy_version(client)
    created = create_user(client, 'Casey', 'Casey Doe')
    user = created['user']
    assert created['policy_version'] == before + 1 and created['temporary_password']
    assert {key: user[key] for key in ('kind', 'login', 'display_name', 'enabled', 'password_change_required', 'version')} == {
        'kind': 'user', 'login': 'Casey', 'display_name': 'Casey Doe', 'enabled': True,
        'password_change_required': True, 'version': 1}
    assert set(user) >= {'memberships', 'assignments', 'keys', 'effective', 'created_at', 'last_login_at'}
    listed = client.get('/access/users', headers=B, params={'kind': 'user', 'q': 'case'}).json()['items']
    assert [item['id'] for item in listed] == [user['id']]
    assert client.get('/access/users', headers=B, params={'kind': 'integration'}).json()['items'] == []

    renamed = client.patch(f"/access/users/{user['id']}", headers=B, json={'display_name': 'Casey D.',
        'version': 1, 'expected_policy_version': created['policy_version']})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()['user']['display_name'] == 'Casey D.' and renamed.json()['user']['login'] == 'Casey'
    assert renamed.json()['user']['version'] == 2 and renamed.json()['policy_version'] == created['policy_version'] + 1

    stale = client.patch(f"/access/users/{user['id']}", headers=B, json={'enabled': False, 'version': 1,
        'expected_policy_version': policy_version(client)})
    assert stale.status_code == 409
    assert client.patch(f"/access/users/{user['id']}", headers=B, json={'enabled': False, 'version': 2,
        'expected_policy_version': policy_version(client) - 1}).status_code == 409
    assert client.patch('/access/users/missing-user', headers=B, json={'enabled': False, 'version': 1,
        'expected_policy_version': policy_version(client)}).status_code == 404
    assert client.get('/access/users/missing-user', headers=B).status_code == 404
    duplicate = client.post('/access/users', headers=B, json={'login': 'casey', 'display_name': 'Dup',
        'enabled': True, 'expected_policy_version': policy_version(client)})
    assert duplicate.status_code == 400 and duplicate.json()['detail'] == 'That name is already in use.'

    reset = client.post(f"/access/users/{user['id']}/reset-password", headers=B,
        json={'version': 2, 'expected_policy_version': policy_version(client)})
    assert reset.status_code == 200, reset.text
    assert reset.json()['temporary_password'] != created['temporary_password']
    assert reset.json()['user']['version'] == 3
    browser = login(client, 'casey', reset.json()['temporary_password'])
    assert browser.refresh()['password_change_required'] is True

    plain, _ = ready_user(client, 'plain')
    assert plain.patch(f"/access/users/{user['id']}", {'display_name': 'x', 'version': 3,
        'expected_policy_version': policy_version(client)}).status_code == 403
    assert plain.post('/access/users', {'login': 'nope', 'display_name': 'Nope', 'enabled': True,
        'expected_policy_version': policy_version(client)}).status_code == 403


def test_integrations_are_created_and_renamed_without_logins(client):
    created = client.post('/access/integrations', headers=B, json={'display_name': 'Billing sync', 'enabled': True,
        'expected_policy_version': policy_version(client)})
    assert created.status_code == 200, created.text
    integration = created.json()['integration']
    assert (integration['kind'], integration['login'], integration['password_change_required']) == ('integration', None, None)
    assert client.patch(f"/access/users/{integration['id']}", headers=B, json={'login': 'sync', 'version': 1,
        'expected_policy_version': policy_version(client)}).status_code == 400
    disabled = client.patch(f"/access/users/{integration['id']}", headers=B, json={'enabled': False, 'version': 1,
        'expected_policy_version': policy_version(client)})
    assert disabled.status_code == 200 and disabled.json()['user']['enabled'] is False
    # The bootstrap principal is not an editable identity.
    assert client.patch('/access/users/bootstrap', headers=B, json={'display_name': 'Nope',
        'version': user_version(client, 'bootstrap'), 'expected_policy_version': policy_version(client)}).status_code == 404


# -- roles ---------------------------------------------------------------------------------------

def test_custom_roles(client):
    roles = client.get('/access/roles', headers=B).json()['items']
    builtin = {role['id']: role for role in roles}
    assert builtin['role_owner']['builtin'] is True and builtin['role_owner']['permissions'] == sorted(PERMISSIONS)
    created = client.post('/access/roles', headers=B, json={'name': 'Night shift', 'description': 'After hours',
        'permissions': ['fax:read', 'fax:send'], 'enabled': True, 'expected_policy_version': policy_version(client)})
    assert created.status_code == 200, created.text
    role = created.json()['role']
    assert (role['builtin'], role['permissions'], role['version']) == (False, ['fax:read', 'fax:send'], 1)
    changed = client.patch(f"/access/roles/{role['id']}", headers=B, json={'permissions': ['fax:read'], 'version': 1,
        'expected_policy_version': created.json()['policy_version']})
    assert changed.status_code == 200, changed.text
    assert changed.json()['role']['permissions'] == ['fax:read'] and changed.json()['role']['name'] == 'Night shift'
    assert changed.json()['role']['version'] == 2
    assert client.patch(f"/access/roles/{role['id']}", headers=B, json={'enabled': False, 'version': 1,
        'expected_policy_version': policy_version(client)}).status_code == 409
    assert client.patch('/access/roles/role_owner', headers=B, json={'name': 'Boss', 'version': 1,
        'expected_policy_version': policy_version(client)}).status_code == 404
    assert client.patch('/access/roles/no-such-role', headers=B, json={'name': 'X', 'version': 1,
        'expected_policy_version': policy_version(client)}).status_code == 404
    assert client.post('/access/roles', headers=B, json={'name': 'Bad', 'permissions': ['fax:everything'],
        'expected_policy_version': policy_version(client)}).status_code == 400
    plain, _ = ready_user(client, 'plain')
    assert plain.post('/access/roles', {'name': 'Mine', 'permissions': [], 'enabled': True,
        'expected_policy_version': policy_version(client)}).status_code == 403


# -- groups and memberships --------------------------------------------------------------------------

def test_groups_and_memberships_report_new_versions(client):
    member = create_user(client, 'member')['user']
    created = client.post('/access/groups', headers=B, json={'name': 'Front desk', 'description': 'Reception',
        'enabled': True, 'expected_policy_version': policy_version(client)})
    assert created.status_code == 200, created.text
    group = created.json()['group']
    assert (group['member_count'], group['version'], group['members']) == (0, 1, [])
    added = client.post(f"/access/groups/{group['id']}/members", headers=B, json={'principal_id': member['id'],
        'principal_version': member['version'], 'group_version': 1, 'expected_policy_version': policy_version(client)})
    assert added.status_code == 200, added.text
    body = added.json()
    assert body['group']['version'] == 2 and body['group']['member_count'] == 1
    assert body['group']['members'][0]['membership_id'] == body['membership_id']
    duplicate = client.post(f"/access/groups/{group['id']}/members", headers=B, json={'principal_id': member['id'],
        'principal_version': member['version'], 'group_version': 2, 'expected_policy_version': policy_version(client)})
    assert duplicate.status_code == 400
    assert client.post(f"/access/groups/{group['id']}/members/{body['membership_id']}/remove", headers=B,
        json={'membership_version': 1, 'group_version': 1, 'expected_policy_version': policy_version(client)}).status_code == 409
    removed = client.post(f"/access/groups/{group['id']}/members/{body['membership_id']}/remove", headers=B,
        json={'membership_version': 1, 'group_version': 2, 'expected_policy_version': policy_version(client)})
    assert removed.status_code == 200, removed.text
    assert removed.json()['group']['version'] == 3 and removed.json()['group']['members'] == []
    renamed = client.patch(f"/access/groups/{group['id']}", headers=B, json={'name': 'Reception', 'version': 3,
        'expected_policy_version': policy_version(client)})
    assert renamed.status_code == 200 and renamed.json()['group']['description'] == 'Reception'
    assert client.get('/access/groups/no-such-group', headers=B).status_code == 404
    assert client.post('/access/groups/no-such-group/members', headers=B, json={'principal_id': member['id'],
        'principal_version': 1, 'group_version': 1, 'expected_policy_version': policy_version(client)}).status_code == 404


# -- resources and assignments --------------------------------------------------------------------------

def test_resources_and_assignments(client):
    user = create_user(client, 'operator')['user']
    resources = client.get('/access/resources', headers=B).json()['items']
    kinds = {item['kind'] for item in resources}
    assert {'installation', 'legacy', 'personal'} <= kinds
    personal = [item for item in resources if item['kind'] == 'personal' and item['principal_id'] == user['id']]
    assert len(personal) == 1 and personal[0]['name'] == 'Operator'
    assert client.get('/access/resources', headers=B, params={'kind': 'mailbox'}).json()['items'] == []

    granted = assign(client, user['id'], 'role_fax_operator')
    assignment = granted['assignment']
    assert assignment['subject'] == {'kind': 'principal', 'id': user['id'], 'name': 'Operator'}
    assert assignment['role'] == {'id': 'role_fax_operator', 'name': assignment['role']['name'], 'builtin': True}
    assert assignment['resource']['id'] == 'installation' and assignment['version'] == 1
    assert granted['subject'] == {'kind': 'principal', 'id': user['id'], 'version': user['version'] + 1}
    listed = client.get('/access/assignments', headers=B, params={'subject_id': user['id']}).json()['items']
    assert [item['id'] for item in listed] == [assignment['id']]
    detail = client.get(f"/access/users/{user['id']}", headers=B).json()
    # The role takes effect once the temporary password is replaced.
    assert detail['version'] == user['version'] + 1 and detail['effective']['installation'] == []
    assert [item['id'] for item in detail['assignments']] == [assignment['id']]

    roles = {role['id']: role for role in client.get('/access/roles', headers=B).json()['items']}
    duplicate = client.post('/access/assignments', headers=B, json={
        'subject': {'kind': 'principal', 'id': user['id'], 'version': detail['version']},
        'role': {'id': 'role_fax_operator', 'version': roles['role_fax_operator']['version']},
        'resource_id': 'installation', 'expected_policy_version': policy_version(client)})
    assert duplicate.status_code == 400
    stale = client.post('/access/assignments', headers=B, json={
        'subject': {'kind': 'principal', 'id': user['id'], 'version': user['version']},
        'role': {'id': 'role_fax_viewer', 'version': roles['role_fax_viewer']['version']},
        'resource_id': 'installation', 'expected_policy_version': policy_version(client)})
    assert stale.status_code == 409
    unknown = client.post('/access/assignments', headers=B, json={
        'subject': {'kind': 'principal', 'id': user['id'], 'version': detail['version']},
        'role': {'id': 'role_fax_viewer', 'version': roles['role_fax_viewer']['version']},
        'resource_id': 'no-such-resource', 'expected_policy_version': policy_version(client)})
    assert unknown.status_code == 400

    removed = client.post(f"/access/assignments/{assignment['id']}/remove", headers=B,
        json={'version': 1, 'expected_policy_version': policy_version(client)})
    assert removed.status_code == 200, removed.text
    assert removed.json()['subject'] == {'id': user['id'], 'version': detail['version'] + 1}
    assert client.post(f"/access/assignments/{assignment['id']}/remove", headers=B,
        json={'version': 1, 'expected_policy_version': policy_version(client)}).status_code == 404


# -- keys --------------------------------------------------------------------------------------------------

def test_key_lifecycle_through_the_key_projection(client):
    integration = client.post('/access/integrations', headers=B, json={'display_name': 'Scanner', 'enabled': True,
        'expected_policy_version': policy_version(client)}).json()['integration']
    assign(client, integration['id'], 'role_fax_operator')
    version = user_version(client, integration['id'])
    ceiling = [{'permission': 'fax:send', 'resource_id': 'installation'},
               {'permission': 'fax:read', 'resource_id': 'installation'}]
    issued = client.post('/access/keys', headers=B, json={'principal': {'id': integration['id'], 'version': version},
        'name': 'scanner', 'note': 'Lobby scanner', 'expires_at': None, 'ceiling': ceiling,
        'expected_policy_version': policy_version(client)})
    assert issued.status_code == 200, issued.text
    key, token = issued.json()['key'], issued.json()['token']
    assert token.startswith('fbk_live_' + key['id'])
    assert key['principal'] == {'id': integration['id'], 'display_name': 'Scanner', 'kind': 'integration'}
    assert key['ceiling'] == sorted(ceiling, key=lambda c: c['permission']) and key['pending_review'] is False
    sent = client.post('/fax', headers={'X-API-Key': token}, data={'to': '+15551230001'},
        files={'file': ('a.txt', b'synthetic key note', 'text/plain')})
    assert sent.status_code == 202, sent.text
    listed = client.get('/access/keys', headers=B, params={'principal_id': integration['id']}).json()['items']
    assert [item['id'] for item in listed] == [key['id']]

    patched = client.patch(f"/access/keys/{key['id']}", headers=B, json={'note': None, 'version': 1,
        'expected_policy_version': policy_version(client)})
    assert patched.status_code == 200, patched.text
    assert (patched.json()['key']['note'], patched.json()['key']['name'], patched.json()['key']['version']) == (None, 'scanner', 2)
    assert client.patch(f"/access/keys/{key['id']}", headers=B, json={'name': 'x', 'version': 1,
        'expected_policy_version': policy_version(client)}).status_code == 409

    rotated = client.post(f"/access/keys/{key['id']}/rotate", headers=B,
        json={'version': 2, 'expected_policy_version': policy_version(client)})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()['key']['id'] == key['id'] and rotated.json()['token'] != token
    job = '/fax/' + sent.json()['id']
    assert client.get(job, headers={'X-API-Key': token}).status_code == 401
    assert client.get(job, headers={'X-API-Key': rotated.json()['token']}).status_code == 200

    assert client.post(f"/access/keys/{key['id']}/approve", headers=B, json={
        'principal': {'id': integration['id'], 'version': user_version(client, integration['id'])},
        'ceiling': ceiling, 'version': 3, 'expected_policy_version': policy_version(client)}).status_code == 404
    revoked = client.post(f"/access/keys/{key['id']}/revoke", headers=B,
        json={'version': 3, 'expected_policy_version': policy_version(client)})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()['key']['revoked_at'] is not None
    assert client.get(job, headers={'X-API-Key': rotated.json()['token']}).status_code == 401
    assert client.post('/access/keys/000000000000/revoke', headers=B,
        json={'version': 1, 'expected_policy_version': policy_version(client)}).status_code == 404
    # The legacy compatibility list reads the same projection and shows the revocation.
    legacy = client.get('/admin/api-keys', headers=B).json()
    assert [(item['key_id'], item['revoked_at'] is not None) for item in legacy] == [(key['id'], True)]

    plain, principal = ready_user(client, 'plain')
    assert plain.get('/access/keys').status_code == 403
    assert plain.post(f"/access/keys/{key['id']}/revoke", {'version': 4,
        'expected_policy_version': policy_version(client)}).status_code == 403
    assert plain.post('/access/keys', {'principal': {'id': principal['id'], 'version': principal['version']},
        'ceiling': [], 'expected_policy_version': policy_version(client)}).status_code == 403


def test_key_expiry_is_accepted_as_iso_text_and_stored_as_naive_utc(client):
    integration = client.post('/access/integrations', headers=B, json={'display_name': 'Expiring', 'enabled': True,
        'expected_policy_version': policy_version(client)}).json()['integration']
    future = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)).replace(microsecond=0)
    issued = client.post('/access/keys', headers=B, json={'principal': {'id': integration['id'], 'version': 1},
        'name': 'temp', 'note': '', 'expires_at': future.isoformat(), 'ceiling': [],
        'expected_policy_version': policy_version(client)})
    assert issued.status_code == 200, issued.text
    key = issued.json()['key']
    assert key['expires_at'] == future.isoformat()
    later = future + timedelta(days=1)
    aware = later.replace(tzinfo=timezone(timedelta(hours=2))) + timedelta(hours=2)
    patched = client.patch(f"/access/keys/{key['id']}", headers=B, json={'expires_at': aware.isoformat(),
        'version': key['version'], 'expected_policy_version': policy_version(client)})
    assert patched.status_code == 200, patched.text
    assert patched.json()['key']['expires_at'] == later.isoformat()
    assert patched.json()['key']['version'] == key['version'] + 1
    assert client.patch(f"/access/keys/{key['id']}", headers=B, json={'expires_at': 'next tuesday',
        'version': key['version'] + 1, 'expected_policy_version': policy_version(client)}).status_code == 400


def test_key_ceiling_cannot_exceed_the_issuer(client):
    manager, principal = ready_user(client, 'manager', role='role_fax_operator')
    integration = client.post('/access/integrations', headers=B, json={'display_name': 'Robot', 'enabled': True,
        'expected_policy_version': policy_version(client)}).json()['integration']
    assert client.post('/access/keys', headers=B, json={'principal': {'id': integration['id'], 'version': 1},
        'ceiling': [{'permission': 'fax:everything', 'resource_id': 'installation'}],
        'expected_policy_version': policy_version(client)}).status_code == 400
    assert manager.post('/access/keys', {'principal': {'id': integration['id'], 'version': 1},
        'ceiling': [{'permission': 'fax:read', 'resource_id': 'installation'}],
        'expected_policy_version': policy_version(client)}).status_code == 403


# -- sessions ------------------------------------------------------------------------------------------------

def test_sessions_list_and_revoke(client):
    plain, principal = ready_user(client, 'plain')
    own = plain.get('/access/sessions').json()['items']
    current = [item for item in own if item['current']]
    assert len(current) == 1 and current[0]['principal'] == {'id': principal['id'], 'display_name': 'Plain'}
    assert current[0]['source_kind'] == 'password'
    others = client.get('/access/sessions', headers=B, params={'principal_id': principal['id']})
    assert others.status_code == 200 and others.json()['items']
    assert plain.get('/access/sessions', params={'principal_id': 'bootstrap'}).status_code == 403
    stale = client.post(f"/access/sessions/{current[0]['session_id']}/revoke", headers=B,
        json={'expected_policy_version': policy_version(client) + 3})
    assert stale.status_code == 409
    revoked = client.post(f"/access/sessions/{current[0]['session_id']}/revoke", headers=B,
        json={'expected_policy_version': policy_version(client)})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()['session_id'] == current[0]['session_id'] and revoked.json()['changed'] is True
    assert plain.get('/auth/me').status_code == 401
    assert client.post('/access/sessions/no-such-session/revoke', headers=B,
        json={'expected_policy_version': policy_version(client)}).status_code == 404


# -- mailboxes and inbound rules -------------------------------------------------------------------------------

def test_mailboxes_and_inbound_rules(client):
    created = client.post('/access/mailboxes', headers=B, json={'label': 'Front desk', 'enabled': True,
        'expected_policy_version': policy_version(client)})
    assert created.status_code == 200, created.text
    mailbox = created.json()['mailbox']
    assert (mailbox['label'], mailbox['enabled'], mailbox['rule_count'], mailbox['version']) == ('Front desk', True, 0, 1)
    renamed = client.patch(f"/access/mailboxes/{mailbox['id']}", headers=B, json={'label': 'Reception', 'version': 1,
        'expected_policy_version': policy_version(client)})
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()['mailbox']['id'], renamed.json()['mailbox']['resource_id']) == (mailbox['id'], mailbox['resource_id'])
    assert renamed.json()['mailbox']['label'] == 'Reception' and renamed.json()['mailbox']['version'] == 2
    assert client.patch(f"/access/mailboxes/{mailbox['id']}", headers=B, json={'enabled': False, 'version': 1,
        'expected_policy_version': policy_version(client)}).status_code == 409
    assert client.post('/access/mailboxes', headers=B, json={'label': 'reception', 'enabled': True,
        'expected_policy_version': policy_version(client)}).status_code == 400
    assert client.patch('/access/mailboxes/no-such-mailbox', headers=B, json={'enabled': False, 'version': 1,
        'expected_policy_version': policy_version(client)}).status_code == 404

    rule = client.post('/access/inbound-rules', headers=B, json={'to_number': '+15551230001',
        'mailbox_id': mailbox['id'], 'expected_policy_version': policy_version(client)})
    assert rule.status_code == 200, rule.text
    assert {k: rule.json()['rule'][k] for k in ('to_number', 'mailbox_id', 'mailbox_label', 'version')} == {
        'to_number': '+15551230001', 'mailbox_id': mailbox['id'], 'mailbox_label': 'Reception', 'version': 1}
    rule_id = rule.json()['rule']['id']
    assert client.post('/access/inbound-rules', headers=B, json={'to_number': 'call me', 'mailbox_id': mailbox['id'],
        'expected_policy_version': policy_version(client)}).status_code == 400
    assert client.post('/access/inbound-rules', headers=B, json={'to_number': '+15551230002',
        'mailbox_id': 'no-such-mailbox', 'expected_policy_version': policy_version(client)}).status_code == 404
    changed = client.patch(f'/access/inbound-rules/{rule_id}', headers=B, json={'to_number': '+15551230009',
        'version': 1, 'expected_policy_version': policy_version(client)})
    assert changed.status_code == 200 and changed.json()['rule']['to_number'] == '+15551230009'
    assert client.get('/access/mailboxes', headers=B).json()['items'][0]['rule_count'] == 1
    assert [item['id'] for item in client.get('/access/inbound-rules', headers=B).json()['items']] == [rule_id]
    resources = client.get('/access/resources', headers=B, params={'kind': 'mailbox'}).json()['items']
    assert [(item['id'], item['mailbox_id'], item['name']) for item in resources] == [
        (mailbox['resource_id'], mailbox['id'], 'Reception')]


# -- audit ------------------------------------------------------------------------------------------------------

def test_audit_lists_allowed_and_denied_operations(client):
    plain, principal = ready_user(client, 'plain')
    assert plain.post('/access/groups', {'name': 'Nope', 'expected_policy_version': policy_version(client)}).status_code == 403
    page = client.get('/access/audit', headers=B, params={'limit': 200})
    assert page.status_code == 200, page.text
    items = page.json()['items']
    created = [item for item in items if item['operation'] == 'create_user']
    assert created and created[0]['outcome'] == 'allowed' and created[0]['actor'] == {'id': 'bootstrap',
        'display_name': created[0]['actor']['display_name']}
    assert created[0]['target']['id'] == principal['id']
    denied = client.get('/access/audit', headers=B, params={'actor_id': principal['id'], 'operation': 'create_group'}).json()['items']
    assert [(item['outcome'], item['credential_kind']) for item in denied] == [('denied', 'session')]
    assert plain.get('/access/audit').status_code == 403
