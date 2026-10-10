"""Every operator route has a caller in the console and a command in the command line.

Routes come from the application table, as in test_route_policy_coverage.py. A
route counts as called when a command line HTTP request (api/app/cli) matches
it, or when a console screen invokes that request directly or through an API
client or helper method (api/admin_ui/src outside
api/, tests and mocks excluded). The API client and the type files alone never
count, so a client method no screen uses leaves its route without a console
caller. Paths match segment by segment; a value filled in at run time matches
a {parameter} segment only. Coverage requires the same HTTP method and path; reading a list never
counts as creating, updating or deleting an entry.

Routes that are not for operators, or belong to one surface by design, are
listed with a reason. AWAITING_CONSOLE and AWAITING_CLI hold the gaps still
open: whoever closes one removes its entry in the same change (a listed route
that has gained its caller fails here), and the lead empties both lists
before the work merges.
"""
import ast
from functools import lru_cache
import json
import subprocess
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
    ('POST', '/phaxio-inbound/{key}'): 'webhook: Phaxio received fax, for one more Phaxio account',
    ('POST', '/sinch-inbound/{key}'): 'webhook: Sinch received fax, for one more Sinch account',
    ('POST', '/efax-inbound/{key}'): 'webhook: eFax notification that starts a check, for one more eFax account',
    ('POST', '/_internal/asterisk/inbound'): 'internal: the fax engine hands over a received fax',
    ('POST', '/_internal/freeswitch/outbound_result'): 'internal: FreeSWITCH reports a send result',
    ('POST', '/_internal/hylafax/polled'): 'internal: the fax engine reports collection of a held fax',
    ('POST', '/_internal/hylafax/result'): 'internal: the SSL Fax engine reports a send result',
    ('POST', '/_internal/hylafax/started'): 'internal: the SSL Fax engine reports that it started again',
    ('POST', '/_internal/hylafax/inbound'): 'internal: the SSL Fax engine hands over a received fax',
    ('POST', '/_internal/hylafax/received-failed'): 'internal: the SSL Fax engine reports a call that left no fax',
    ('POST', '/direct/deliveries'): 'partner protocol: a signed delivery from a partner installation',
    ('GET', '/direct/deliveries/{message_id}'): 'partner protocol: a signed status request',
    ('POST', '/direct/verifications'): 'partner protocol: a signed code confirmation',
    ('POST', '/direct/capabilities'): 'partner protocol: a signed statement of what the partner accepts',
    ('GET', '/forms/partner/holdings'): 'partner protocol: a signed question about which forms are held',
    ('GET', '/forms/partner/forms/{address}'): 'partner protocol: a signed fetch of one form by address',
    ('POST', '/direct/relay/statements'): 'partner protocol: a signed relay offer, acceptance, withdrawal, price or receipt',
    ('GET', '/direct/relay/outcomes/{message_id}'): 'partner protocol: a signed question about a relayed fax',
    ('POST', '/direct/introductions'): 'partner protocol: a signed introduction of another partner (a hint only)',
    ('GET', '/.well-known/faxbot-direct'): 'partner protocol: the public partner card that partners and directories read',
    ('POST', '/direct/transfers'): 'partner protocol: a signed preflight before a document is sent in pieces',
    ('PUT', '/direct/transfers/{message_id}/pieces/{sequence}'): 'partner protocol: one signed piece of a document',
    ('GET', '/direct/transfers/{message_id}'): 'partner protocol: a signed question about the pieces held',
    ('POST', '/direct/transfers/{message_id}/commit'): 'partner protocol: a signed commit of a document sent in pieces',
    ('POST', '/direct/notices'): 'partner protocol: a signed link between a notice fax and its original',
    ('POST', '/direct/notices/paired'): 'partner protocol: a signed statement that a notice fax was paired',
    ('POST', '/direct/distribution/statements'): 'partner protocol: a signed send-once offer, acceptance or withdrawal',
    ('POST', '/direct/distributions'): "partner protocol: a send's first document with the signed list of recipients",
    ('POST', '/direct/holdings'): 'partner protocol: a signed question about which documents are still held',
    ('POST', '/direct/references'): 'partner protocol: a signed manifest for a copy the partner already holds',
    ('POST', '/direct/patches'): 'partner protocol: the signed changes to an earlier version the partner holds',
    ('POST', '/direct/regions'): "partner protocol: a fax image's signed new header regions around a body it holds",
    ('POST', '/direct/calls/pages'): 'partner protocol: a signed question about the pages of a broken call',
    ('GET', '/digital/jwks/{key}'): "recipient's system: a FHIR client's public keys it reads to register the client",
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
    ('GET', '/plugins/{plugin_id}/config'): ('the Provider plugins page, off by default and retired next release; '
                                             'faxbot delivery providers show reads /admin/settings'),
    ('PUT', '/plugins/{plugin_id}/config'): ('the Provider plugins page, off by default and retired next release; '
                                             'faxbot delivery providers configure writes /admin/settings'),
}

