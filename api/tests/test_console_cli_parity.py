"""Every operator route has a caller in the console and a command in the command line.

Routes come from the application table, as in test_route_policy_coverage.py. A
route counts as called when a path in the console source (api/admin_ui/src,
tests and mocks excluded) or in the command line (api/app/cli) matches it,
segment by segment; a value filled in at run time matches a {parameter}
segment only. Matching is by path, so every method on a called path counts.

Routes that are not for operators, or belong to one surface by design, are
listed with a reason. AWAITING_CONSOLE and AWAITING_CLI hold the gaps still
open: whoever closes one removes its entry in the same change (a listed route
that has gained its caller fails here), and the lead empties both lists
before the work merges.
"""
import ast
from pathlib import Path
import re

from fastapi.routing import APIWebSocketRoute, iter_route_contexts
from starlette.routing import Mount

from app import main

API_ROOT = Path(__file__).resolve().parents[1]
CONSOLE_SOURCE = API_ROOT / 'admin_ui' / 'src'
CLI_SOURCE = API_ROOT / 'app' / 'cli'
HOLE = '\x00'

# Not an operator surface: provider and partner traffic, internal calls, the public
# fax API that SDKs use, the scoped key program API, and the API description.
NOT_OPERATOR = {
    ('POST', '/phaxio-callback'): 'webhook: Phaxio status callback',
    ('POST', '/signalwire-callback'): 'webhook: SignalWire status callback',
    ('POST', '/phaxio-inbound'): 'webhook: Phaxio received fax',
    ('POST', '/sinch-inbound'): 'webhook: Sinch received fax',
    ('POST', '/efax-inbound'): 'webhook: eFax notification that starts a check',
    ('POST', '/_internal/asterisk/inbound'): 'internal: the fax engine hands over a received fax',
    ('POST', '/_internal/freeswitch/outbound_result'): 'internal: FreeSWITCH reports a send result',
    ('POST', '/direct/deliveries'): 'partner protocol: a signed delivery from a partner installation',
    ('GET', '/direct/deliveries/{message_id}'): 'partner protocol: a signed status request',
    ('POST', '/direct/verifications'): 'partner protocol: a signed code confirmation',
    ('POST', '/mobile/pair'): 'device: the phone exchanges its pairing code',
    ('POST', '/fax'): 'public fax API for SDKs and integrations',
    ('GET', '/fax/{job_id}'): 'public fax API for SDKs and integrations',
    ('GET', '/fax/{job_id}/pdf'): 'public fax API: the provider fetches the document with a token',
    ('GET', '/inbound'): 'public fax API for SDKs and integrations',
    ('GET', '/inbound/{inbound_id}'): 'public fax API for SDKs and integrations',
    ('GET', '/inbound/{inbound_id}/pdf'): 'public fax API for SDKs and integrations',
    ('GET', '/health'): 'public liveness check',
    ('GET', '/health/ready'): 'public readiness check',
    ('GET', '/admin/api-keys'): 'program API for older clients and scripts; people use /access/keys',
    ('POST', '/admin/api-keys'): 'program API for older clients and scripts; people use /access/keys',
    ('DELETE', '/admin/api-keys/{key_id}'): 'program API for older clients and scripts; people use /access/keys',
    ('POST', '/admin/api-keys/{key_id}/rotate'): 'program API for older clients and scripts; people use /access/keys',
    ('POST', '/admin/diagnostics/run'): ('program API: every setting check for older clients and the console\'s '
                                         'bucket check; people read /admin/diagnostics/report'),
    ('GET', '/openapi.json'): 'API description',
    ('GET', '/docs'): 'API description',
    ('GET', '/docs/oauth2-redirect'): 'API description',
    ('GET', '/redoc'): 'API description',
}

# The console only, by design.
CONSOLE_ONLY = {
    ('GET', '/auth/context'): 'browser sign-in: what the signed-in person may see',
    ('GET', '/auth/setup'): 'browser sign-in: whether a first owner is still needed',
    ('POST', '/auth/login'): 'browser sign-in',
    ('POST', '/auth/key-login'): 'browser sign-in with a key',
    ('POST', '/auth/logout'): 'browser sign-out',
    ('POST', '/auth/password'): 'browser: change your own password',
    ('GET', '/auth/sessions'): 'browser: your own sessions (the command line uses /access/sessions)',
    ('POST', '/auth/sessions/{session_id}/revoke'): 'browser: end one of your own sessions',
    ('POST', '/admin/terminal/ticket'): 'the terminal is a browser feature',
    ('WS', '/admin/terminal'): 'the terminal is a browser feature',
}

