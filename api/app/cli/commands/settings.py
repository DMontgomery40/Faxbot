"""Installation settings, providers, health, diagnostics and phone pairing."""
import typer

from .. import profiles, state
from ..errors import CliError, EXIT_NOT_FOUND
from ..output import local_time, text

settings = typer.Typer(help='Installation settings. Secrets are always shown masked.', no_args_is_help=True)
providers = typer.Typer(help='Fax providers: which are installed and whether the active one is ready.',
                        no_args_is_help=True)
diagnostics = typer.Typer(help='Check the installation without sending a fax.', no_args_is_help=True)
pair = typer.Typer(help='Pair the Faxbot iPhone app (or a script acting as a phone).', no_args_is_help=True)


def register(app):
    app.add_typer(settings, name='settings')
    app.add_typer(providers, name='providers')
    app.command('health')(health)
    app.add_typer(diagnostics, name='diagnostics')
    app.add_typer(pair, name='pair')


def _flatten(prefix, value, rows):
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f'{prefix}.{key}' if prefix else key, item, rows)
    else:
        rows.append([prefix, text(value)])


@settings.command('get')
def settings_get(section: str = typer.Argument(None, help='Only this section, for example limits or inbound.')):
    """Show the installation settings, including changes waiting for a restart."""
    current = state.api().get('/admin/settings')
    meta = current.get('_meta', {})
    shown = current
    if section:
        if section not in current or section == '_meta':
            raise CliError(f"There is no settings section named '{section}'. Run 'faxbot settings get' to see them.",
                           EXIT_NOT_FOUND)
        shown = {section: current[section]}

    def human(out):
        rows = []
        _flatten('', {key: value for key, value in shown.items() if key != '_meta'}, rows)
        out.table(['Setting', 'Value'], rows)
        if meta.get('apply_state') == 'pending_restart':
            out.line('Some saved changes take effect after a restart: ' + ', '.join(meta.get('pending_fields') or []))
    state.out().result(shown if section else current, human)


def _value(raw):
    lowered = raw.strip().lower()
    if lowered in {'true', 'yes', 'on'}:
        return True
    if lowered in {'false', 'no', 'off'}:
        return False
    if lowered.lstrip('-').isdigit():
        return int(lowered)
    return raw


@settings.command('set')
def settings_set(assignments: list[str] = typer.Argument(None, metavar='NAME=VALUE...',
                                                         help='Settings to change, for example max_file_size_mb=20 '
                                                              'fax_disabled=false.'),
                 secret: list[str] = typer.Option(None, '--secret', metavar='NAME',
                                                  help='Ask for this setting without showing what you type, for '
                                                       'passwords and provider keys. Repeat for more.'),
                 as_text: bool = typer.Option(False, '--text', help='Keep every value as text (no true/false or '
                                                                    'number conversion).')):
    """Change settings. Faxbot checks the whole result before saving it."""
    changes = {}
    for item in assignments or []:
        name, separator, raw = item.partition('=')
        if not separator or not name.strip():
            raise CliError(f"Write each setting as NAME=VALUE; '{item}' has no '='.")
        changes[name.strip()] = raw if as_text else _value(raw)
    for name in secret or []:
        changes[name] = typer.prompt(f'Value for {name}', hide_input=True, confirmation_prompt=True)
    if not changes:
        raise CliError('Nothing to change. Give NAME=VALUE pairs or --secret NAME.')
    api = state.api()
    current = api.get('/admin/settings')
    result = api.put('/admin/settings', json={**changes, 'expected_revision_id': current['_meta']['desired_revision_id']})

    def human(out):
        if not result.get('changed'):
            out.line('Nothing changed; the settings already had these values.')
        elif result.get('_meta', {}).get('restart_recommended'):
            out.line('Settings saved. Restart Faxbot to apply them.')
        else:
            out.line('Settings saved and applied.')
    state.out().result(result, human)


VALIDATE_FIELDS = (('phaxio_api_key', 'PHAXIO_API_KEY'), ('phaxio_api_secret', 'PHAXIO_API_SECRET'),
                   ('sinch_project_id', 'SINCH_PROJECT_ID'), ('sinch_api_key', 'SINCH_API_KEY'),
                   ('sinch_api_secret', 'SINCH_API_SECRET'), ('ami_host', 'AMI_HOST'),
                   ('ami_username', 'AMI_USERNAME'), ('ami_password', 'AMI_PASSWORD'))


@settings.command('validate')
def settings_validate(backend: str = typer.Argument(..., help='Provider to check: phaxio, sinch or sip.'),
                      ami_port: int = typer.Option(None, '--ami-port', help='Asterisk manager port (sip).')):
    """Check provider credentials without saving them or sending a fax.

    Credentials are read from the environment so they stay out of your shell
    history: PHAXIO_API_KEY, PHAXIO_API_SECRET, SINCH_PROJECT_ID, SINCH_API_KEY,
    SINCH_API_SECRET, AMI_HOST, AMI_USERNAME and AMI_PASSWORD.
    """
    import os
    body = {'backend': backend}
    for field, variable in VALIDATE_FIELDS:
        if os.environ.get(variable):
            body[field] = os.environ[variable]
    if ami_port is not None:
        body['ami_port'] = ami_port
    result = state.api().post('/admin/settings/validate', json=body)

    def human(out):
        rows = []
        _flatten('', result.get('checks', {}), rows)
        out.table(['Check', 'Result'], rows, empty='No checks apply to this provider.')
    state.out().result(result, human)