# The command line only, by design.
CLI_ONLY = {
    ('GET', '/routing/quote'): (
        'one fax priced by each account your rules allow, for faxbot savings fax --to; the console shows the same '
        'price for each allowed account in Delivery setup -> Routing rules -> Try a fax (POST /routing/explain)'),
    ('GET', '/routing/inbound/{inbound_id}/cost'): (
        "one received fax's cost for faxbot savings received <id>; the console reads the costs of the received faxes "
        'on screen in one request (GET /routing/inbound-costs)'),
    ('GET', '/routing/published-plans'): (
        "any provider's published plans for faxbot savings plans <provider>; the console shows the plans of the "
        'providers in use (GET /routing/published-plans/in-use)'),
    ('GET', '/case-checklists/{name}'): (
        'one checklist, or one of its earlier versions, for faxbot faxes cases checklist show and build; the '
        'console reads every checklist with its items in one request (GET /case-checklists)'),
}

# Gaps still open in the console. Builder L removes each entry with the screen that closes it.
AWAITING_CONSOLE: dict = {}

# Gaps still open in the command line. Builder M removes each entry with the command that closes it.
AWAITING_CLI: dict = {
}

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


def screen_files():
    """Components, hooks and shell files; API declarations alone never count."""
    return [path for path in _console_files() if path.relative_to(CONSOLE_SOURCE).parts[0] != 'api']


@lru_cache(maxsize=1)
def _console_inventory():
    """Resolve invoked TypeScript methods and wrappers through the compiler API."""
    helper = Path(__file__).with_name('console_route_calls.cjs')
    result = subprocess.run(['node', str(helper), str(CONSOLE_SOURCE)], check=True,
                            capture_output=True, text=True, timeout=90)
    return json.loads(result.stdout)


def client_methods():
    return {name: {(call['method'], call['path']) for call in calls}
            for name, calls in _console_inventory()['methods'].items()}


def console_requests():
    return {(call['method'], call['path']) for call in _console_inventory()['calls']}


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


