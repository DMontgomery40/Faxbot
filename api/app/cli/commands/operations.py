"""Running the server: activity logs and restart."""
import typer

from .. import state
from ..errors import CliError
from ..output import local_time

logs = typer.Typer(help='The activity log: sign-ins, faxes, phone pairing and terminal use.', no_args_is_help=True)


def _log_rows(items):
    rows = []
    for item in items:
        if isinstance(item, dict):
            details = ', '.join(f'{key}={value}' for key, value in item.items() if key not in {'ts', 'event'})
            rows.append([local_time(item.get('ts')), item.get('event'), details])
        else:
            rows.append(['', '', str(item)])
    return rows


@logs.command('list')
def logs_list(search: str = typer.Option(None, '--search', help='Only entries containing this text.'),
              event: str = typer.Option(None, '--event', help='Only log entries of this event type, for example job_created.'),
              since: str = typer.Option(None, '--since', help='Only entries after this time, for example 2026-10-01.'),
              limit: int = typer.Option(200, '--limit', min=1, help='How many entries to show.')):
    """Show recent activity log entries."""
    result = state.api().get('/admin/logs', params={'q': search, 'event': event, 'since': since, 'limit': limit})
    state.out().result(result, lambda out: out.table(['When', 'Event', 'Details'], _log_rows(result.get('items', [])),
                                                     empty='No log entries.'))


@logs.command('tail')
def logs_tail(search: str = typer.Option(None, '--search', help='Only lines containing this text.'),
              event: str = typer.Option(None, '--event', help='Only log entries of this event type.'),
              lines: int = typer.Option(200, '--lines', min=1, max=20000, help='How many of the last lines to show.')):
    """Show the end of the activity log file, when Faxbot writes one."""
    result = state.api().get('/admin/logs/tail', params={'q': search, 'event': event, 'lines': lines})

    def human(out):
        for line in result.get('items', []):
            out.line(line if isinstance(line, str) else str(line))
    state.out().result(result, human)


def restart(yes: bool = typer.Option(False, '--yes', '-y', help='Do not ask for confirmation.')):
    """Restart Faxbot, when the installation allows it."""
    if not yes:
        if state.out().json_mode or state.out().quiet:
            raise CliError('Add --yes to confirm when using --json or --quiet.')
        typer.confirm('Restart the Faxbot server now? Faxes being sent continue after it starts again.', abort=True)
    result = state.api().post('/admin/restart')
    state.out().result(result, lambda out: out.line('Faxbot is restarting. Its service manager starts it again.'))
