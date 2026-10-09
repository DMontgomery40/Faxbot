"""Collecting faxes by polling, on the command line (M21, api/app/routing/polling.py)."""
import typer

from .. import state
from ..client import segment


def polling_fields(view):
    """The rows 'faxbot recipients polling' prints for one number."""
    return [('Fax number', view['number']), ('Collecting', 'On' if view['enabled'] else 'Off'),
            ('Other site', view.get('label')), ('Selective polling address', view.get('selective')),
            ('What it would cost', view.get('advice')), ('Before you turn it on', view.get('note'))]


def _human(view):
    def human(out):
        out.fields(polling_fields(view))
        if view.get('requests'):
            out.table(['Asked', 'By', 'Result', 'What happened'],
                      [[item['requested'], item.get('requested_by') or '', item['state'], item['sentence']]
                       for item in view['requests']], title='Recent collections')
    return human


def recipient_polling(number: str = typer.Argument(..., help='Fax number of the other site.'),
                      collecting: bool = typer.Option(None, '--on/--off',
                                                      help='Allow or stop collecting faxes from this number. '
                                                           'Faxbot never collects by itself.'),
                      name: str = typer.Option(None, '--name', metavar='NAME',
                                               help='A name for the other site, such as "Denver office".'),
                      selective: str = typer.Option(None, '--selective-address', metavar='DIGITS',
                                                    help='The address the other fax server asks callers to give '
                                                         'before it sends a held fax, if it asks for one.')):
    """Show or set whether Faxbot may collect faxes from another site's fax server by calling it."""
    api = state.api()
    path = '/routing/destinations/' + segment(number) + '/polling'
    view = api.get(path)
    if any(value is not None for value in (collecting, name, selective)):
        view = api.put(path, json={'enabled': view['enabled'] if collecting is None else collecting,
                                   'label': view.get('label') if name is None else name,
                                   'selective': view.get('selective') if selective is None else selective})
    state.out().result(view, _human(view))


def recipient_collect(number: str = typer.Argument(..., help='Fax number of the other site.')):
    """Call another site's fax server once and collect the fax it holds for you (it arrives in Received)."""
    api = state.api()
    view = api.post('/routing/destinations/' + segment(number) + '/polling/collect')
    state.out().result(view, lambda out: (out.line(view['sentence']), _human(view)(out)))