def python_requests(source):
    """HTTP calls, including paths in local variables and arguments passed to helpers.

    An unrelated string literal never establishes coverage. Resolve each name in
    its lexical scope and retain the method of the request that consumes it.
    """
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def scope(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return node
        return tree

    assignments = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and node.value is not None:
                    assignments.setdefault((scope(node), target.id), []).append(node.value)

    def texts(node, env, seen=frozenset()):
        if isinstance(node, ast.Name):
            key = (scope(node), node.id)
            if node.id in env:
                return env[node.id]
            if key in seen:
                return set()
            candidates = assignments.get(key, assignments.get((tree, node.id), []))
            return set().union(*(texts(value, env, seen | {key}) for value in candidates))
        if isinstance(node, ast.JoinedStr):
            result = {''}
            for part in node.values:
                options = texts(part.value, env, seen) if isinstance(part, ast.FormattedValue) else {part.value}
                result = {prefix + value for prefix in result for value in (options or {HOLE})}
            return result
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = texts(node.left, env, seen), texts(node.right, env, seen)
            return {a + b for a in (left or {HOLE}) for b in (right or {HOLE})}
        if isinstance(node, ast.IfExp):
            return texts(node.body, env, seen) | texts(node.orelse, env, seen)
        return _python_texts(node) or set()

    found = set()

    def invoke(node, env, stack):
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr.upper()
            receiver = node.func.value
            is_api = (isinstance(receiver, ast.Name) and receiver.id in {'api', 'client', 'self'} or
                      isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Attribute)
                      and receiver.func.attr == 'api')
            if is_api and node.args:
                if method == 'PAGES':
                    method = 'GET'
                if method in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}:
                    found.update((method, value) for value in texts(node.args[0], env))
                elif method == 'REQUEST' and len(node.args) >= 2:
                    found.update((verb, value) for verb in texts(node.args[0], env)
                                 for value in texts(node.args[1], env))
        if isinstance(node.func, ast.Name) and node.func.id in functions:
            function = functions[node.func.id]
            if function in stack:
                return
            bindings = dict(env)
            for parameter, argument in zip(function.args.args, node.args):
                bindings[parameter.arg] = texts(argument, env)
            for keyword in node.keywords:
                if keyword.arg:
                    bindings[keyword.arg] = texts(keyword.value, env)
            for child in ast.walk(function):
                if isinstance(child, ast.Call):
                    invoke(child, bindings, stack | {function})

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            invoke(node, {}, frozenset())
    return found


def cli_requests():
    found = set()
    for path in sorted(CLI_SOURCE.rglob('*.py')):
        found |= python_requests(path.read_text(encoding='utf-8'))
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


def _called(route, callers):
    method, route_path = route
    return any(method == verb and _matches(route_path, caller) for verb, caller in callers)


def _callers(requests):
    return {(method, segments) for method, text in requests if (segments := _segments(text))}


def _show(keys):
    return '\n'.join(f'  {method} {path}' for method, path in sorted(keys, key=lambda key: (key[1], key[0])))


# -- tests -----------------------------------------------------------------------------

def test_every_operator_route_has_a_console_caller_and_a_command():
    console, cli = _callers(console_requests()), _callers(cli_requests())
    operator = [key for key in _routes() if key not in NOT_OPERATOR]
    no_console = [key for key in operator
                  if key not in CLI_ONLY and key not in AWAITING_CONSOLE and not _called(key, console)]
    no_command = [key for key in operator
                  if key not in CONSOLE_ONLY and key not in AWAITING_CLI and not _called(key, cli)]
    assert not (no_console or no_command), ('Operator routes with no caller in the console:\n' + _show(no_console)
        + '\nOperator routes with no faxbot command:\n' + _show(no_command))


def test_gaps_listed_as_awaiting_are_still_open():
    console, cli = _callers(console_requests()), _callers(cli_requests())
    closed_console = [key for key in AWAITING_CONSOLE if _called(key, console)]
    closed_cli = [key for key in AWAITING_CLI if _called(key, cli)]
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


def test_a_client_method_counts_only_when_a_screen_calls_it():
    """The API client is not a screen: a route its methods request but no screen calls has no console caller."""
    methods = client_methods()
    assert ('GET', '/routing/inbound/' + HOLE + '/cost') in methods['getInboundCost']
    assert _called(('GET', '/routing/savings'), _callers(methods['getSavings']))
    console = _callers(console_requests())
    assert not _called(('GET', '/routing/inbound/{inbound_id}/cost'), console)
    assert _called(('GET', '/routing/savings'), console) and _called(('GET', '/routing/inbound-costs'), console)
    assert not any(path.relative_to(CONSOLE_SOURCE).parts[0] == 'api' for path in screen_files())


