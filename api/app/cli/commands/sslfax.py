"""The fast fax service (SSL Fax engine) on the command line: one fax machine's own limits."""
import typer

from .. import state
from ..client import segment
from ..errors import CliError

RATES = ('14400', '9600', '7200', '4800')


def limits_fields(view):
    """The rows 'faxbot recipients show' and 'faxbot recipients limits' print for one number."""
    # The sentence the console shows when Faxbot knows; otherwise not known yet.
    return [('Faster pages', view.get('sslfax_sentence') or 'not known yet'),
            ('Highest speed', f"{view['max_rate']} bits per second" if view.get('max_rate') else 'as set for all faxes'),
            ('Error correction', 'as set for all faxes' if view.get('ecm') is None else 'on' if view['ecm'] else 'off')]


def recipient_limits(number: str = typer.Argument(..., help='Fax number.'),
                     speed: str = typer.Option(None, '--speed', metavar='RATE',
                                               help='Highest speed for this number: 14400, 9600, 7200, 4800, '
                                                    'or default for the setting all faxes use.'),
                     ecm: str = typer.Option(None, '--error-correction', metavar='ON|OFF',
                                             help='Error correction for this number: on, off, or default.')):
    """Show or set the highest speed and error correction for one fax machine that keeps failing."""
    api = state.api()
    path = '/routing/destinations/' + segment(number) + '/fax-limits'
    view = api.get(path)
    if speed is not None or ecm is not None:
        body = {'max_rate': view.get('max_rate'), 'ecm': view.get('ecm')}
        if speed is not None:
            if speed != 'default' and speed not in RATES:
                raise CliError('Choose a speed of 14400, 9600, 7200 or 4800, or default.')
            body['max_rate'] = None if speed == 'default' else int(speed)
        if ecm is not None:
            if ecm not in ('on', 'off', 'default'):
                raise CliError('Choose on, off or default for error correction.')
            body['ecm'] = None if ecm == 'default' else ecm == 'on'
        view = api.put(path, json=body)

    def human(out):
        out.fields([('Fax number', view['number']), *limits_fields(view)])
    state.out().result(view, human)


def tuning_fields(view):
    """The rows 'faxbot recipients tuning' prints for one number."""
    smaller = ('off for this number' if view.get('tune') is False
               else 'on' if view.get('setting') else 'off in your fax settings')
    rows = [('Smaller pages', smaller), ('Smallest page format', view.get('jbig_sentence') or '')]
    if view.get('reasons'):
        rows.append(('Why', ' '.join(view['reasons'])))
    return rows


def recipient_tuning(number: str = typer.Argument(..., help='Fax number.'),
                     smaller: str = typer.Option(None, '--smaller', metavar='DEFAULT|OFF',
                                                 help='Smaller pages for this number: default (as set for all '
                                                      'faxes) or off.'),
                     smallest: str = typer.Option(None, '--smallest-format', metavar='ON|OFF',
                                                  help='Also send this number the smallest page format. Turn this '
                                                       'on only if its fax machine prints faxes from Faxbot '
                                                       'correctly.')):
    """Show or set whether Faxbot makes pages smaller for one number, without changing what it prints."""
    api = state.api()
    path = '/routing/destinations/' + segment(number) + '/coding-tuning'
    view = api.get(path)
    if smaller is not None or smallest is not None:
        body = {'tune': view.get('tune'), 'tune_jbig': bool(view.get('tune_jbig'))}
        if smaller is not None:
            if smaller not in ('default', 'off'):
                raise CliError('Choose default or off for smaller pages.')
            body['tune'] = None if smaller == 'default' else False
        if smallest is not None:
            if smallest not in ('on', 'off'):
                raise CliError('Choose on or off for the smallest page format.')
            body['tune_jbig'] = smallest == 'on'
        view = api.put(path, json=body)

    def human(out):
        out.fields([('Fax number', view['number']), *tuning_fields(view)])
        if view.get('tune_jbig'):
            out.line(view.get('warning') or '')
    state.out().result(view, human)
