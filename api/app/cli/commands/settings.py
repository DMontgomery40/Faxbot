"""Installation settings, providers, health, diagnostics and phone pairing."""
import typer

from .. import profiles, state
from ..errors import CliError, EXIT_FAILURE, EXIT_NOT_FOUND
from ..output import local_time, text
from ..settings_write import secret_names, secrets_from_stdin, write_settings
from ...provider_labels import provider_label
# The same rule as /health/ready's HTTP status, applied to the answer's fields (any server version).
from ...readiness import ready_for_setup


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
                 secret_stdin: list[str] = typer.Option(None, '--secret-stdin', metavar='NAME',
                                                        help='Read this password or key from standard input, one '
                                                             'line each, for scripts. Repeat for more.'),
                 as_text: bool = typer.Option(False, '--text', help='Send every value exactly as typed.')):
    """Change settings by name, for example max_file_size_mb=20. Faxbot checks the result before saving it.

    Passwords and keys are never typed as NAME=VALUE, where they would stay in your shell history: use --secret
    NAME to type one without showing it, or --secret-stdin NAME to read it from standard input.
    """
    changes, typed_secrets, secrets = {}, [], secret_names()
    known = _request_names()
    for item in assignments or []:
        name, separator, raw = item.partition('=')
        if not separator or not name.strip():
            raise CliError(f"Write each setting as NAME=VALUE; '{item}' has no '='.")
        name = name.strip()
        if known.get(name, name) in secrets:
            typed_secrets.append(name)  # refused after the .env check, which comes first
        changes[known.get(name, name)] = raw if as_text or name in known else _value(raw)
    for name in secret or []:
        changes[known.get(name, name)] = typer.prompt(f'Value for {name}', hide_input=True, confirmation_prompt=True)
    for name, value in secrets_from_stdin(secret_stdin).items():
        changes[known.get(name, name)] = value
    if not changes:
        raise CliError('Nothing to change. Give NAME=VALUE pairs, --secret NAME or --secret-stdin NAME.')
    api = state.api()
    result = write_settings(api, changes, typed_secrets=typed_secrets)

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
    """Save every setting to the server's recovery file (owners only). Goes away in the next release; use 'faxbot system backup'."""
    result = state.api().post('/admin/settings/persist', json={})

    def human(out):
        out.line(f"Settings written to {result.get('path')} on the server.")
        out.line("The recovery copy goes away in the next release. To back up everything, run 'faxbot system backup'.")
    state.out().result(result, human)


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


# A provider's section in the settings document, when it is not named after the provider.
_SECTIONS = {'freeswitch': 'fs', 's3': 'storage', 'local': 'storage'}
FREESWITCH_RETIRING = 'FreeSWITCH is removed in the next release. Choose another provider with the Setup wizard.'
# What each role uses, as the setting that chooses it.
_ROLES = {'outbound': 'outbound_backend', 'inbound': 'inbound_backend', 'storage': 'storage_backend'}


def _provider_fields(provider):
    """The provider's id and {its setting name: setting}; an unknown provider is a not-found error."""
    from ...config_plugin_fields import PLUGIN_FIELDS
    name = provider.strip().lower()
    if name not in PLUGIN_FIELDS:
        raise CliError(f"There is no provider named '{provider}'. Run 'faxbot providers list' to see them.",
                       EXIT_NOT_FOUND)
    return name, PLUGIN_FIELDS[name]


def _in_use(current, name):
    hybrid, storage = current.get('hybrid') or {}, current.get('storage') or {}
    return {'outbound': hybrid.get('outbound_backend') == name, 'inbound': hybrid.get('inbound_backend') == name,
            'storage': storage.get('backend') == name}


