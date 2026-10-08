"""When Faxbot sends to one recipient, on the command line: its hours, and the busy hours and call hours Faxbot
learned."""
import typer

from .. import state
from ..client import segment
from ..errors import CliError

DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')


def schedule_fields(view):
    """The rows 'faxbot recipients schedule' prints for one number."""
    return [('Fax number', view['number']), ('Hours', view['hours_sentence']),
            ('Busy hours', view['busy_sentence']), ('Failed tries', (view.get('failed_try') or {}).get('sentence')),
            ('Call hours', view.get('call_hours_sentence')), ('Any hour', view.get('typical_hour'))]


def _human(view):
    def human(out):
        out.fields(schedule_fields(view))
        if view.get('busy_hours'):
            out.table(['Hours', 'What earlier calls showed'],
                      [[item['label'], item['sentence']] for item in view['busy_hours']],
                      title='Busy hours learned from the last 30 days')
        if view.get('call_hours'):
            out.table(['Hours', 'What earlier calls showed'],
                      [[item['label'], item['sentence']] for item in view['call_hours']],
                      title='Time a page and failed calls by hour, from the last 30 days')
    return human


def recipient_schedule(number: str = typer.Argument(..., help='Fax number.'),
                       days: str = typer.Option(None, '--days', metavar='DAYS',
                                                help='Days the recipient takes faxes, such as mon,tue,wed,thu,fri.'),
                       start: str = typer.Option(None, '--from', metavar='HH:MM',
                                                 help='Time the recipient starts taking faxes, such as 08:00.'),
                       end: str = typer.Option(None, '--until', metavar='HH:MM',
                                               help='Time the recipient stops taking faxes, such as 18:00.'),
                       any_time: bool = typer.Option(False, '--any-time',
                                                     help='The recipient takes faxes at any time (clears the '
                                                          'days and hours).'),
                       time_zone: str = typer.Option(None, '--time-zone', metavar='ZONE',
                                                     help="The recipient's time zone, such as America/New_York, "
                                                          "or default for your installation's."),
                       learn: bool = typer.Option(None, '--learn/--no-learn',
                                                  help='Whether Faxbot learns when this number is usually '
                                                       'busy, slow or failing, and holds ordinary faxes for a '
                                                       'better hour.')):
    """Show or set when Faxbot sends to one recipient: the hours it takes faxes, and the busy hours and call hours
    Faxbot learned."""
    api = state.api()
    path = '/routing/destinations/' + segment(number) + '/schedule'
    view = api.get(path)
    if any(value is not None for value in (days, start, end, time_zone, learn)) or any_time:
        if any_time and (days or start or end):
            raise CliError('Use --any-time on its own, or give --days, --from and --until.')
        body = {'time_zone': view.get('time_zone') or None, 'days': view.get('days'), 'start': view.get('start'),
                'end': view.get('end'), 'learn_busy': view.get('learn_busy', True)}
        if any_time:
            body.update(days=None, start=None, end=None)
        if days is not None:
            chosen = [day.strip().lower()[:3] for day in days.split(',') if day.strip()]
            if not chosen or any(day not in DAYS for day in chosen):
                raise CliError('Name days as mon, tue, wed, thu, fri, sat or sun, separated by commas.')
            body['days'] = chosen
        if (start is None) != (end is None) and body['start'] is None:
            raise CliError('Give both --from and --until.')
        if start is not None:
            body['start'] = start
        if end is not None:
            body['end'] = end
        if time_zone is not None:
            body['time_zone'] = None if time_zone == 'default' else time_zone
        if learn is not None:
            body['learn_busy'] = learn
        view = api.put(path, json=body)
    state.out().result(view, _human(view))
