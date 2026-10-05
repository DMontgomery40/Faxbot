"""Terminal handshake: a single-use ticket in the first message, exact Origin for browsers, live revocation."""
import json
import threading

import pytest
import sqlalchemy as sa
from starlette.websockets import WebSocketDisconnect

import app.main as main_module
import app.terminal as terminal_module
from api.tests.test_access_management_http import B, COOKIE, ORIGIN, client, policy_version, ready_user

SOCKET = 'wss://testserver/admin/terminal'


class FakeTerminal:
    """Stands in for the PTY so the test sees exactly which input reached the shell."""
    def __init__(self):
        self.inputs, self.resizes, self.closed = [], [], []

    async def create_session(self, websocket, session_id):
        await websocket.send_text(json.dumps({'type': 'output', 'data': 'ready'}))
        return True

    async def send_input(self, session_id, data):
        self.inputs.append(data)
        return True

    async def resize_terminal(self, session_id, cols, rows):
        self.resizes.append((cols, rows))
        return True

    async def close_session(self, session_id):
        self.closed.append(session_id)


@pytest.fixture
def shell(monkeypatch):
    monkeypatch.setenv('ENABLE_ADMIN_EXEC', 'true')
    fake = FakeTerminal()
    monkeypatch.setattr(terminal_module, 'terminal_manager', fake)
    monkeypatch.setattr(terminal_module, 'check_terminal_requirements', lambda: [])
    monkeypatch.setattr(terminal_module, 'RECHECK_SECONDS', 0.2)
    monkeypatch.setattr(terminal_module, 'BATCH_SECONDS', 0.0)
    monkeypatch.setattr(main_module, '_TERMINAL_AUTH_SECONDS', 0.5)
    return fake


def receive(ws, timeout=5.0):
    """One server message, failing instead of hanging when the server sends nothing."""
    result = {}

    def run():
        try:
            result['message'] = ws.receive()
        except BaseException as error:  # surfaced in the test thread
            result['error'] = error
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), 'the terminal sent nothing in time'
    if 'error' in result:
        raise result['error']
    return result['message']


def output(ws):
    message = receive(ws)
    assert message['type'] == 'websocket.send', message
    return json.loads(message['text'])


def closed_with(ws, code=1008):
    message = receive(ws)
    assert message['type'] == 'websocket.close' and message['code'] == code, message
    return True


def ticket(client, headers=B):
    response = client.post('/admin/terminal/ticket', headers=headers, json={})
    assert response.status_code == 200, response.text
    assert set(response.json()) == {'ticket', 'expires_at'}
    return response.json()['ticket']


def connect(client, headers):
    return client.websocket_connect(SOCKET, headers=headers)


def terminal_audits(client):
    """The installation's persistent audit rows for the terminal: (outcome, principal, details)."""
    runtime = client.app.state.access_runtime
    audit = runtime.store.tables['access_audit']
    with runtime.store.engine.connect() as connection:
        rows = connection.execute(sa.select(audit).where(audit.c.operation == 'host.terminal')
                                  .order_by(audit.c.created_at)).mappings().all()
    return [(row['outcome'], row['actor_principal_id'], json.loads(row['details'])) for row in rows]


def integration_with(client, role_ids, ceiling):
    """A new integration holding these roles at the installation, and a key limited to ``ceiling``."""
    integration = client.post('/access/integrations', headers=B, json={'display_name': 'Ops robot', 'enabled': True,
        'expected_policy_version': policy_version(client)}).json()['integration']
    version = integration['version']
    for role_id in role_ids:
        roles = {role['id']: role for role in client.get('/access/roles', headers=B).json()['items']}
        granted = client.post('/access/assignments', headers=B, json={
            'subject': {'kind': 'principal', 'id': integration['id'], 'version': version},
            'role': {'id': role_id, 'version': roles[role_id]['version']},
            'resource_id': 'installation', 'expected_policy_version': policy_version(client)})
        assert granted.status_code == 200, granted.text
        version = granted.json()['subject']['version']
    issued = client.post('/access/keys', headers=B, json={
        'principal': {'id': integration['id'], 'version': version}, 'name': 'ops',
        'ceiling': [{'permission': permission, 'resource_id': 'installation'} for permission in ceiling],
        'expected_policy_version': policy_version(client)})
    assert issued.status_code == 200, issued.text
    return integration['id'], issued.json()