@providers.command('config')
def providers_config(provider: str = typer.Argument(..., help="Provider from 'faxbot providers list'."),
                     role: str = typer.Option(None, '--role', help='Only say whether it is used for outbound '
                                                                   '(sending), inbound (receiving) or storage.')):
    """Show a provider's settings. Passwords and keys are hidden."""
    name, _ = _provider_fields(provider)
    if role is not None and role not in _ROLES:
        raise CliError('Choose --role outbound, inbound or storage.')
    current = state.api().get('/admin/settings')
    used = _in_use(current, name)
    result = {'provider': name, 'in_use': {role: used[role]} if role else used,
              'settings': current.get(_SECTIONS.get(name, name)) or {}}
    words = {'outbound': 'sending', 'inbound': 'receiving', 'storage': 'storage'}

    def human(out):
        rows = []
        _flatten('', result['settings'], rows)
        roles = [words[item] for item, on in result['in_use'].items() if on]
        out.fields([('Provider', _provider(name)), ('In use for', ', '.join(roles) if roles else 'nothing')])
        out.table(['Setting', 'Value'], rows, empty='This provider has no settings.')
        if name == 'freeswitch':
            out.line(FREESWITCH_RETIRING)
    state.out().result(result, human)


@providers.command('configure')
def providers_configure(provider: str = typer.Argument(..., help="Provider from 'faxbot providers list'."),
                        assignments: list[str] = typer.Argument(None, metavar='NAME=VALUE...',
                                                                help='Provider settings to change.'),
                        secret: list[str] = typer.Option(None, '--secret', metavar='NAME',
                                                         help="Prompt for this setting's value without echoing it, for passwords and keys. Repeat for more."),
                        secret_stdin: list[str] = typer.Option(None, '--secret-stdin', metavar='NAME',
                                                               help='Read this password or key from standard input, '
                                                                    'one line each, for scripts. Repeat for more.'),
                        role: str = typer.Option(None, '--role', help='With --enable: outbound (sending), inbound '
                                                                      '(receiving) or storage.'),
                        enable: bool = typer.Option(False, '--enable', help='Use this provider for sending, receiving or storage '
                                                                     '(choose which with --role).')):
    """Change a provider's settings, or start using it for sending, receiving or storage.

    Passwords and keys are never typed as NAME=VALUE, where they would stay in your shell history: use --secret
    NAME to type one without showing it, or --secret-stdin NAME to read it from standard input.
    """
    name, fields = _provider_fields(provider)
    known = _request_names()

    def setting(key):
        target = fields.get(key) or (key if key in fields.values() else None)
        if target is None:
            raise CliError(f"{_provider(name)} has no setting named '{key}'. Run 'faxbot providers show {name}' "
                           'to see them.')
        return known.get(target, target)

    changes, typed_secrets = {}, []
    for item in assignments or []:
        key, separator, raw = item.partition('=')
        if not separator or not key.strip():
            raise CliError(f"Write each setting as NAME=VALUE; '{item}' has no '='.")
        if setting(key.strip()) in secret_names():
            typed_secrets.append(key.strip())  # refused after the .env check, which comes first
        changes[setting(key.strip())] = raw
    for key in secret or []:
        changes[setting(key)] = typer.prompt(f'Value for {key}', hide_input=True, confirmation_prompt=True)
    for key, value in secrets_from_stdin(secret_stdin).items():
        changes[setting(key)] = value
    if enable:
        if role not in _ROLES:
            raise CliError('Add --role outbound, inbound or storage to say what to use it for.')
        changes[known.get(_ROLES[role], _ROLES[role])] = name
    elif role is not None:
        raise CliError('--role goes with --enable.')
    if not changes:
        raise CliError('Nothing to change. Give NAME=VALUE pairs, --secret NAME, --secret-stdin NAME or --enable.')
    api = state.api()
    result = write_settings(api, changes, typed_secrets=typed_secrets)
    def human(out):
        out.line('Nothing changed.' if not result.get('changed') else 'Saved. Restart Faxbot to apply it.'
                 if result.get('_meta', {}).get('restart_recommended') else 'Saved and applied.')
        if name == 'freeswitch':
            out.line(FREESWITCH_RETIRING)
    state.out().result(result, human)


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
        out.fields([('Sending provider', _provider(result.get('backend'))), ('Ready to send', result.get('backend_healthy')),
                    ('Receiving faxes', result.get('inbound_enabled')),
                    ('Receiving provider', _provider(result.get('receiving_backend'))),
                    ('Ready to receive', result.get('receiving_ready')),
                    ('API keys set up', result.get('api_keys_configured')),
                    ('Checked', local_time(result.get('timestamp')))])
        jobs = result.get('jobs')
        if isinstance(jobs, dict):
            names = {'waiting_for_line': 'waiting for a free line'}
            out.table(['Faxes', 'Count'], [[names.get(name, name.replace('_', ' ')), count]
                                           for name, count in jobs.items()])
    state.out().result(result, human)


