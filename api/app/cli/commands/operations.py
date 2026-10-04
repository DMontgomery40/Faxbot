"""Running the server: activity logs, remote-access tunnels, approved host actions and restart."""
import os

import typer

from .. import state
from ..errors import CliError
from ..output import local_time

logs = typer.Typer(help='The activity log: sign-ins, faxes, phone pairing and terminal use.', no_args_is_help=True)
tunnel = typer.Typer(help='Remote access: reach Faxbot from outside your office network.', no_args_is_help=True)
actions = typer.Typer(help='Maintenance tasks this server lets you run.', no_args_is_help=True)


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
              event: str = typer.Option(None, '--event', help='Only this kind of entry, for example job_created.'),
              since: str = typer.Option(None, '--since', help='Only entries after this time, for example 2026-10-01.'),
              limit: int = typer.Option(200, '--limit', min=1, help='How many entries to show.')):
    """Show recent activity log entries."""
    result = state.api().get('/admin/logs', params={'q': search, 'event': event, 'since': since, 'limit': limit})
    state.out().result(result, lambda out: out.table(['When', 'Event', 'Details'], _log_rows(result.get('items', [])),
                                                     empty='No log entries.'))


@logs.command('tail')
def logs_tail(search: str = typer.Option(None, '--search', help='Only lines containing this text.'),
              event: str = typer.Option(None, '--event', help='Only this kind of entry.'),
              lines: int = typer.Option(200, '--lines', min=1, max=20000, help='How many of the last lines to show.')):
    """Show the end of the activity log file, when Faxbot writes one."""
    result = state.api().get('/admin/logs/tail', params={'q': search, 'event': event, 'lines': lines})

    def human(out):
        for line in result.get('items', []):
            out.line(line if isinstance(line, str) else str(line))
    state.out().result(result, human)


def _tunnel_fields(result):
    return [('Enabled', result.get('enabled')), ('Provider', result.get('provider')), ('State', result.get('status')),
            ('Public address', result.get('public_url')), ('Local address', result.get('local_ip')),
            ('Problem', result.get('error_message')), ('Checked', local_time(result.get('last_checked')))]


@tunnel.command('status')
def tunnel_status():
    """Show whether remote access is on and working."""
    result = state.api().get('/admin/tunnel/status')
    state.out().result(result, lambda out: out.fields(_tunnel_fields(result)))


@tunnel.command('set')
def tunnel_set(provider: str = typer.Argument(..., help='How Faxbot is reached from outside: none, cloudflare, wireguard or tailscale.'),
               disable: bool = typer.Option(False, '--disable', help='Save the settings but keep the tunnel off.'),
               cloudflare_domain: str = typer.Option(None, '--cloudflare-domain', help='Custom domain (Cloudflare).'),
               wireguard_endpoint: str = typer.Option(None, '--wireguard-endpoint', help='Server endpoint (WireGuard).'),
               wireguard_public_key: str = typer.Option(None, '--wireguard-public-key',
                                                        help="Server's public key (WireGuard)."),
               wireguard_client_ip: str = typer.Option(None, '--wireguard-client-ip', help='This computer\'s tunnel '
                                                                                           'address (WireGuard).'),
               wireguard_dns: str = typer.Option(None, '--wireguard-dns', help='DNS server (WireGuard).'),
               tailscale_hostname: str = typer.Option(None, '--tailscale-hostname', help='Host name (Tailscale).')):
    """Choose how Faxbot is reached from outside your office, and turn it on or off.

    For Tailscale, set TAILSCALE_AUTH_KEY to its sign-in key before you run this.
    """
    body = {'enabled': provider != 'none' and not disable, 'provider': provider,
            'cloudflare_custom_domain': cloudflare_domain, 'wireguard_endpoint': wireguard_endpoint,
            'wireguard_server_public_key': wireguard_public_key, 'wireguard_client_ip': wireguard_client_ip,
            'wireguard_dns': wireguard_dns, 'tailscale_hostname': tailscale_hostname,
            'tailscale_auth_key': os.environ.get('TAILSCALE_AUTH_KEY') or None}
    result = state.api().post('/admin/tunnel/config', json=body)
    state.out().result(result, lambda out: out.fields(_tunnel_fields(result)))


@tunnel.command('test')
def tunnel_test():
    """Check that Faxbot can be reached at its public address."""
    result = state.api().post('/admin/tunnel/test')
    state.out().result(result, lambda out: out.line(('Reachable' if result.get('ok') else 'Not reachable')
                                                    + (f" at {result['target']}" if result.get('target') else '')
                                                    + (f": {result['message']}" if result.get('message') else '.')))
    if not result.get('ok'):
        raise typer.Exit(1)


@actions.command('list')
def actions_list():
    """List the maintenance tasks this server lets you run."""
    result = state.api().get('/admin/actions')

    def human(out):
        if not result.get('enabled'):
            out.line('Maintenance actions are turned off on this server.')
            return
        out.table(['Action', 'Name'], [[item['id'], item.get('label')] for item in result.get('items', [])],
                  empty='No actions apply to this provider.')
    state.out().result(result, human)


@actions.command('run')
def actions_run(action: str = typer.Argument(..., help="Action from 'faxbot system actions list'.")):
    """Run a maintenance task this server allows and show what it printed."""
    result = state.api().post('/admin/actions/run', json={'id': action})

    def human(out):
        out.line(('Finished.' if result.get('ok') else 'The action failed.') + f" (exit code {result.get('code')})")
        for stream in ('stdout', 'stderr'):
            if result.get(stream):
                out.line(result[stream].rstrip())
    state.out().result(result, human)
    if not result.get('ok'):
        raise typer.Exit(1)


def restart(yes: bool = typer.Option(False, '--yes', '-y', help='Do not ask for confirmation.')):
    """Restart Faxbot, when the installation allows it."""
    if not yes:
        if state.out().json_mode or state.out().quiet:
            raise CliError('Add --yes to confirm when using --json or --quiet.')
        typer.confirm('Restart the Faxbot server now? Faxes being sent continue after it starts again.', abort=True)
    result = state.api().post('/admin/restart')
    state.out().result(result, lambda out: out.line('Faxbot is restarting. Its service manager starts it again.'))
