"""The carrier SIP trunk: apply it, its status in plain sentences and its recent calls."""
import time

import typer

from .. import state
from ..output import local_time

trunk = typer.Typer(help="Faxbot's own carrier SIP trunk: status, network and recent calls.", no_args_is_help=True)

TRANSPORTS = {'tls': 'Encrypted (TLS)', 'tcp': 'TCP', 'udp': 'UDP'}


def register(app):
    app.add_typer(trunk, name='trunk')


def _status_lines(out, result):
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
    if result.get('suggest_audio'):
        out.line('Audio fax may work for new calls: run faxbot trunk mode audio.')


@trunk.command('status')
def trunk_status():
    """Check the trunk: registration, the carrier's answer, Faxbot's internet address and the last call."""
    result = state.api().get('/admin/sip/status')
    state.out().result(result, lambda out: _status_lines(out, result))


def _settled(status):
    return (not status.get('engine_restarting') and status.get('asterisk_connected')
            and status.get('registration') in ('registered', 'rejected', 'not_used'))


def _connect(api, wait, timeout):
    """Apply the trunk; after a restart, check it until the carrier answers or the wait ends."""
    applied = api.post('/admin/sip/apply')
    status = None
    if wait and applied.get('engine') in ('restarting', 'current'):
        deadline = time.monotonic() + timeout
        while True:
            status = api.get('/admin/sip/status')
            if _settled(status) or time.monotonic() >= deadline:
                break
            time.sleep(2)
    return applied, status


@trunk.command('apply')
def trunk_apply(wait: bool = typer.Option(True, '--wait/--no-wait',
                                          help='Wait for Asterisk and the carrier, then show the trunk check.'),
                timeout: int = typer.Option(90, '--timeout', min=5, max=600, help='Seconds to wait.')):
    """Write the saved trunk for Asterisk and connect it.

    In the Docker Compose install Faxbot restarts Asterisk to load the trunk,
    once no call is up; elsewhere it says to restart the Asterisk service.
    """
    applied, status = _connect(state.api(), wait, timeout)
    result = {**applied, 'status': status}

    def human(out):
        out.line(applied.get('message') or '')
        if status:
            _status_lines(out, status)
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


@trunk.command('mode')
def trunk_mode(mode: str = typer.Argument(..., metavar='t38|audio',
                                          help='t38 (recommended) or audio, for when T.38 data cannot come back.')):
    """Choose T.38 or audio fax for new calls and connect the trunk with it."""
    if mode not in ('t38', 'audio'):
        raise typer.BadParameter('Use t38 or audio.', param_hint='MODE')
    api = state.api()
    current = api.get('/admin/settings')
    saved = api.put('/admin/settings', json={'sip_t38_enabled': mode == 't38',
                                             'expected_revision_id': current['_meta']['desired_revision_id']})
    applied, _ = _connect(api, wait=False, timeout=0)
    result = {'mode': mode, 'changed': bool(saved.get('changed')), 'applied': bool(applied.get('ok')),
              'engine': applied.get('engine'), 'message': applied.get('message')}

    def human(out):
        kind = 'T.38' if mode == 't38' else 'audio'
        if not result['changed']:
            out.line(f'The trunk already uses {kind} fax.')
        elif result['engine'] == 'manual':
            out.line(f'New calls use {kind} fax once you restart the Asterisk service.')
        else:
            out.line(f'New calls use {kind} fax. {applied.get("message")}')
    state.out().result(result, human)
