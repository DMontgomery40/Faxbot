"""The carrier SIP trunk: its status in plain sentences and its recent calls."""
import typer

from .. import state
from ..output import local_time

trunk = typer.Typer(help="Faxbot's own carrier SIP trunk: status, network and recent calls.", no_args_is_help=True)

TRANSPORTS = {'tls': 'Encrypted (TLS)', 'tcp': 'TCP', 'udp': 'UDP'}


def register(app):
    app.add_typer(trunk, name='trunk')


@trunk.command('status')
def trunk_status():
    """Check the trunk: registration, the carrier's answer, Faxbot's internet address and the last call."""
    result = state.api().get('/admin/sip/status')

    def human(out):
        out.line(result.get('message') or '')
        if not result.get('configured'):
            return
        out.fields([('Carrier', result.get('preset_label')),
                    ('Transport', TRANSPORTS.get(result.get('registration_transport') or result.get('transport'))),
                    ('Internet address', result.get('internet_address') or result.get('public_address'))])
        for key in ('registration_text', 'reachability_text', 'public_address_text', 'ports_text'):
            if result.get(key) and result.get(key) != result.get('message'):
                out.line(result[key])
        if result.get('last_call_text'):
            out.line(f"Last call ({local_time(result.get('last_call_at'))}): {result['last_call_text']}")
    state.out().result(result, human)


@trunk.command('calls')
def trunk_calls(limit: int = typer.Option(10, '--limit', min=1, max=200, help='How many calls to show, newest first.'),
                direction: str = typer.Option(None, '--direction', help='Only outbound or inbound calls.')):
    """List recent trunk calls, newest first, each with one sentence about what happened."""
    params = {'limit': limit}
    if direction:
        if direction not in ('outbound', 'inbound'):
            raise typer.BadParameter('Use outbound or inbound.', param_hint='--direction')
        params['direction'] = direction
    result = state.api().get('/admin/sip/calls', params=params)

    def human(out):
        rows = [[local_time(call.get('started_at')), 'Sent' if call.get('direction') == 'outbound' else 'Received',
                 (call.get('called') if call.get('direction') == 'outbound' else call.get('caller')) or 'Unknown',
                 call.get('summary') or ''] for call in result.get('items') or []]
        out.table(['Time', 'Direction', 'Number', 'What happened'], rows, empty='No calls yet.')
    state.out().result(result, human)
