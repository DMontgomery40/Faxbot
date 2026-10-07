"""Dense pages on the command line: one number's pages per sheet and blank space, and long pages per route."""
import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time

PAGES_PER_SHEET = {'machine': 'allow', 'never': 'never'}
SWITCH = {'on': True, 'off': False, 'default': None}


def page_fields(view):
    """The rows 'faxbot recipients show' prints for one number's pages."""
    learned = view.get('learned_at')
    capability = view.get('capability_sentence') or 'not known yet'
    if learned:
        capability += f' Learned {local_time(learned)}.'
    own = view.get('trim_blank')
    trim = ('as set for all faxes (' + ('on' if view.get('trim_blank_default') else 'off') + ')' if own is None
            else 'on for machines without error correction' if own else 'off')
    return [('Longest page', capability),
            ('This machine', view.get('ecm_sentence') or 'error correction not known yet'),
            ('Pages per sheet', 'as the receiving machine allows' if view.get('packing') != 'never' else 'never'),
            ('Blank space at the bottom of pages', trim)]


def recipient_page_body(pages_per_sheet, blank_space):
    """The body for PUT /routing/destinations/{number}/pages, or {} when neither option was given."""
    body = {}
    if pages_per_sheet is not None:
        if pages_per_sheet not in PAGES_PER_SHEET:
            raise CliError("Choose machine (as the receiving machine allows) or never for --pages-per-sheet.")
        body['packing'] = PAGES_PER_SHEET[pages_per_sheet]
    if blank_space is not None:
        if blank_space not in SWITCH:
            raise CliError('Choose on, off or default for --blank-space.')
        body['trim_blank'] = SWITCH[blank_space]
    return body


def providers_long_pages(route: str = typer.Argument(None, help='Route to change, as listed: sip, sinch, documo, '
                                                                'humblefax, efax, phaxio or signalwire.'),
                         long_pages: str = typer.Option(None, '--long-pages', metavar='ON|OFF|DEFAULT',
                                                        help='Several pages on one long page on this route: on, off, '
                                                             'or default.'),
                         blank_space: str = typer.Option(None, '--blank-space', metavar='ON|OFF|DEFAULT',
                                                         help='For all faxes on your phone line (route sip): leave out '
                                                              'the blank bottom of pages for machines without error '
                                                              'correction. On, off, or default.')):
    """Show or set long pages for each delivery route, and blank space at the bottom of pages."""
    api = state.api()
    if long_pages is None and blank_space is None:
        result = api.get('/routing/page-routes')
        routes = [item for item in result.get('routes', []) if route is None or item['route'] == route]

        def human(out):
            out.table(['Route', 'Name', 'Long pages', 'Blank space left out', 'What happens'],
                      [[item['route'], item['label'], 'on' if item['long_pages'] else 'off',
                        '-' if item.get('trim_blank') is None else 'on' if item['trim_blank'] else 'off',
                        item['sentence']] for item in routes], empty='No such route.')
        state.out().result({'routes': routes}, human)
        return
    if route is None:
        raise CliError('Name the route to change, for example: faxbot providers long-pages sinch --long-pages on')
    body = {}
    for name, value in (('long_pages', long_pages), ('trim_blank', blank_space)):
        if value is not None:
            if value not in SWITCH:
                raise CliError('Choose on, off or default.')
            body[name] = SWITCH[value]
    view = api.put('/routing/page-routes/' + segment(route), json=body)
    state.out().result(view, lambda out: out.line(view['sentence']))
