"""The SIP trunk to a carrier or to your phone system: choose it, apply it, its status and recent calls."""
import time

import typer

from .. import state
from ..errors import CliError
from ..output import local_time, parse_time

trunk = typer.Typer(help='Your own phone line for faxing, to a phone carrier or to your phone system: presets, status and '
                         'recent calls.', no_args_is_help=True)

TRANSPORTS = {'tls': 'Encrypted (TLS)', 'tcp': 'TCP', 'udp': 'UDP'}
KINDS = {'carrier': 'Carrier', 'phone_system': 'Phone system'}
SIGN_IN = {'registration': 'Username and password', 'ip': 'IP address'}
NUMBER_FORMATS = {'e164': '+ and country code', 'local': 'As a phone here dials it'}


def _status_lines(out, result):
    out.line(result.get('message') or '')
    if not result.get('configured'):
        return
    phone = result.get('kind') == 'phone_system'
    out.fields([('Phone system' if phone else 'Carrier', result.get('preset_label')),
                ('Transport', TRANSPORTS.get(result.get('registration_transport') or result.get('transport'))),
                ('Address on your network' if phone else 'Internet address',
                 (result.get('phone_system') or {}).get('address') if phone
                 else result.get('internet_address') or result.get('public_address'))])
    for key in ('registration_text', 'reachability_text', 'public_address_text', 'network_text', 'ports_text',
                'engine_text'):
        if result.get(key) and result.get(key) != result.get('message'):
            out.line(result[key])
    if result.get('engine_audio'):
        out.line('To try T.38 again, run faxbot providers trunk apply.')
    telnyx = result.get('telnyx_t38') or {}
    for entry in telnyx.get('numbers') or []:
        if entry.get('state') != 'on':
            out.line(entry['text'])
    for text in telnyx.get('connection_texts') or []:
        out.line(text)
    if any(entry.get('fixable') for entry in telnyx.get('numbers') or []):
        out.line('To turn it on, run faxbot providers trunk telnyx t38-on followed by the number.')
    if result.get('network_t38') == 'blocked':
        out.line('If the network check shows a problem, run faxbot providers trunk network status to see how to fix it.')
    if result.get('phone_system_command'):
        out.line(f"Set {result.get('phone_system_setting')} in .env to this computer's address on your local "
                 f"network, then run: {result['phone_system_command']}")
    if result.get('last_call_text'):
        out.line(f"Last call ({local_time(result.get('last_call_at'))}): {result['last_call_text']}")
    if result.get('suggest_audio'):
        out.line('Audio fax may work for new calls: run faxbot providers trunk mode audio.')
    if result.get('t38_off_reason'):
        from ...sip_fax_mode import off_sentence
        moment = parse_time(result.get('t38_off_at'))
        day = moment.astimezone().strftime('%-d %B') if moment else ''
        out.line(off_sentence(result['t38_off_reason'], day, carrier=result.get('preset_label') or ''))
        out.line('To try T.38 again, run faxbot providers trunk mode t38.')


@trunk.command('status')
def trunk_status():
    """Check the SIP trunk: registration with the carrier, Faxbot's public IP address, and the last call."""
    result = state.api().get('/admin/sip/status')
    state.out().result(result, lambda out: _status_lines(out, result))


def _settled(status):
    """Asterisk is back and the carrier has refused Faxbot or answered its check."""
    if status.get('engine_restarting') or not status.get('asterisk_connected'):
        return False
    if status.get('registration') == 'rejected':
        return True
    return status.get('registration') in ('registered', 'not_used') and status.get('reachability') == 'reachable'


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
                                          help='Wait until the SIP trunk registers, then show the trunk check.'),
                timeout: int = typer.Option(60, '--timeout', min=5, max=600, help='Seconds to wait.')):
    """Connect the saved phone line settings. Faxbot restarts its fax engine to load them once no call is in progress, or tells you how to restart it yourself."""
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
    """List recent calls on the phone line, newest first, each with one sentence about what happened."""
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


@trunk.command('restart-engine')
def trunk_restart_engine():
    """Restart the fast fax service once no fax is being sent or received."""
    result = state.api().post('/admin/sip/engine/restart')
    state.out().result(result, lambda out: out.line(result.get('message') or ''))


@trunk.command('mode')
def trunk_mode(mode: str = typer.Argument(..., metavar='t38|audio',
                                          help='t38 (recommended), or audio when T.38 faxes fail on your line.')):
    """Choose how new fax calls are sent, T.38 (fax over IP) or audio when T.38 fails, then reconnect the trunk."""
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


