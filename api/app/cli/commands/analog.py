"""faxbot delivery providers trunk analog-line: the business line you already pay for, through an analog gateway, as a route
whose local calls cost nothing more (routing/analog.py)."""
from pathlib import Path

import typer

from .. import state
from ..errors import CliError
from ..client import segment

analog = typer.Typer(help='An analog phone line through a gateway: its local calling area and its prices, so local '
                          'numbers go out on it at no extra cost.', no_args_is_help=True)
ACCOUNT = typer.Option('sip', '--account', metavar='KEY',
                       help="Which trunk, by its key from 'faxbot delivery providers accounts list'; the first trunk when left "
                            'out.')


def _lines(out, view):
    if not view.get('analog'):
        out.line('This trunk is not an analog line. Choose an analog line gateway as its carrier first '
                 "(faxbot delivery providers trunk presets).")
        return
    out.fields([('Trunk', view.get('label')), ('Gateway', view.get('preset_label')),
                ('Calls at once', view.get('calls_at_once')), ('Local prefixes', view.get('local_prefixes')),
                ('Other numbers cost', view.get('toll_rate')), ('Monthly fee', view.get('monthly_fee')),
                ('List from', ', '.join(view.get('sources') or []) or None)])
    out.line(view.get('saved') or view['sentence'])
    if view.get('route_sentence'):
        out.line(view['route_sentence'])


@analog.command('show')
def analog_show(account: str = ACCOUNT):
    """Show an analog line's local calling area and prices."""
    view = state.api().get('/routing/analog-lines/' + segment(account))
    state.out().result(view, lambda out: _lines(out, view))


@analog.command('routing')
def analog_routing(state_word: str = typer.Argument(..., metavar='on|off',
                                                    help='on: Faxbot may choose the line by itself; off: only a '
                                                         'sending rule that names it uses it.'),
                   account: str = ACCOUNT):
    """Let Faxbot choose an analog line by itself, or stop it. Importing a local calling area turns this on."""
    word = state_word.strip().lower()
    if word not in ('on', 'off'):
        raise CliError('Use on or off.')
    view = state.api().put('/routing/analog-lines/' + segment(account) + '/routing', json={'on': word == 'on'})
    state.out().result(view, lambda out: _lines(out, view))


@analog.command('import')
def analog_import(path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True, metavar='FILE',
                                             help="The list of local prefixes for your line: the Local Calling "
                                                  "Guide's Local prefixes page saved as HTML, its XML, a CSV with NPA "
                                                  'and NXX columns, or one prefix a line such as 303-426.'),
                  account: str = ACCOUNT,
                  plan: str = typer.Option(None, '--plan', help='The calling plan your line has, when the list names '
                                                                'several.'),
                  line: str = typer.Option(None, '--line', metavar='NPA-NXX',
                                           help="Your line's own prefix or number, checked against the list."),
                  toll_rate: str = typer.Option(None, '--toll-rate', metavar='AMOUNT',
                                                help='What the line charges a minute for calls outside the local '
                                                     'area, from your phone bill; 0 when your plan includes them. '
                                                     'Needed the first time.'),
                  monthly_fee: str = typer.Option(None, '--monthly-fee', metavar='AMOUNT',
                                                  help="The line's monthly fee, shown with its prices."),
                  increment: int = typer.Option(60, '--increment', min=1, max=3600,
                                                help='The billing step for calls outside the local area, in seconds.'),
                  source: str = typer.Option(None, '--source', metavar='URL',
                                             help='The address of the page the list came from.')):
    """Import an analog line's local calling area from a file you saved, so local numbers go out on it at no extra cost. Faxbot never looks the area up itself."""
    data = path.read_bytes()
    if len(data) > 4_000_000:
        raise CliError('This file is larger than 4 MB. Save only the list of local prefixes for your line.')
    body = {'text': data.decode('utf-8', errors='replace'), 'filename': path.name, 'plan': plan, 'line': line,
            'toll_per_minute': toll_rate, 'monthly_fee': monthly_fee, 'increment': increment, 'source_url': source}
    view = state.api().put('/routing/analog-lines/' + segment(account) + '/local-calls',
                           json={key: value for key, value in body.items() if value is not None})
    state.out().result(view, lambda out: _lines(out, view))
