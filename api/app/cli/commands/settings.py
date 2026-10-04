"""Installation settings, providers, health, diagnostics and phone pairing."""
import typer

from .. import profiles, state
from ..client import segment
from ..errors import CliError, EXIT_CONFLICT, EXIT_NOT_FOUND
from ..output import local_time, text
from ...provider_labels import provider_label


def _provider(identity):
    """A provider's one plain name for people; JSON output keeps the id."""
    return provider_label(identity) if identity else None

settings = typer.Typer(help='Every Faxbot setting: show, change, check and save them. Passwords and keys are never shown.', no_args_is_help=True)
providers = typer.Typer(help='Fax providers: which are installed and whether the active one is ready.',
                        no_args_is_help=True)
diagnostics = typer.Typer(help='Check the installation without sending a fax.', no_args_is_help=True)
pair = typer.Typer(help='Pair the Faxbot iPhone app with this installation.', no_args_is_help=True)


def _flatten(prefix, value, rows):
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f'{prefix}.{key}' if prefix else key, item, rows)
    else:
        rows.append([prefix, text(value)])


@settings.command('get')
def settings_get(section: str = typer.Argument(None, help='Only this section, for example limits or inbound.')):
    """Show the settings, including changes waiting for a restart."""
    current = state.api().get('/admin/settings')
    meta = current.get('_meta', {})
    shown = current
    if section:
        if section not in current or section == '_meta':
            raise CliError(f"There is no settings section named '{section}'. Run 'faxbot system settings get' to see them.",
                           EXIT_NOT_FOUND)
        shown = {section: current[section]}

    managed = set(meta.get('env_managed') or [])

    def human(out):
        rows = []
        _flatten('', {key: value for key, value in shown.items() if key != '_meta'}, rows)
        for row in rows:
            if row[0].replace('.', '_') in managed:
                row[1] = 'set in .env'
        out.table(['Setting', 'Value'], rows)
        if managed:
            out.line('Set in .env (change them there, then run docker compose up -d): ' + ', '.join(sorted(managed)))
        if not (current.get('backend') or {}).get('type') and (not section or section in {'backend', 'hybrid'}):
            out.line('No fax provider set up yet.')
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


def _request_names():
    """Each setting's name in a settings change, by its own name and by that name.

    Settings are accepted by their configuration names too (fax_backend is sent as
    backend). The server converts text to numbers and yes/no itself, so values of
    known settings are sent as typed: a fax number such as 3035551234 stays text.
    """
    from ...config_values import ConfigurationValues
    names = {}
    for name, field in ConfigurationValues.model_fields.items():
        sent = (field.json_schema_extra or {}).get('patch_name', name)
        names[name] = names[sent] = sent
    return names


@settings.command('set')
def settings_set(assignments: list[str] = typer.Argument(None, metavar='NAME=VALUE...',
                                                         help='Settings to change, for example max_file_size_mb=20 '
                                                              'fax_disabled=false.'),
                 secret: list[str] = typer.Option(None, '--secret', metavar='NAME',
                                                  help='Ask for this setting without showing what you type, for '
                                                       'passwords and provider keys. Repeat for more.'),
                 as_text: bool = typer.Option(False, '--text', help='Send every value exactly as typed.')):
    """Change settings by name, for example max_file_size_mb=20. Faxbot checks the result before saving it."""
    changes = {}
    known = _request_names()
    for item in assignments or []:
        name, separator, raw = item.partition('=')
        if not separator or not name.strip():
            raise CliError(f"Write each setting as NAME=VALUE; '{item}' has no '='.")
        name = name.strip()
        changes[known.get(name, name)] = raw if as_text or name in known else _value(raw)
    for name in secret or []:
        changes[known.get(name, name)] = typer.prompt(f'Value for {name}', hide_input=True, confirmation_prompt=True)
    if not changes:
        raise CliError('Nothing to change. Give NAME=VALUE pairs or --secret NAME.')
    api = state.api()
    current = api.get('/admin/settings')
    if set(changes) & set(current.get('_meta', {}).get('env_managed') or []):
        raise CliError('This key is set in .env. Change it there, then run docker compose up -d.', EXIT_CONFLICT)
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
                   ('ami_username', 'AMI_USERNAME'), ('ami_password', 'AMI_PASSWORD'),
                   ('efax_app_id', 'EFAX_APP_ID'), ('efax_api_key', 'EFAX_API_KEY'), ('efax_user_id', 'EFAX_USER_ID'))


