"""The fast fax service (SSL Fax engine) on the command line: one fax machine's own limits."""
import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time

RATES = ('14400', '9600', '7200', '4800')


def limits_fields(view):
    """The rows 'faxbot recipients show' and 'faxbot recipients limits' print for one number."""
    accepts = view.get('accepts_sslfax')
    seen = local_time(view.get('accepts_sslfax_at')) if view.get('accepts_sslfax_at') else None
    takes = None if accepts is None else ('yes' if accepts else 'no') + (f' (seen {seen})' if seen else '')
    return [('Faster pages', takes or 'not known yet'),
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
        if view.get('sslfax_sentence'):
            out.line(view['sslfax_sentence'])
    state.out().result(view, human)