def terminal_role(client):
    """A role of the owner's own that holds only the terminal."""
    created = client.post('/access/roles', headers=B, json={'name': 'Terminal', 'description': 'Server terminal',
        'permissions': ['host:terminal'], 'enabled': True, 'expected_policy_version': policy_version(client)})
    assert created.status_code == 200, created.text
    return created.json()['role']['id']


def test_production_timings_meet_the_revocation_bound():
    assert terminal_module.RECHECK_SECONDS + terminal_module.BATCH_SECONDS < 5
    assert main_module._TERMINAL_AUTH_SECONDS == 5.0
    assert main_module._TERMINAL_TICKET_TTL.total_seconds() == 60


def test_ticket_requires_host_terminal_and_the_exec_gate(client, shell, monkeypatch):
    assert client.post('/admin/terminal/ticket', json={}).status_code == 401
    admin, _ = ready_user(client, 'admin', role='role_administrator')
    assert admin.post('/admin/terminal/ticket', {}).status_code == 403
    owner, _ = ready_user(client, 'owner', role='role_owner')
    assert owner.post('/admin/terminal/ticket', {}, csrf=False).status_code == 403
    assert ticket(client, owner.headers())
    monkeypatch.setenv('ENABLE_ADMIN_EXEC', 'false')
    off = client.post('/admin/terminal/ticket', headers=B, json={})
    assert off.status_code == 404 and off.json()['detail'] == 'The terminal is not available on this server.'


def test_key_minted_ticket_opens_a_terminal_from_any_origin(client, shell):
    secret = ticket(client)
    with connect(client, {'Origin': 'null'}) as ws:
        ws.send_json({'type': 'auth', 'ticket': secret})
        assert output(ws) == {'type': 'output', 'data': 'ready'}
        ws.send_json({'type': 'resize', 'cols': 100, 'rows': 30})
        ws.send_json({'type': 'input', 'data': 'ls\r'})
        ws.send_json({'type': 'ping'})
        assert output(ws) == {'type': 'pong'}
    assert shell.inputs == ['ls\r'] and shell.resizes == [(100, 30)] and len(shell.closed) == 1
    # A ticket opens exactly one socket.
    with connect(client, {'Origin': 'null'}) as ws:
        ws.send_json({'type': 'auth', 'ticket': secret})
        assert closed_with(ws)


def test_the_first_message_must_be_a_valid_ticket_in_time(client, shell):
    with connect(client, {}) as ws:
        assert closed_with(ws)
    with connect(client, {}) as ws:
        ws.send_json({'type': 'auth', 'ticket': 'not-a-ticket'})
        assert closed_with(ws)
    secret = ticket(client)
    with connect(client, {}) as ws:
        ws.send_json({'type': 'input', 'data': 'ls\r', 'ticket': secret})
        assert closed_with(ws)
    assert shell.inputs == []
    # No credential is accepted from the URL, not even a valid ticket.
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(f'{SOCKET}?ticket={ticket(client)}'):
            pass
    assert refused.value.code == 1008
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f'{SOCKET}?api_key={B["X-API-Key"]}'):
            pass


def test_browser_sockets_need_an_exact_allowed_origin(client, shell):
    owner, _ = ready_user(client, 'owner', role='role_owner')
    cookie = {'Cookie': f'{COOKIE}={owner.token}'}
    for origin in ('https://evil.example', 'null'):
        secret = ticket(client, owner.headers())
        with connect(client, {**cookie, 'Origin': origin}) as ws:
            ws.send_json({'type': 'auth', 'ticket': secret})
            assert closed_with(ws)
    # A session-minted ticket is held to the browser rule even without the cookie.
    secret = ticket(client, owner.headers())
    with connect(client, {'Origin': 'https://evil.example'}) as ws:
        ws.send_json({'type': 'auth', 'ticket': secret})
        assert closed_with(ws)
    # A socket carrying the session cookie is a browser socket even with a key-minted ticket.
    secret = ticket(client)
    with connect(client, {**cookie, 'Origin': 'https://evil.example'}) as ws:
        ws.send_json({'type': 'auth', 'ticket': secret})
        assert closed_with(ws)
    secret = ticket(client, owner.headers())
    with connect(client, {**cookie, 'Origin': ORIGIN}) as ws:
        ws.send_json({'type': 'auth', 'ticket': secret})
        assert output(ws)['data'] == 'ready'
    assert shell.inputs == []