def test_paths_are_matched_by_segment():
    callers = _callers(('GET', value) for value in {'/work' + HOLE, '/work/' + HOLE + '/assign', HOLE + '/auth/me',
                        '/routing/published-plans?provider_id=' + HOLE, '/inbound/' + HOLE + '/pdf?token=' + HOLE})
    assert _called(('GET', '/work'), callers) and _called(('GET', '/work/{item_id}/assign'), callers)
    assert _called(('GET', '/auth/me'), callers) and _called(('GET', '/routing/published-plans'), callers)
    assert _called(('GET', '/inbound/{inbound_id}/pdf'), callers)
    # A value filled in at run time never stands for a fixed segment.
    assert not _called(('GET', '/work/counts'), _callers({('GET', '/work/' + HOLE)}))
    assert not _called(('GET', '/work/{item_id}'), _callers({('GET', '/work/')}))
    built = _python_texts(ast.parse("('/routing/inbound/' if r else '/routing/faxes/') + segment(x) + '/cost'",
                                    mode='eval').body)
    assert built == {'/routing/inbound/' + HOLE + '/cost', '/routing/faxes/' + HOLE + '/cost'}
    assert _python_texts(ast.parse("f'/work/{segment(i)}/done'", mode='eval').body) == {'/work/' + HOLE + '/done'}
    assert _callers({('WS', HOLE + '//' + HOLE + '/admin/terminal')}) == {('WS', ('admin', 'terminal'))}


def test_http_methods_are_not_interchangeable_and_bare_paths_do_not_count():
    requests = python_requests("""
def send(api, number):
    unused = '/only-mentioned'
    path = '/recipients/' + segment(number)
    api.get(path)
    api.post(path + '/confirm')
    api.pages('/users')
""")
    callers = _callers(requests)
    assert _called(('GET', '/recipients/{number}'), callers)
    assert not _called(('POST', '/recipients/{number}'), callers)
    assert _called(('POST', '/recipients/{number}/confirm'), callers)
    assert not _called(('GET', '/recipients/{number}/confirm'), callers)
    assert not _called(('GET', '/only-mentioned'), callers)
    assert _called(('GET', '/users'), callers)


def test_python_helpers_keep_the_method_and_resolve_path_arguments():
    requests = python_requests("""
def update(api, address):
    api.patch(address)
def command(api, number):
    update(api, f'/recipients/{segment(number)}')
""")
    assert ('PATCH', '/recipients/' + HOLE) in requests
    assert ('GET', '/recipients/' + HOLE) not in requests


def test_typescript_follows_calls_not_unused_api_methods_or_bare_paths(tmp_path):
    source = tmp_path / 'src'
    api = source / 'api'
    api.mkdir(parents=True)
    (api / 'client.ts').write_text("""
export default class AdminAPIClient {
  private json(path: string, options: {method?: string} = {}) { return Promise.resolve({}); }
  call(request: {method: string; path: string}) { return this.json(request.path, {method: request.method}); }
  read() { return this.json('/items'); }
  unusedCreate() { return this.json('/items', {method: 'POST'}); }
}
""")
    (source / 'helper.ts').write_text("""
import AdminAPIClient from './api/client';
export function helper(client: AdminAPIClient) {
  const post = (path: string) => client.call({method: 'POST', path});
  return {
    confirm: (id: string) => post(`/items/${id}/confirm`),
    unused: () => post('/unused-helper'),
  };
}
""")
    (source / 'Screen.tsx').write_text("""
import AdminAPIClient from './api/client';
import {helper} from './helper';
export function Screen(client: AdminAPIClient) {
  const unrelated = {unusedCreate: () => '/not-requested'};
  unrelated.unusedCreate();
  const documentation = {method: 'DELETE', path: '/items'};
  const call = (request: {method: string; path: string}) => request;
  call({method: 'PUT', path: '/items'});
  client.read();
  helper(client).confirm('synthetic');
}
""")
    result = subprocess.run(['node', str(Path(__file__).with_name('console_route_calls.cjs')), str(source)],
                            check=True, capture_output=True, text=True, timeout=90)
    calls = _callers((entry['method'], entry['path']) for entry in json.loads(result.stdout)['calls'])
    assert _called(('GET', '/items'), calls)
    assert _called(('POST', '/items/{id}/confirm'), calls)
    assert not _called(('POST', '/items'), calls)
    assert not _called(('DELETE', '/items'), calls)
    assert not _called(('PUT', '/items'), calls)
    assert not _called(('POST', '/unused-helper'), calls)
    assert not _called(('GET', '/not-requested'), calls)