@settings.command('validate')
def settings_validate(backend: str = typer.Argument(..., help='Provider whose credentials to check: phaxio, sinch, efax or sip.'),
                      ami_port: int = typer.Option(None, '--ami-port', help='SIP only: the Asterisk manager interface port, if not 5038.')):
    """Check a provider's credentials without saving them or sending a fax.

    Credentials are read from these environment variables so they stay out of your shell history: PHAXIO_API_KEY, PHAXIO_API_SECRET, SINCH_PROJECT_ID, SINCH_API_KEY, SINCH_API_SECRET, AMI_HOST, AMI_USERNAME, AMI_PASSWORD, EFAX_APP_ID, EFAX_API_KEY and EFAX_USER_ID. Checking eFax credentials can end the eFax session Faxbot was using; Faxbot signs in again by itself.
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


@settings.command('reload')
def settings_reload():
    """Read the saved settings again and show any changes still waiting for a restart."""
    result = state.api().post('/admin/settings/reload', json={})
    meta = result.get('_meta', {})

    def human(out):
        out.line('Faxbot read its saved settings again.')
        if meta.get('apply_state') == 'pending_restart':
            out.line('Some saved changes take effect after a restart: ' + ', '.join(meta.get('pending_fields') or []))
    state.out().result(result, human)


@settings.command('persist')
def settings_persist():
    """Save every setting, passwords and keys included, to the installation's private recovery file for restoring later. Owners only."""
    result = state.api().post('/admin/settings/persist', json={})
    state.out().result(result, lambda out: out.line(f"Settings written to {result.get('path')} on the server."))


@settings.command('export')
def settings_export():
    """Print every setting, one per line, as it would appear in a settings file. Passwords and keys are shown as ***."""
    result = state.api().get('/admin/settings/export')

    def human(out):
        import sys
        sys.stdout.write(result.get('env', ''))
        if not result.get('env', '').endswith('\n'):
            sys.stdout.write('\n')
    state.out().result(result, human)


@providers.command('list')
def providers_list():
    """List the fax and storage providers installed, and which ones are in use."""
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
            out.fields([('Sending provider', _provider(hybrid.get('outbound'))),
                        ('Receiving provider', _provider(hybrid.get('inbound'))),
                        ('Credentials saved for', [name for name, ok in summary['configured'].items()
                                                   if ok is True and not name.endswith('_default')])])
        state.out().result(summary, fallback)
        return
    state.out().result(items, lambda out: out.table(['Provider', 'Name', 'Used for', 'In use', 'Source'],
        [[item['id'], item.get('name'), item.get('categories'), item.get('enabled'), item.get('source')]
         for item in items]))


@providers.command('callbacks')
def providers_callbacks():
    """Show the addresses your receiving provider must call for incoming faxes."""
    result = state.api().get('/admin/inbound/callbacks')
    state.out().result(result, lambda out: out.table(['Callback', 'Address', 'Notes'],
        [[item.get('name'), item.get('url'), item.get('notes')] for item in result.get('callbacks', [])],
        empty='The receiving provider needs no callback address.'))


@providers.command('config')
def providers_config(provider: str = typer.Argument(..., help="Provider from 'faxbot providers list'."),
                     role: str = typer.Option(None, '--role', help='outbound, inbound or storage.')):
    """Show a provider's settings. Passwords and keys are hidden."""
    result = state.api().get(f'/plugins/{segment(provider)}/config', params={'role': role})

    def human(out):
        rows = []
        _flatten('', result.get('settings', {}), rows)
        out.fields([('Provider', provider), ('Role', result.get('role')), ('In use', result.get('enabled'))])
        out.table(['Setting', 'Value'], rows, empty='This provider has no settings.')
    state.out().result(result, human)


