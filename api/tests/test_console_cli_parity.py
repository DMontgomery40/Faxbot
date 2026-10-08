"""Every operator route has a caller in the console and a command in the command line.

Routes come from the application table, as in test_route_policy_coverage.py. A
route counts as called when a path in the command line (api/app/cli) matches
it, or when a console screen requests it: a path written in a screen file, or
a path in an API client method that a screen calls (api/admin_ui/src outside
api/, tests and mocks excluded). The API client and the type files alone never
count, so a client method no screen uses leaves its route without a console
caller. Paths match segment by segment; a value filled in at run time matches
a {parameter} segment only. Matching is by path, so every method on a called
path counts.

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
    ('POST', '/phaxio-inbound/{key}'): 'webhook: Phaxio received fax, for one more Phaxio account',
    ('POST', '/sinch-inbound/{key}'): 'webhook: Sinch received fax, for one more Sinch account',
    ('POST', '/efax-inbound/{key}'): 'webhook: eFax notification that starts a check, for one more eFax account',
    ('POST', '/_internal/asterisk/inbound'): 'internal: the fax engine hands over a received fax',
    ('POST', '/_internal/freeswitch/outbound_result'): 'internal: FreeSWITCH reports a send result',
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
                                             'faxbot providers show reads /admin/settings'),
    ('PUT', '/plugins/{plugin_id}/config'): ('the Provider plugins page, off by default and retired next release; '
                                             'faxbot providers configure writes /admin/settings'),
}

# The command line only, by design.
CLI_ONLY = {
    ('GET', '/routing/quote'): (
        'one fax priced by each account your rules allow, for faxbot costs fax --to; the console shows the same '
        'price for each allowed account in Providers -> Rules -> Try a fax (POST /routing/explain)'),
    ('GET', '/routing/inbound/{inbound_id}/cost'): (
        "one received fax's cost for faxbot costs received <id>; the console reads the costs of the received faxes "
        'on screen in one request (GET /routing/inbound-costs)'),
    ('GET', '/routing/published-plans'): (
        "any provider's published plans for faxbot costs plans <provider>; the console shows the plans of the "
        'providers in use (GET /routing/published-plans/in-use)'),
    ('GET', '/case-checklists/{name}'): (
        'one checklist, or one of its earlier versions, for faxbot recipients cases checklist show and build; the '
        'console reads every checklist with its items in one request (GET /case-checklists)'),
}

# Gaps still open in the console. Builder L removes each entry with the screen that closes it.
AWAITING_CONSOLE: dict = {
}

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


def _literal_paths(source):
    """Every string or template literal in ``source`` that starts with a path (or ${base}/path)."""
    found = set()
    for match in re.finditer(r"""['"`](?=/|\$\{)""", source):
        text = _template_text(source, match.end(), match.group(0))
        if text is not None:
            found.add(text)
    return found


def screen_files():
    """Console files that people see or that screens use (components, hooks, the shell), never the API layer.

    ``api/`` holds the client and the response types: a route counts only when a
    screen calls the client method that requests it, not because the client has one.
    """
    return [path for path in _console_files() if path.relative_to(CONSOLE_SOURCE).parts[0] != 'api']


CLIENT = CONSOLE_SOURCE / 'api' / 'client.ts'
# A member of the AdminAPIClient class, at two spaces: "  async getSavings(", "  static async login(".
_MEMBER = re.compile(r'^  (?:(?:private|public|protected|static|async|readonly)\s+)*(?:get\s+|set\s+)?'
                     r'([A-Za-z_$][\w$]*)\s*(?:<[^>\n]*>)?\(', re.M)


def client_methods():
    """{AdminAPIClient method: paths it requests}, including paths of the client methods it calls itself."""
    source = CLIENT.read_text(encoding='utf-8')
    start = source.index('class AdminAPIClient')
    end = source.index('\n}\n', start)
    body = source[start:end]
    members = [(match.start(), match.group(1)) for match in _MEMBER.finditer(body)]
    texts = {}
    for index, (offset, name) in enumerate(members):
        stop = members[index + 1][0] if index + 1 < len(members) else len(body)
        texts[name] = texts.get(name, '') + body[offset:stop]
    paths = {name: _literal_paths(text) for name, text in texts.items()}
    calls = {name: set(re.findall(r'(?:this|AdminAPIClient)\.(\w+)\(', text)) & set(texts)
             for name, text in texts.items()}
    changed = True
    while changed:
        changed = False
        for name in paths:
            for other in calls[name]:
                if not paths[other] <= paths[name]:
                    paths[name] |= paths[other]
                    changed = True
    return paths


def console_paths():
    """Paths the console's screens request: literals in screen files, and the paths of client methods they call."""
    methods = client_methods()
    found = set()
    for path in screen_files():
        source = path.read_text(encoding='utf-8')
        found |= _literal_paths(source)
        for name in set(re.findall(r'\.(\w+)\(', source)) & set(methods):
            found |= methods[name]
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


def test_a_client_method_counts_only_when_a_screen_calls_it():
    """The API client is not a screen: a route its methods request but no screen calls has no console caller."""
    methods = client_methods()
    assert '/routing/inbound/' + HOLE + '/cost' in methods['getInboundCost']
    assert '/routing/savings' + HOLE in methods['getSavings']
    console = _callers(console_paths())
    assert not _called('/routing/inbound/{inbound_id}/cost', console)
    assert _called('/routing/savings', console) and _called('/routing/inbound-costs', console)
    assert not any(path.relative_to(CONSOLE_SOURCE).parts[0] == 'api' for path in screen_files())


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