@settings.command('export')
def settings_export():
    """Print the settings as environment lines. Secrets are replaced with ***."""
    result = state.api().get('/admin/settings/export')

    def human(out):
        import sys
        sys.stdout.write(result.get('env', ''))
        if not result.get('env', '').endswith('\n'):
            sys.stdout.write('\n')
    state.out().result(result, human)


@providers.command('list')
def providers_list():
    """List installed fax and storage providers and which ones are in use."""
    api = state.api()
    try:
        items = api.get('/plugins')['items']
    except CliError as error:
        if error.status != 404:
            raise
        config = api.get('/admin/config')
        hybrid = config.get('hybrid', {})
        summary = {'outbound': hybrid.get('outbound'), 'inbound': hybrid.get('inbound'),
                   'configured': config.get('backend_configured', {})}

        def fallback(out):
            out.fields([('Sending provider', hybrid.get('outbound')), ('Receiving provider', hybrid.get('inbound')),
                        ('Credentials saved for', [name for name, ok in summary['configured'].items()
                                                   if ok is True and not name.endswith('_default')])])
        state.out().result(summary, fallback)
        return
    state.out().result(items, lambda out: out.table(['Provider', 'Name', 'Used for', 'In use', 'Source'],
        [[item['id'], item.get('name'), item.get('categories'), item.get('enabled'), item.get('source')]
         for item in items]))


@providers.command('status')
def providers_status():
    """Show whether the active provider is ready and how many faxes are in each state."""
    result = state.api().get('/admin/health-status')

    def human(out):
        out.fields([('Provider', result.get('backend')), ('Ready', result.get('backend_healthy')),
                    ('Receiving faxes', result.get('inbound_enabled')), ('API keys set up', result.get('api_keys_configured')),
                    ('Checked', local_time(result.get('timestamp')))])
        jobs = result.get('jobs')
        if isinstance(jobs, dict):
            out.table(['Faxes', 'Count'], [[name.replace('_', ' '), count] for name, count in jobs.items()])
    state.out().result(result, human)


def health():
    """Check that the server answers and whether it is ready to send faxes. No API key needed."""
    api = state.api()
    live = api.get('/health', auth=False)
    ready = api.get('/health/ready', auth=False, allow=(503,))
    result = {'live': live, 'ready': ready}

    def human(out):
        checks = (ready or {}).get('checks', {})
        out.fields([('Server', api.url), ('Answering', (live or {}).get('status') == 'ok'),
                    ('Ready to send', (ready or {}).get('status') == 'ready'),
                    ('Provider', (ready or {}).get('backend')), ('Database', checks.get('db')),
                    ('Ghostscript', checks.get('ghostscript'))])
        for warning in (ready or {}).get('warnings') or []:
            out.line('Warning: ' + warning)
    state.out().result(result, human)
    if (ready or {}).get('status') != 'ready':
        raise typer.Exit(1)


@diagnostics.command('run')
def diagnostics_run():
    """Run the installation checks and list anything that needs attention."""
    result = state.api().post('/admin/diagnostics/run')

    def human(out):
        summary = result.get('summary', {})
        out.fields([('Healthy', summary.get('healthy'))])
        for issue in summary.get('critical_issues') or []:
            out.line('Problem: ' + str(issue))
        for warning in summary.get('warnings') or []:
            out.line('Warning: ' + str(warning))
        rows = [[f'{section} {name}'.replace('_', ' '), outcome]
                for section, values in (result.get('check_outcomes') or {}).items()
                for name, outcome in values.items() if outcome in {'pass', 'fail', 'warning'}]
        out.table(['Check', 'Result'], rows, empty='No checks ran.')
    state.out().result(result, human)


@pair.command('new')
def pair_new():
    """Create a six-digit code that pairs one phone. It works once, for five minutes."""
    result = state.api().post('/admin/tunnel/pair')
    out = state.out()
    out.result(result, lambda o: o.line(f"Valid once, until {local_time(result['expires_at'])}. Enter it in the "
                                        'Faxbot app.'))
    out.secret('Pairing code', result['code'])


@pair.command('device')
def pair_device(code: str = typer.Argument(..., help='The six-digit pairing code.'),
                device_name: str = typer.Option('Command line', '--device-name', help='Name the key is listed under.'),
                save_profile: str = typer.Option(None, '--save-profile', metavar='NAME',
                                                 help='Save the new key in this profile instead of printing it.')):
    """Do what the phone does with a pairing code: exchange it for the device's own API key."""
    api = state.api()
    result = api.post('/mobile/pair', auth=False, json={'code': code, 'device_name': device_name})
    out = state.out()
    if save_profile:
        document = profiles.load()
        document['profiles'][profiles.check_name(save_profile)] = {'url': api.url, 'key': result['token']}
        path = profiles.save(document)
        out.result({'saved_profile': save_profile, 'base_urls': result.get('base_urls')},
                   lambda o: o.line(f'Paired. The device key is saved in profile {save_profile} ({path}).'))
        return
    out.result(result, lambda o: o.line('Paired.'))
    out.secret('Device API key (shown only once)', result['token'])