@providers.command('configure')
def providers_configure(provider: str = typer.Argument(..., help="Provider from 'faxbot providers list'."),
                        assignments: list[str] = typer.Argument(None, metavar='NAME=VALUE...',
                                                                help='Provider settings to change.'),
                        secret: list[str] = typer.Option(None, '--secret', metavar='NAME',
                                                         help="Prompt for this setting's value without echoing it, for passwords and keys. Repeat for more."),
                        role: str = typer.Option(None, '--role', help='outbound, inbound or storage.'),
                        enable: bool = typer.Option(False, '--enable', help='Make this provider active for the role.'),
                        disable: bool = typer.Option(False, '--disable', help='Stop using this provider for the role.')):
    """Change a provider's settings, or start or stop using it."""
    if enable and disable:
        raise CliError('Choose --enable or --disable, not both.')
    changes = {}
    for item in assignments or []:
        name, separator, raw = item.partition('=')
        if not separator or not name.strip():
            raise CliError(f"Write each setting as NAME=VALUE; '{item}' has no '='.")
        changes[name.strip()] = _value(raw)
    for name in secret or []:
        changes[name] = typer.prompt(f'Value for {name}', hide_input=True, confirmation_prompt=True)
    body = {'role': role}
    if changes:
        body['settings'] = changes
    if enable or disable:
        body['enabled'] = enable
    if not changes and not (enable or disable):
        raise CliError('Nothing to change. Give NAME=VALUE pairs, --secret NAME, --enable or --disable.')
    api = state.api()
    current = api.get(f'/plugins/{segment(provider)}/config', params={'role': role})
    result = api.put(f'/plugins/{segment(provider)}/config',
                     json={**body, 'expected_revision_id': current['_meta']['desired_revision_id']})
    state.out().result(result, lambda out: out.line(
        'Nothing changed.' if not result.get('changed') else 'Saved. Restart Faxbot to apply it.'
        if result.get('_meta', {}).get('restart_recommended') else 'Saved and applied.'))


@providers.command('registry')
def providers_registry():
    """List the providers you can add from Faxbot's provider list."""
    result = state.api().get('/plugin-registry')
    items = result.get('items', []) if isinstance(result, dict) else []
    state.out().result(result, lambda out: out.table(['Provider', 'Name', 'Description'],
        [[item.get('id'), item.get('name'), item.get('description')] for item in items], empty='The registry is empty.'))


def efax_status():
    """Show whether Faxbot is collecting your received faxes from eFax, when it last checked, and faxes still stored at eFax."""
    result = state.api().get('/admin/inbound/efax')

    def human(out):
        if not result.get('receiving'):
            out.line('Faxbot is not collecting received faxes from eFax; eFax is not set up to receive.')
        else:
            out.line('Faxbot collects your received faxes from eFax.')
            checked = local_time(result.get('checked_at'), empty=None)
            out.line(result.get('problem') or (f'Faxbot last checked eFax at {checked}.' if checked
                                               else 'Faxbot has not checked eFax yet.'))
        for note in result.get('notes') or []:
            out.line(note)
    state.out().result(result, human)


def providers_import(source: str = typer.Argument(..., metavar='FILE',
                                                  help="A JSON file of provider descriptions, or a Markdown file with "
                                                       "them in code blocks; '-' reads standard input.")):
    """Add several fax services at once from a file of their descriptions."""
    import json
    import sys
    if source == '-':
        text = sys.stdin.read()
    else:
        try:
            with open(source, encoding='utf-8') as handle:
                text = handle.read()
        except OSError:
            raise CliError(f'Cannot read {source}.') from None
    try:
        document = json.loads(text)
    except ValueError:
        body = {'markdown': text}
    else:
        if isinstance(document, dict) and isinstance(document.get('items'), list):
            document = document['items']
        body = {'items': document if isinstance(document, list) else [document]}
    result = state.api().post('/admin/plugins/http/import-manifests', json=body)

    def human(out):
        for item in result.get('imported') or []:
            out.line(f"Added the provider {item.get('name') or item.get('id')}.")
        for item in result.get('errors') or []:
            out.line(f"Could not add one provider: {item.get('error')}")
        if not result.get('imported') and not result.get('errors'):
            out.line('No providers were added.')
    state.out().result(result, human)


