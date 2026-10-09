"""Collecting faxes by polling, and holding faxes for another site to collect, on the command line (M21,
api/app/routing/polling.py)."""
from pathlib import Path

import typer

from .. import state
from ..client import CliError, segment


def polling_fields(view):
    """The rows 'faxbot recipients polling' prints for one number."""
    return [('Fax number', view['number']), ('Collecting', 'On' if view['enabled'] else 'Off'),
            ('Other site', view.get('label')), ('Selective polling address', view.get('selective')),
            ('Polling password', 'Set' if view.get('has_password') else 'None'),
            ('Timetable', view.get('timetable') or 'None: Faxbot collects only when you ask'),
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
                                                         'before it sends a held fax, if it asks for one.'),
                      password: str = typer.Option(None, '--password', metavar='DIGITS',
                                                   help='The polling password the other fax server asks for; '
                                                        'kept sealed, never shown. Give "" to clear it.'),
                      collect_at: str = typer.Option(None, '--collect-at', metavar='TIMES',
                                                     help='Times of day to collect by themselves, such as '
                                                          '08:00,16:00. Needs --collect-days.'),
                      collect_days: str = typer.Option(None, '--collect-days', metavar='DAYS',
                                                       help='Days to collect by themselves, such as '
                                                            'mon,tue,wed,thu,fri.'),
                      time_zone: str = typer.Option(None, '--time-zone', metavar='ZONE',
                                                    help="The other site's time zone for the timetable, such as "
                                                         'America/Denver.'),
                      no_timetable: bool = typer.Option(False, '--no-timetable',
                                                        help='Clear the timetable: Faxbot collects only when you '
                                                             'ask.')):
    """Show or set whether Faxbot may collect faxes from another site's fax server by calling it, the password it
    sends, and a timetable for collecting by itself."""
    api = state.api()
    path = '/routing/destinations/' + segment(number) + '/polling'
    view = api.get(path)
    changes = (collecting, name, selective, password, collect_at, collect_days, time_zone)
    if any(value is not None for value in changes) or no_timetable:
        if no_timetable and (collect_at or collect_days):
            raise CliError('Use --no-timetable on its own, or give --collect-at and --collect-days.')
        body = {'enabled': view['enabled'] if collecting is None else collecting,
                'label': view.get('label') if name is None else name,
                'selective': view.get('selective') if selective is None else selective,
                'password': password, 'collect_times': collect_at, 'collect_days': collect_days,
                'time_zone': time_zone}
        if no_timetable:
            body.update(collect_times='', collect_days='', time_zone='')
        view = api.put(path, json=body)
    state.out().result(view, _human(view))


def recipient_collect(number: str = typer.Argument(..., help='Fax number of the other site.')):
    """Call another site's fax server once and collect the fax it holds for you (it arrives in Received)."""
    api = state.api()
    view = api.post('/routing/destinations/' + segment(number) + '/polling/collect')
    state.out().result(view, lambda out: (out.line(view['sentence']), _human(view)(out)))


# Holding faxes for the other site to collect ----------------------------------------------------------------

def hold_fields(view):
    return [('Fax number', view['number']), ('Collects from Faxbot', 'On' if view['enabled'] else 'Off'),
            ('Other site', view.get('label')), ('Selective polling address it must give', view.get('selective')),
            ('Polling password it must give', 'Set' if view.get('has_password') else 'None'),
            ('Before you turn it on', view.get('note'))]


def _held_human(view):
    def human(out):
        out.fields(hold_fields(view))
        if view.get('held'):
            out.table(['Held', 'By', 'Pages', 'Document', 'State', 'What happened'],
                      [[item['held'], item.get('held_by') or '', str(item['pages']), item.get('name') or '',
                        item['state'], item['sentence']] for item in view['held']], title='Faxes held for it')
    return human


def recipient_hold(number: str = typer.Argument(..., help='Fax number of the other site.'),
                   holding: bool = typer.Option(None, '--on/--off',
                                                help='Allow or stop this number collecting faxes from Faxbot.'),
                   name: str = typer.Option(None, '--name', metavar='NAME',
                                            help='A name for the other site, such as "Denver office".'),
                   selective: str = typer.Option(None, '--selective-address', metavar='DIGITS',
                                                 help='The address the other site must give to get its faxes; '
                                                      'leave empty for none.'),
                   password: str = typer.Option(None, '--password', metavar='DIGITS',
                                                help='The polling password the other site must give; kept sealed, '
                                                     'never shown. Give "" to clear it.')):
    """Show or set whether another site's fax server may call Faxbot and collect the faxes held for it, and
    what it must give to get them."""
    api = state.api()
    path = '/routing/destinations/' + segment(number) + '/polling/hold'
    view = api.get(path)
    if any(value is not None for value in (holding, name, selective, password)):
        view = api.put(path, json={'enabled': view['enabled'] if holding is None else holding,
                                   'label': view.get('label') if name is None else name,
                                   'selective': view.get('selective') if selective is None else selective,
                                   'password': password})
    state.out().result(view, _held_human(view))


def recipient_hold_fax(number: str = typer.Argument(..., help='Fax number of the other site.'),
                       document: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                                       help='The PDF or fax TIFF to hold for it.')):
    """Hold a document for another site to collect: it goes out when that site calls Faxbot and asks for it."""
    api = state.api()
    with document.open('rb') as handle:
        view = api.post('/routing/destinations/' + segment(number) + '/polling/hold/faxes',
                        files={'file': (document.name, handle, 'application/octet-stream')})
    state.out().result(view, lambda out: (out.line(view['sentence']), _held_human(view)(out)))


def recipient_held(number: str = typer.Argument(..., help='Fax number of the other site.')):
    """List the faxes held for another site to collect, and what happened to each."""
    api = state.api()
    view = api.get('/routing/destinations/' + segment(number) + '/polling/hold')
    state.out().result(view, _held_human(view))


def recipient_withdraw(number: str = typer.Argument(..., help='Fax number of the other site.'),
                       held_id: str = typer.Argument(..., metavar='HELD-FAX', help='The held fax, from '
                                                                                   "'faxbot recipients held'.")):
    """Take a held fax back so the other site can no longer collect it."""
    api = state.api()
    view = api.delete('/routing/destinations/' + segment(number) + '/polling/hold/faxes/' + segment(held_id))
    state.out().result(view, lambda out: (out.line(f"Outcome: {view['outcome']}."), _held_human(view)(out)))