network = typer.Typer(help='Whether fax over IP (T.38) works on the network Faxbot runs on, and what to do when it '
                             'does not.', no_args_is_help=True)
trunk.add_typer(network, name='network')


def _network_lines(out, result):
    from ...sip_network import action_sentence
    if not result.get('applies'):
        out.line(result.get('text') or '')
        return
    out.line(result.get('text') or '')
    for key in ('platform_text', 'tries_text'):
        if result.get(key):
            out.line(result[key])
    moment = parse_time(result.get('action_at'))
    done = action_sentence(result.get('action'), moment.astimezone().strftime('%-d %B') if moment else '')
    if done:
        out.line(done)
    if result.get('router_text'):
        out.line(result['router_text'])
    if result.get('engine_message'):
        out.line(result['engine_message'])
    if result.get('fix_text'):
        out.line(result['fix_text'])
        for step in result.get('fix_steps') or []:
            out.line(f'  {step}')
        if result.get('fix_note'):
            out.line(result['fix_note'])
    if result.get('audio_text'):
        out.line(result['audio_text'])
    if result.get('checked'):
        out.fields([('Internet address', result.get('internet_address') or 'Not known'),
                    ('Checked', local_time(result.get('checked_at')))])


@network.command('status')
def network_status():
    """Show whether fax over IP (T.38) works on this network, where Faxbot runs, and how to fix it when it does not."""
    result = state.api().get('/admin/sip/network')
    state.out().result(result, lambda out: _network_lines(out, result))


@network.command('check')
def network_check():
    """Run the network check again now; new calls use fax over IP only when it works."""
    result = state.api().post('/admin/sip/network/check')
    state.out().result(result, lambda out: _network_lines(out, result))


@network.command('router-ports')
def network_router_ports(choice: str = typer.Argument(..., metavar='on|off',
                                                      help='on lets Faxbot open its fax ports on your router; off stops '
                                                           'it and closes any it opened.')):
    """Let Faxbot open its fax ports on your router (on, the default) or not (off), then check the network again."""
    if choice not in ('on', 'off'):
        raise typer.BadParameter('Use on or off.', param_hint='on|off')
    api = state.api()
    current = api.get('/admin/settings')
    api.put('/admin/settings', json={'sip_router_ports': choice == 'on',
                                     'expected_revision_id': current['_meta']['desired_revision_id']})
    result = api.post('/admin/sip/network/check')
    state.out().result(result, lambda out: _network_lines(out, result))


telnyx = typer.Typer(help='Telnyx settings for fax over IP (T.38) on your trunk numbers.', no_args_is_help=True)
trunk.add_typer(telnyx, name='telnyx')


def _telnyx_lines(out, result):
    if not result.get('applies'):
        out.line('This needs the Telnyx trunk with a Telnyx API key (the key Faxbot also uses for call charges).')
        return
    if result.get('message'):
        out.line(result['message'])
    out.line(result.get('text') or '')
    rows = [[entry['display'], {'on': 'On', 'off': 'Off'}.get(entry['state'], 'Not known'), entry['text']]
            for entry in result.get('numbers') or []]
    if rows:
        out.table(['Number', 'Fax over IP (T.38)', 'What Telnyx shows'], rows, empty='')
    for text in result.get('connection_texts') or []:
        out.line(text)
    if result.get('checked_at'):
        out.fields([('Checked', local_time(result.get('checked_at')))])


@telnyx.command('status')
def telnyx_status():
    """Show whether Telnyx has fax over IP (T.38) turned on for each trunk number, from the last check."""
    result = state.api().get('/admin/sip/telnyx')
    state.out().result(result, lambda out: _telnyx_lines(out, result))


@telnyx.command('t38-on')
def telnyx_t38_on(number: str = typer.Argument(..., metavar='NUMBER',
                                               help='The trunk number, for example +17208565062.')):
    """Turn on fax over IP (T.38) at Telnyx for one trunk number. Only that setting changes."""
    from urllib.parse import quote
    result = state.api().post(f'/admin/sip/telnyx/numbers/{quote(number.strip(), safe="")}/t38')
    state.out().result(result, lambda out: _telnyx_lines(out, result))