def test_a_real_shell_echoes_input(client, monkeypatch):
    monkeypatch.setenv('ENABLE_ADMIN_EXEC', 'true')
    monkeypatch.setenv('SHELL', '/bin/sh')
    secret = ticket(client)
    with connect(client, {}) as ws:
        ws.send_json({'type': 'auth', 'ticket': secret})
        ws.send_json({'type': 'input', 'data': 'echo faxbot-$((40+2))\r'})
        seen = ''
        for _ in range(200):
            message = output(ws)
            seen += message.get('data', '')
            if 'faxbot-42' in seen:
                break
        assert 'faxbot-42' in seen


def test_revoking_the_minting_session_closes_the_socket(client, shell):
    owner, _ = ready_user(client, 'owner', role='role_owner')
    session_id = owner.refresh()['session']['id']
    secret = ticket(client, owner.headers())
    with connect(client, {'Cookie': f'{COOKIE}={owner.token}', 'Origin': ORIGIN}) as ws:
        ws.send_json({'type': 'auth', 'ticket': secret})
        assert output(ws)['data'] == 'ready'
        ws.send_json({'type': 'input', 'data': 'before\r'})
        ws.send_json({'type': 'ping'})
        assert output(ws) == {'type': 'pong'}
        revoked = client.post(f'/access/sessions/{session_id}/revoke', headers=B,
                              json={'expected_policy_version': policy_version(client)})
        assert revoked.status_code == 200, revoked.text
        ws.send_json({'type': 'input', 'data': 'after\r'})
        assert closed_with(ws)
    assert shell.inputs == ['before\r']


def test_revoking_the_minting_key_closes_an_idle_socket(client, shell):
    _, issued = integration_with(client, [terminal_role(client)], ['host:terminal'])
    secret = ticket(client, {'X-API-Key': issued['token']})
    with connect(client, {}) as ws:
        ws.send_json({'type': 'auth', 'ticket': secret})
        assert output(ws)['data'] == 'ready'
        key = issued['key']
        assert client.post(f"/access/keys/{key['id']}/revoke", headers=B, json={
            'version': key['version'], 'expected_policy_version': policy_version(client)}).status_code == 200
        assert closed_with(ws)


def test_the_terminal_is_the_owners_and_is_granted_to_others_on_purpose(client, shell):
    """A Host Operator restarts Faxbot but has no terminal; an owner's own role can grant it."""
    operator, issued = integration_with(client, ['role_host_operator'], ['host:restart'])
    refused = client.post('/admin/terminal/ticket', headers={'X-API-Key': issued['token']}, json={})
    assert refused.status_code == 403
    assert ('denied', operator, {'request': 'POST /admin/terminal/ticket', 'reason': 'forbidden'}) in terminal_audits(client)

    granted, issued = integration_with(client, ['role_host_operator', terminal_role(client)], ['host:terminal'])
    secret = ticket(client, {'X-API-Key': issued['token']})
    with connect(client, {}) as ws:
        ws.send_json({'type': 'auth', 'ticket': secret})
        assert output(ws)['data'] == 'ready'
    # The ticket and the session start are each recorded, in that order.
    mine = [(outcome, details) for outcome, principal, details in terminal_audits(client) if principal == granted]
    assert mine == [('allowed', {'request': 'POST /admin/terminal/ticket'}),
                    ('allowed', {'request': 'WEBSOCKET /admin/terminal', 'session': 'started'})]


def test_a_refused_session_start_closes_the_socket_before_any_shell(client, shell, monkeypatch):
    from app.access.mutation_types import MutationDeniedError, MutationReason
    secret = ticket(client)

    def refused(*args, **kwargs):
        raise MutationDeniedError(MutationReason.FORBIDDEN)
    monkeypatch.setattr(main_module, 'authorize_operation', refused)
    with connect(client, {}) as ws:
        ws.send_json({'type': 'auth', 'ticket': secret})
        assert closed_with(ws)  # no "ready": the shell never started
    assert shell.inputs == [] and shell.closed == []