def _manifest(path):
    import json
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except OSError:
        raise CliError(f'Cannot read {path}.') from None
    except ValueError:
        raise CliError(f'{path} is not valid JSON.') from None


@providers.command('validate')
def providers_validate(manifest: str = typer.Argument(..., help='The file that describes the fax service (JSON).')):
    """Check the file that describes a fax service before you add it. Nothing is installed or sent."""
    result = state.api().post('/admin/plugins/http/validate', json={'manifest': _manifest(manifest), 'render_only': True})
    state.out().result(result, lambda out: out.line('The manifest is valid.' if result.get('ok', True)
                                                    else 'The manifest has problems: ' + text(result.get('error'))))


@providers.command('install')
def providers_install(manifest: str = typer.Argument(..., help='The file that describes the fax service (JSON).')):
    """Install a custom HTTP fax provider from its manifest file."""
    result = state.api().post('/admin/plugins/http/install', json={'manifest': _manifest(manifest)})
    state.out().result(result, lambda out: out.line(f"Provider {result.get('id')} installed. Configure it with "
                                                    f"faxbot providers configure {result.get('id')}."))


@providers.command('status')
def providers_status():
    """Show whether the active provider is ready and how many faxes are in each state."""
    result = state.api().get('/admin/health-status')

    def human(out):
        out.fields([('Provider', _provider(result.get('backend'))), ('Ready', result.get('backend_healthy')),
                    ('Receiving faxes', result.get('inbound_enabled')), ('API keys set up', result.get('api_keys_configured')),
                    ('Checked', local_time(result.get('timestamp')))])
        jobs = result.get('jobs')
        if isinstance(jobs, dict):
            out.table(['Faxes', 'Count'], [[name.replace('_', ' '), count] for name, count in jobs.items()])
    state.out().result(result, human)


def health():
    """Check that Faxbot answers and is ready to send faxes. No key is needed. For scripts, the command ends with exit code 1 when Faxbot is not ready."""
    api = state.api()
    live = api.get('/health', auth=False)
    ready = api.get('/health/ready', auth=False, allow=(503,))
    result = {'live': live, 'ready': ready}

    def human(out):
        checks = (ready or {}).get('checks', {})
        out.fields([('Server', api.url), ('Answering', (live or {}).get('status') == 'ok'),
                    ('Ready to send', (ready or {}).get('status') == 'ready'),
                    ('Provider', _provider((ready or {}).get('backend'))), ('Database', checks.get('db')),
                    ('Ghostscript', checks.get('ghostscript'))])
        for warning in (ready or {}).get('warnings') or []:
            out.line('Warning: ' + warning)
        if (ready or {}).get('message'):
            out.line(ready['message'])
    state.out().result(result, human)
    if (ready or {}).get('status') != 'ready':
        raise typer.Exit(1)


@diagnostics.command('database')
def diagnostics_database():
    """Show whether Faxbot can reach its database, and how many records you can see."""
    result = state.api().get('/admin/db-status')

    def human(out):
        sqlite = result.get('sqlite') or {}
        out.fields([('Database', result.get('engine')), ('Connected', result.get('connected')),
                    ('File', sqlite.get('path')), ('Size (bytes)', sqlite.get('size_bytes'))])
        counts = result.get('counts') or {}
        out.table(['Records', 'Count'], [[name.replace('_', ' '), '-' if count is None else count]
                                          for name, count in counts.items()])
    state.out().result(result, human)


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
    """Create a six-digit pairing code for one phone. It works once, within five minutes."""
    result = state.api().post('/admin/tunnel/pair')
    out = state.out()
    out.result(result, lambda o: o.line(f"Valid once, until {local_time(result['expires_at'])}. Enter it in the "
                                        'Faxbot app.'))
    out.secret('Pairing code', result['code'])


@pair.command('device')
def pair_device(code: str = typer.Argument(..., help='The six-digit pairing code.'),
                device_name: str = typer.Option('Command line', '--device-name', help='Device name shown on the new key.'),
                save_profile: str = typer.Option(None, '--save-profile', metavar='NAME',
                                                 help='Save the new key in this profile instead of printing it.')):
    """Test pairing as if this computer were a phone: exchange a pairing code for the device's own API key."""
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
