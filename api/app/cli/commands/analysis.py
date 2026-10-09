"""Configure operational AI analysis and read or refresh its saved report."""
import typer
from .. import state
from ..errors import CliError
from ..output import local_time
from ..settings_write import secrets_from_stdin, write_settings

analysis = typer.Typer(help='AI analysis: configure a model, test the connection, and refresh its advice.',
                       no_args_is_help=True)
ENDPOINTS = {'openai': 'https://api.openai.com/v1', 'openrouter': 'https://openrouter.ai/api/v1'}


def _show(data):
    def human(out):
        out.line(data.get('message') or f"Analysis: {data.get('state', 'unavailable').replace('_', ' ')}.")
        run = data.get('last_run') or {}
        if run.get('summary'):
            if data.get('stale'):
                out.line('This saved analysis is out of date. Refresh it before relying on its advice.')
            out.line(f"Last completed: {local_time(run.get('finished_at'))}; model: {run.get('model') or 'unknown'}.")
            out.line(run['summary'])
            for item in run.get('evidence') or []:
                if isinstance(item, dict):
                    out.line('Evidence: ' + str(item.get('label') or item.get('tool') or item.get('source') or 'Operational records'))
        if data.get('next_run_at'):
            out.line('Next refresh: ' + local_time(data['next_run_at']))
    state.out().result(data, human)


@analysis.command('status')
def analysis_status():
    """Read the saved analysis, its freshness and the next refresh time."""
    _show(state.api().get('/analysis'))


@analysis.command('run')
def analysis_run():
    """Queue a fresh analysis. Check status for the result; this does not change fax settings."""
    _show(state.api().post('/analysis/run'))


@analysis.command('test')
def analysis_test():
    """Test the saved model connection with a synthetic request, without fax records."""
    result = state.api().post('/analysis/test')
    state.out().result(result, lambda out: out.line(result.get('message') or 'Connection test completed.'))
    if not result.get('ok'):
        raise typer.Exit(1)


@analysis.command('configure')
def analysis_configure(
    provider: str = typer.Option(None, help='openai, openrouter or compatible.'),
    model: str = typer.Option(None, help='The model identifier from your provider.'),
    base_url: str = typer.Option(None, help='API base address for a compatible provider.'),
    interval_hours: int = typer.Option(None, min=0, max=168, help='Refresh interval in hours; 0 means manual only.'),
    enable: bool = typer.Option(None, '--enable/--disable', help='Allow or stop operational analysis.'),
    api_key: bool = typer.Option(False, '--key-prompt', help='Enter the model API key without showing it.'),
    key_stdin: bool = typer.Option(False, '--key-stdin', help='Read the model API key from standard input.'),
):
    """Save model settings. The owner must enable analysis before operational data is sent."""
    if api_key and key_stdin:
        raise CliError('Choose either --key-prompt or --key-stdin.')
    if provider is not None and provider not in {*ENDPOINTS, 'compatible'}:
        raise CliError('Choose openai, openrouter or compatible.')
    changes = {}
    if provider is not None:
        changes['analysis_provider'] = provider
        if provider in ENDPOINTS and base_url is None:
            changes['analysis_base_url'] = ENDPOINTS[provider]
    for key, value in [('model', model), ('base_url', base_url), ('interval_hours', interval_hours), ('enabled', enable)]:
        if value is not None:
            changes['analysis_' + key] = value
    if api_key:
        changes['analysis_api_key'] = typer.prompt('Model API key', hide_input=True)
    elif key_stdin:
        changes.update(secrets_from_stdin(['analysis_api_key']))
    if not changes:
        raise CliError('Choose settings to change. Use --help to see the options.')
    result = write_settings(state.api(), changes)
    state.out().result(result, lambda out: out.line('AI analysis settings saved. Run faxbot system analysis test to check the connection.'))