# The command line only, by design.
CLI_ONLY: dict = {}

# Gaps still open in the console. Builder L removes each entry with the screen that closes it.
AWAITING_CONSOLE: dict = {}

# Gaps still open in the command line. Builder M removes each entry with the command that closes it.
AWAITING_CLI: dict = {}

LISTS = {'NOT_OPERATOR': NOT_OPERATOR, 'CONSOLE_ONLY': CONSOLE_ONLY, 'CLI_ONLY': CLI_ONLY,
         'AWAITING_CONSOLE': AWAITING_CONSOLE, 'AWAITING_CLI': AWAITING_CLI}


def _routes():
    """Every (method, path) the application serves, as test_route_policy_coverage reads them."""
    for route in iter_route_contexts(main.app.routes):
        if isinstance(route.original_route, Mount):
            continue
        if isinstance(route.original_route, APIWebSocketRoute):
            yield 'WS', route.path
            continue
        for method in sorted(set(route.methods or ()) - {'HEAD'}):
            yield method, route.path


# -- callers -------------------------------------------------------------------------

def _console_files():
    for path in sorted(CONSOLE_SOURCE.rglob('*')):
        relative = path.relative_to(CONSOLE_SOURCE)
        if (path.suffix in {'.ts', '.tsx'} and not {'__tests__', 'test', 'mocks'} & set(relative.parts)
                and not re.search(r'\.(test|spec)\.tsx?$', path.name)):
            yield path


def _template_text(source, start, quote):
    """The text of a string literal starting after ``quote`` at ``start``; ${...} becomes a hole."""
    text, position = [], start
    while position < len(source):
        character = source[position]
        if character == '\\':
            position += 2
            continue
        if character == quote:
            return ''.join(text)
        if character == '\n' and quote != '`':
            return None
        if quote == '`' and source.startswith('${', position):
            depth, position = 1, position + 2
            while position < len(source) and depth:
                depth += {'{': 1, '}': -1}.get(source[position], 0)
                position += 1
            text.append(HOLE)
            continue
        text.append(character)
        position += 1
    return None


def console_paths():
    """Every string or template literal in the console that starts with a path (or ${base}/path)."""
    found = set()
    for path in _console_files():
        source = path.read_text(encoding='utf-8')
        for match in re.finditer(r"""['"`](?=/|\$\{)""", source):
            text = _template_text(source, match.end(), match.group(0))
            if text is not None:
                found.add(text)
    return found


def _python_texts(node):
    """The strings an expression can build, with each value filled in at run time as a hole.

    Handles '/x/' + segment(id) + '/y', f-strings and ('/a/' if c else '/b/') + ...;
    None when the expression builds no string.
    """
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return {''.join(part.value if isinstance(part, ast.Constant) else HOLE for part in node.values)}
    if isinstance(node, ast.IfExp):
        body, orelse = _python_texts(node.body), _python_texts(node.orelse)
        return None if body is None and orelse is None else (body or set()) | (orelse or set())
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _python_texts(node.left), _python_texts(node.right)
        if left is None and right is None:
            return None
        return {first + second for first in (left or {HOLE}) for second in (right or {HOLE})}
    return None


