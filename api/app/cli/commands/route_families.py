"""Route problems, the 2-by-2 test and shared upstreams: ``faxbot system diagnostics routes`` (brief 92, RF).

Hangs off the diagnostics group (``settings.diagnostics``); ``settings.py`` imports this module so the commands
exist before ``nouns.py`` copies the group. Only the 2-by-2's ``send`` places a call, and only for the one test fax
you name.
"""
import typer

from .. import state
from ..client import segment
from ..errors import CliError
from .settings import diagnostics


routes = typer.Typer(help='Route problems: failures that belong to one of your sending routes rather than to the '
                          'numbers it called, and the 2-by-2 test that tells them apart.', no_args_is_help=True)
diagnostics.add_typer(routes, name='routes')

CELL_HELP = 'a1, a2, b1 or b2: the first or second account (a, b) to your first or second number (1, 2).'
STATE_WORDS = {'not_sent': 'Not sent', 'pending': 'Sending', 'success': 'Went through', 'failed': 'Failed'}


def _short(identity):
    return (identity or '')[:8]


def _find(items, prefix, what):
    prefix = (prefix or '').strip().lower()
    found = [item for item in items if item['id'].startswith(prefix)] if prefix else []
    if len(found) != 1:
        raise CliError(f'No single {what} starts with "{prefix}". Run faxbot system diagnostics routes list to see '
                       'them.')
    return found[0]


def _test_lines(out, test):
    out.line(f"Test {_short(test['id'])}: {test['route_a_label']} and {test['route_b_label']} to "
             f"{test['number_a']} and {test['number_b']}, started {test['created_text']}.")
    out.table(['Cell', 'Account', 'To', 'Result'],
              [[cell['cell'], cell['account_label'], cell['number'],
                STATE_WORDS.get(cell['state'], cell['state'])] for cell in test['cells']])
    out.line(test['sentence'])


@routes.command('list')
def routes_list():
    """Route problems Faxbot found (open ones first), recent 2-by-2 tests, and known shared upstreams."""
    result = state.api().get('/routing/families')

    def human(out):
        problems = result.get('incidents') or []
        if not problems:
            out.line('Faxbot has found no problem shared by a whole sending route.')
        for item in problems:
            status = 'Open' if item['open'] else 'Ended'
            out.line(f"{status} ({_short(item['id'])}): {item['sentence']}")
            if item.get('advice'):
                out.line(f"  {item['advice']} Start one with: faxbot system diagnostics routes test")
        for test in result.get('tests') or []:
            out.line('')
            _test_lines(out, test)
        upstreams = result.get('upstreams') or []
        if upstreams:
            out.line('')
            out.table(['Provider', 'Known upstream', 'Source', 'Read on'],
                      [[row['provider'], row['upstream'], row['source_url'] or '-', row['source_day'] or '-']
                       for row in upstreams])
    state.out().result(result, human)


@routes.command('close')
def routes_close(problem: str = typer.Argument(..., metavar='PROBLEM',
                                               help='The first characters of the problem, from routes list.')):
    """Close a route problem by hand, for example after you fixed the trunk; Faxbot sets aside what it taught."""
    api = state.api()
    item = _find(api.get('/routing/families').get('incidents') or [], problem, 'route problem')
    result = api.post('/routing/families/' + segment(item['id']) + '/close')
    state.out().result(result, lambda out: out.line(result['sentence']))


@routes.command('test')
def routes_test(route_a: str = typer.Option(..., '--route-a', metavar='ACCOUNT',
                                            help='The first sending account, such as sip or sinch.'),
                route_b: str = typer.Option(..., '--route-b', metavar='ACCOUNT', help='The second sending account.'),
                number_a: str = typer.Option(..., '--number-a', metavar='NUMBER',
                                             help='Your first own receiving number.'),
                number_b: str = typer.Option(..., '--number-b', metavar='NUMBER',
                                             help='Your second own receiving number.')):
    """Plan a 2-by-2 test: two of your sending accounts against two of your own numbers. Nothing is sent yet."""
    result = state.api().post('/routing/families/tests', json={
        'route_a': route_a.strip(), 'route_b': route_b.strip(), 'number_a': number_a.strip(),
        'number_b': number_b.strip()})

    def human(out):
        _test_lines(out, result)
        out.line(f"Send each test fax when you are ready: faxbot system diagnostics routes send "
                 f"{_short(result['id'])} a1")
    state.out().result(result, human)


def _test(api, prefix):
    return _find(api.get('/routing/families').get('tests') or [], prefix, 'test')


@routes.command('send')
def routes_send(test: str = typer.Argument(..., metavar='TEST', help='The first characters of the test.'),
                cell: str = typer.Argument(..., metavar='CELL', help=CELL_HELP)):
    """Send one test fax of a 2-by-2 test. This places one real call; each test fax is sent once."""
    api = state.api()
    found = _test(api, test)
    cell = cell.strip().lower()
    if cell not in ('a1', 'a2', 'b1', 'b2'):
        raise CliError('Choose a1, a2, b1 or b2.')
    result = api.post('/routing/families/tests/' + segment(found['id']) + '/send/' + cell)
    state.out().result(result, lambda out: out.line(result['sentence']))


@routes.command('show-test')
def routes_show_test(test: str = typer.Argument(..., metavar='TEST', help='The first characters of the test.')):
    """What a 2-by-2 test shows so far."""
    api = state.api()
    found = _test(api, test)
    result = api.get('/routing/families/tests/' + segment(found['id']))
    state.out().result(result, lambda out: _test_lines(out, result))


@routes.command('upstream')
def routes_upstream(provider: str = typer.Argument(..., metavar='PROVIDER', help='The provider, such as sinch.'),
                    upstream: str = typer.Option(None, '--upstream', metavar='CARRIER',
                                                 help='The carrier the provider is known to use upstream.'),
                    source: str = typer.Option(None, '--source', metavar='WEB_ADDRESS',
                                               help='Where that is published (required with --upstream).'),
                    read_on: str = typer.Option(None, '--read-on', metavar='DATE',
                                                help='The day you read the source, such as 2026-10-10.'),
                    unknown: bool = typer.Option(False, '--unknown',
                                                 help='Faxbot no longer knows what this provider uses upstream.')):
    """Record which carrier a provider uses upstream, so Faxbot can say when two routes share a path."""
    if bool(upstream) == bool(unknown):
        raise CliError('Give --upstream with --source, or --unknown.')
    result = state.api().put('/routing/upstreams/' + segment(provider.strip().lower()), json={
        'upstream': None if unknown else upstream, 'source_url': source, 'source_date': read_on})

    def human(out):
        if result.get('upstream'):
            out.line(f"{result['provider']} uses {result['upstream']} upstream"
                     + (f", read on {result['source_day']}" if result.get('source_day') else '') + '.')
        else:
            out.line(f"Faxbot no longer says what {result['provider']} uses upstream.")
    state.out().result(result, human)


# Receiving readiness and the UPS (readiness.py) hang off the same diagnostics group.
from . import readiness as _readiness  # noqa: E402,F401