def health():
    """Check that Faxbot answers and is ready to send and receive faxes, for whichever of those it is set up to do. No key is needed. For scripts, the command ends with exit code 1 when Faxbot is not ready for them."""
    api = state.api()
    live = api.get('/health', auth=False)
    ready = api.get('/health/ready', auth=False, allow=(503,))
    sends, receives, ok = ready_for_setup(ready)
    result = {'live': live, 'ready': ready, 'ready_for_setup': ok}

    def human(out):
        checks = (ready or {}).get('checks', {})
        out.fields([('Server', api.url), ('Answering', (live or {}).get('status') == 'ok'),
                    ('Ready to send', (ready or {}).get('status') == 'ready' if sends else 'Not set up'),
                    ('Ready to receive', (ready or {}).get('ready_to_receive') if receives else 'Not set up'),
                    ('Sending provider', _provider((ready or {}).get('backend'))),
                    ('Receiving provider', _provider((checks.get('inbound') or {}).get('backend')
                                                     if (checks.get('inbound') or {}).get('enabled') else None)),
                    ('Database', checks.get('db')),
                    ('Ghostscript', checks.get('ghostscript'))])
        for warning in (ready or {}).get('warnings') or []:
            out.line('Warning: ' + warning)
        if (ready or {}).get('message'):
            out.line(ready['message'])
    state.out().result(result, human)
    if not ok:
        raise typer.Exit(EXIT_FAILURE)  # the result above already says what failed, in --json too


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


_DIAGNOSTICS_WORDS = {'ok': 'Working', 'attention': 'Needs attention', 'problem': 'Not working', 'off': 'Not in use'}


def _print_report(result):
    def human(out):
        if not result.get('checked_at'):
            out.line('Diagnostics have not run yet. Run: faxbot system diagnostics run')
            return
        out.line(f"{result.get('summary')} (checked {result.get('checked_at_text')})")
        for section in result.get('sections') or []:
            out.line('')
            out.line(section['title'])
            for item in section['checks']:
                line = f"  {_DIAGNOSTICS_WORDS.get(item['status'], item['status'])}: {item['title']}. {item['sentence']}"
                if item.get('fix'):
                    line += f" ({item['fix']['label']} in the console.)"
                out.line(line)
    state.out().result(result, human)


@diagnostics.command('run')
def diagnostics_run():
    """Check sending, receiving, the fax engine, this server and security now. Sends nothing, changes nothing."""
    _print_report(state.api().post('/admin/diagnostics/report'))


@diagnostics.command('show')
def diagnostics_show():
    """Show the last diagnostics results without checking again."""
    _print_report(state.api().get('/admin/diagnostics/report'))


@diagnostics.command('engine')
def diagnostics_engine(view: str = typer.Argument(..., metavar='VIEW',
                                                 help='registrations, contacts, calls or faxes.')):
    """List what the fax engine reports now: trunk sign-ins, checked addresses, calls or faxes."""
    result = state.api().get(f'/admin/diagnostics/engine/{view}')

    def human(out):
        out.line(result['title'])
        if result['rows']:
            out.table(result['columns'], result['rows'])
        if result.get('message'):
            out.line(result['message'])
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