def cli_paths():
    """Every string expression in the command line that builds a path, '/x/' + segment(id) included."""
    found = set()
    for path in sorted(CLI_SOURCE.rglob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            found.update(_python_texts(node) or ())
    return found


def _segments(text):
    """Path segments of a caller's text, or None when it is not a path."""
    text = re.sub(r'^[^/]*//[^/]*(?=/)', '', text)  # ${protocol}//${window.location.host}/admin/terminal
    if text.startswith(HOLE):  # ${this.baseURL}/auth/me
        text = text.lstrip(HOLE)
    text = re.split(r'[?#]', text, maxsplit=1)[0]
    if not text.startswith('/') or len(text) < 2:
        return None
    segments = []
    for part in text[1:].split('/'):
        if part and part.strip(HOLE) == '':
            segments.append(HOLE)
        elif HOLE in part:  # '/work' + query(...): the hole is a query, not a segment
            segments.append(part.split(HOLE, 1)[0])
            break
        else:
            segments.append(part)
    return tuple(segments)


def _matches(route_path, caller):
    expected = route_path.strip('/').split('/')
    if len(expected) != len(caller):
        return False
    for want, have in zip(expected, caller):
        if want.startswith('{') and want.endswith('}'):
            if not have:
                return False
        elif want != have:
            return False
    return True


def _called(route_path, callers):
    return any(_matches(route_path, caller) for caller in callers)


def _callers(texts):
    return {segments for segments in map(_segments, texts) if segments}


def _show(keys):
    return '\n'.join(f'  {method} {path}' for method, path in sorted(keys, key=lambda key: (key[1], key[0])))


# -- tests -----------------------------------------------------------------------------

def test_every_operator_route_has_a_console_caller_and_a_command():
    console, cli = _callers(console_paths()), _callers(cli_paths())
    operator = [key for key in _routes() if key not in NOT_OPERATOR]
    no_console = [key for key in operator
                  if key not in CLI_ONLY and key not in AWAITING_CONSOLE and not _called(key[1], console)]
    no_command = [key for key in operator
                  if key not in CONSOLE_ONLY and key not in AWAITING_CLI and not _called(key[1], cli)]
    assert not no_console, 'Operator routes with no caller in the console:\n' + _show(no_console)
    assert not no_command, 'Operator routes with no faxbot command:\n' + _show(no_command)


def test_gaps_listed_as_awaiting_are_still_open():
    console, cli = _callers(console_paths()), _callers(cli_paths())
    closed_console = [key for key in AWAITING_CONSOLE if _called(key[1], console)]
    closed_cli = [key for key in AWAITING_CLI if _called(key[1], cli)]
    assert not closed_console, 'The console now calls these; remove them from AWAITING_CONSOLE:\n' + _show(closed_console)
    assert not closed_cli, 'The command line now calls these; remove them from AWAITING_CLI:\n' + _show(closed_cli)


def test_every_listed_route_exists_once_with_a_reason():
    routes = set(_routes())
    for name, entries in LISTS.items():
        stale = [key for key in entries if key not in routes]
        assert not stale, f'{name} names routes that no longer exist:\n' + _show(stale)
        assert all(isinstance(reason, str) and reason.strip() for reason in entries.values()), name
    # A route may wait for both surfaces at once; otherwise each route has one place.
    exclusive = [NOT_OPERATOR, CONSOLE_ONLY, CLI_ONLY]
    for index, first in enumerate(exclusive):
        for second in exclusive[index + 1:] + [AWAITING_CONSOLE, AWAITING_CLI]:
            assert not set(first) & set(second), _show(set(first) & set(second))
    assert not set(CONSOLE_ONLY) & set(AWAITING_CONSOLE) and not set(CLI_ONLY) & set(AWAITING_CLI)


def test_paths_are_matched_by_segment():
    callers = _callers({'/work' + HOLE, '/work/' + HOLE + '/assign', HOLE + '/auth/me',
                        '/routing/published-plans?provider_id=' + HOLE, '/inbound/' + HOLE + '/pdf?token=' + HOLE})
    assert _called('/work', callers) and _called('/work/{item_id}/assign', callers)
    assert _called('/auth/me', callers) and _called('/routing/published-plans', callers)
    assert _called('/inbound/{inbound_id}/pdf', callers)
    # A value filled in at run time never stands for a fixed segment.
    assert not _called('/work/counts', _callers({'/work/' + HOLE}))
    assert not _called('/work/{item_id}', _callers({'/work/'}))
    built = _python_texts(ast.parse("('/routing/inbound/' if r else '/routing/faxes/') + segment(x) + '/cost'",
                                    mode='eval').body)
    assert built == {'/routing/inbound/' + HOLE + '/cost', '/routing/faxes/' + HOLE + '/cost'}
    assert _python_texts(ast.parse("f'/work/{segment(i)}/done'", mode='eval').body) == {'/work/' + HOLE + '/done'}
    assert _callers({HOLE + '//' + HOLE + '/admin/terminal'}) == {('admin', 'terminal')}