@trunk.command('presets')
def trunk_presets(preset: str = typer.Argument(None, metavar='[PRESET]',
                                               help='Show one preset in full, for example avaya-ipoffice.')):
    """List the carriers and phone systems Faxbot knows the settings for, or show one with where each setting comes from. For a phone system it also lists, in order, what you set in it."""
    result = state.api().get('/admin/sip/presets')
    items = result.get('presets') or []
    if preset:
        chosen = next((item for item in items if item['id'] == preset), None)
        if chosen is None:
            raise CliError(f"No preset is called '{preset}'. Run faxbot providers trunk presets to list them.")

        def detail(out):
            out.fields([('Preset', chosen['id']), ('Name', chosen['label']), ('Kind', KINDS[chosen['kind']]),
                        ('Sign-in', ', '.join(SIGN_IN[mode] for mode in chosen['auth_modes'])),
                        ('Transport', ', '.join(TRANSPORTS[name] for name in chosen['transports']))])
            for note in chosen['notes']:
                out.line(note)
            if chosen['t38']:
                out.line(chosen['t38'])
            if chosen['admin_steps']:
                out.line('')
                out.line(f"What you set in {chosen['label']}:")
                for number, step in enumerate(chosen['admin_steps'], 1):
                    out.line(f'{number}. {step}')
            if chosen['sources']:
                out.line('')
                out.line('Sources:')
                for source in chosen['sources']:
                    out.line(f"- {source['url']} (read {local_date(source['read_on'])})")
        state.out().result(chosen, detail)
        return

    def human(out):
        rows = [[item['id'], item['label'], KINDS[item['kind']],
                 ', '.join(SIGN_IN[mode] for mode in item['auth_modes'])] for item in items]
        out.table(['Preset', 'Name', 'Kind', 'Sign-in'], rows, empty='No presets.')
    state.out().result(result, human)


def local_date(day):
    from datetime import date
    try:
        moment = date.fromisoformat(day)
    except (TypeError, ValueError):
        return day
    return f'{moment.day} {moment.strftime("%B %Y")}'


@trunk.command('use')
def trunk_use(preset: str = typer.Argument(..., metavar='PRESET', help='Carrier or phone system preset, for example telnyx (see faxbot providers trunk presets).'),
              host: str = typer.Option(None, '--host', help="The carrier's server address, or your phone system's "
                                                             "address (IP Office, or Aura Session Manager)."),
              port: int = typer.Option(None, '--port', min=1, max=65535, help="The carrier's port, when not the usual one."),
              transport: str = typer.Option(None, '--transport', help='How Faxbot connects to the line: udp, tcp or tls (encrypted), where the preset offers it.'),
              number_format: str = typer.Option(None, '--number-format', metavar='e164|local',
                                                help='How numbers are dialed: e164 (international format, +44...) or '
                                                     'local (as a phone at your site dials them).'),
              prefix: str = typer.Option(None, '--prefix', help='Outside-line digits before a number dialled '
                                                                 'as a phone here dials it, such as 9.')):
    """Choose a carrier or phone system preset for the SIP trunk and save its settings; then connect it with faxbot providers trunk apply.

    A phone system recognizes Faxbot by its IP address, so it needs no username or password.
    """
    api = state.api()
    catalog = {item['id']: item for item in api.get('/admin/sip/presets').get('presets') or []}
    chosen = catalog.get(preset)
    if chosen is None:
        raise CliError(f"No preset is called '{preset}'. Run faxbot providers trunk presets to list them.")
    current = api.get('/admin/settings')
    saved = (current.get('sip') or {}).get('trunk') or {}
    changes = {'sip_trunk_preset': preset}
    if saved.get('auth') not in chosen['auth_modes']:
        changes['sip_trunk_auth'] = chosen['auth_modes'][0]
    if host is not None:
        changes['sip_trunk_host'] = host.strip()
    if port is not None:
        changes['sip_trunk_port'] = port
    if transport is not None:
        if transport not in chosen['transports']:
            raise typer.BadParameter(f"Use {' or '.join(chosen['transports'])}.", param_hint='--transport')
        changes['sip_trunk_transport'] = transport
    if number_format is not None:
        if number_format not in chosen['dial_formats']:
            raise typer.BadParameter(f"{chosen['label']} takes no number format choice."
                                     if not chosen['dial_formats'] else 'Use e164 or local.',
                                     param_hint='--number-format')
        changes['sip_trunk_dial_format'] = number_format
    if prefix is not None:
        changes['sip_trunk_dial_prefix'] = prefix.strip()
    result = api.put('/admin/settings', json={**changes, 'expected_revision_id': current['_meta']['desired_revision_id']})

    def human(out):
        kind = 'phone system' if chosen['kind'] == 'phone_system' else 'carrier'
        out.line(f"Saved {chosen['label']} as the trunk's {kind}. Run faxbot providers trunk apply to connect it.")
    state.out().result({'preset': preset, 'changed': bool(result.get('changed'))}, human)
