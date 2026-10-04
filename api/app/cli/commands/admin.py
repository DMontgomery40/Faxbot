"""Local administration of a stopped installation on this computer.

These commands read the same settings as the server (DATABASE_URL, FAX_DATA_DIR,
FAXBOT_INSTALLATION_KEY_PATH, FAXBOT_DIRECT_KEY_PATH) and refuse to run while a
Faxbot server answers on the configured address or holds the installation.
"""
import functools
import inspect
import os

import typer

from .. import state
from ..client import Api
from ..errors import EXIT_RUNNING, EXIT_UNAVAILABLE, CliError
from ..output import local_time

admin = typer.Typer(help='Run a stopped installation on this computer: recover owner access, back up, restore, '
                         'upgrade the database and check its state.', no_args_is_help=True)

# The options that find a stopped installation, read as the server reads them.
_LOCATION = (
    ('database_url', '--database-url', 'DATABASE_URL', 'URL', 'Where the database is, if not the usual place (./faxbot.db, as the server uses).'),
    ('data_dir', '--data-dir', 'FAX_DATA_DIR', 'FOLDER', 'Where the data folder is, if not the usual place (./faxdata, as the server uses).'),
    ('key_file', '--key-file', 'FAXBOT_INSTALLATION_KEY_PATH', 'FILE',
     'Installation encryption key file (default: .configuration.key in the data folder).'),
    ('direct_key_file', '--direct-key-file', 'FAXBOT_DIRECT_KEY_PATH', 'FILE',
     'Direct delivery signing key file (default: .direct-identity.key in the data folder).'),
)


def _location_option(flag, variable, metavar, help):
    return typer.Option(None, flag, envvar=variable, show_envvar=True, metavar=metavar, show_default=False, help=help)


def _remember(database_url=None, data_dir=None, key_file=None, direct_key_file=None):
    state.current().admin_options = {'database_url': database_url, 'data_dir': data_dir, 'key_path': key_file,
                                     'direct_key_path': direct_key_file}


@admin.callback()
def admin_options(ctx: typer.Context,
                  database_url: str = _location_option(*_LOCATION[0][1:]),
                  data_dir: str = _location_option(*_LOCATION[1][1:]),
                  key_file: str = _location_option(*_LOCATION[2][1:]),
                  direct_key_file: str = _location_option(*_LOCATION[3][1:])):
    _remember(database_url, data_dir, key_file, direct_key_file)


def on_this_computer(command):
    """The command with the options that find the installation, for its home under faxbot system."""
    signature = inspect.signature(command)
    located = [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=str,
                                 default=_location_option(flag, variable, metavar, help))
               for name, flag, variable, metavar, help in _LOCATION]

    @functools.wraps(command)
    def run(*args, database_url=None, data_dir=None, key_file=None, direct_key_file=None, **kwargs):
        _remember(database_url, data_dir, key_file, direct_key_file)
        return command(*args, **kwargs)
    run.__signature__ = signature.replace(parameters=[*signature.parameters.values(), *located])
    return run


def _installation():
    from ..local import locate
    return locate(os.environ, **state.current().admin_options)


def _probe():
    """Refuse when a Faxbot server answers on the configured address."""
    current = state.current()
    probe = Api(current.url, None, client_factory=current.client_factory, timeout=3.0)
    try:
        probe.request('GET', '/health', auth=False, raw=True, allow=tuple(range(400, 600)))
    except CliError as error:
        if error.exit_code == EXIT_UNAVAILABLE:
            return
        raise
    finally:
        probe.close()
    raise CliError(f'A Faxbot server answers at {current.url}. Stop it, then run this command again.', EXIT_RUNNING)


@admin.command('status')
def admin_status():
    """Show whether the database is up to date, whether settings are saved, and how many records there are."""
    from ..local import status, stopped
    installation = _installation()
    with stopped(installation, _probe):
        result = status(installation)

    def human(out):
        configuration = result.get('configuration') or {}
        key_state = configuration.get('installation_key_set')
        # The schema revision is in --json output only.
        out.fields([('Database', result['database']), ('Data folder', result['data_dir']),
                    ('Database up to date', result['schema_current']),
                    ('Configuration saved', bool(configuration)),
                    ('Settings changes waiting for restart', configuration.get('restart_pending')),
                    ('Installation key (API_KEY) set', 'unknown (key file not found)'
                     if configuration and not configuration.get('key_file_found') else key_state)])
        labels = {'users': 'Users', 'owners': 'Users with the Owner role', 'integrations': 'Integrations',
                  'api_keys': 'API keys (not revoked)', 'active_sessions': 'Active sessions', 'mailboxes': 'Mailboxes',
                  'sent_faxes': 'Sent faxes', 'received_faxes': 'Received faxes'}
        out.table(['Records', 'Count'], [[labels[name], '-' if count is None else count]
                                          for name, count in result['counts'].items()])
        if not result['schema_current']:
            out.line('The database needs an upgrade; run faxbot system migrate before starting Faxbot.')
    state.out().result(result, human)


@admin.command('migrate')
def admin_migrate():
    """Update the database after you install a new version of Faxbot."""
    from ..local import migrate, stopped
    installation = _installation()
    with stopped(installation, _probe):
        result = migrate(installation)
    # Revisions are in --json output only.
    state.out().result(result, lambda out: out.line('Database upgraded.' if result['changed']
                                                    else 'The database is up to date.'))


@admin.command('recover-owner')
def admin_recover_owner(yes: bool = typer.Option(False, '--yes', '-y', help='Do not ask for confirmation.')):
    """Recover owner access when no owner can sign in: create a new installation key and show it once.

    Run it while Faxbot is stopped. Anything that used the old installation key stops working. Then start Faxbot and create an owner with faxbot access owner enroll.
    """
    from ..local import recover_owner, stopped
    if not yes and (state.out().json_mode or state.out().quiet):
        raise CliError('Add --yes to confirm when using --json or --quiet.')
    installation = _installation()
    with stopped(installation, _probe):
        if not yes:
            typer.confirm('This replaces the installation key (API_KEY). Anything using the current key stops '
                          'working. Continue?', abort=True)
        secret, pending = recover_owner(installation)
    out = state.out()
    current = state.current()
    out.result({'installation_key': secret, 'active_after_restart': pending}, lambda o: o.line(
        'A new installation key is saved.' + (' It takes effect with the settings changes waiting for a restart.'
                                              if pending else '')))
    out.secret('Installation key (shown only once)', secret)
    out.line('Store it somewhere safe, such as a password manager. Next:')
    out.line('  1. Start Faxbot.')
    out.line(f'  2. Run: FAXBOT_API_KEY=<the key above> faxbot --url {current.url} access owner enroll '
             '--login <name> --name "<Your Name>"')
    out.line('  3. Sign in to the console with that name and the temporary password, and choose a new password.')


@admin.command('backup')
def admin_backup(folder: str = typer.Argument(..., help='New, empty folder for the backup.')):
    """Copy everything Faxbot needs to a new folder: the database, the data folder and the installation keys, with checksums to check them later.

    The copy holds private keys and fax documents, so keep it as safe as the installation itself.
    """
    from ..local import backup, stopped
    installation = _installation()
    with stopped(installation, _probe):
        result = backup(installation, folder)
    state.out().result(result, lambda out: out.line(
        f"Backup saved in {result['folder']}: {result['database']}, {result['data_files']} data files and the "
        f"installation key{' and direct delivery key' if result['direct_delivery_key'] else ''}."))


@admin.command('restore')
def admin_restore(folder: str = typer.Argument(..., help='Backup folder made by faxbot system backup.'),
                  force: bool = typer.Option(False, '--force', help='Replace the database, data folder and keys '
                                                                    'already on this computer.')):
    """Restore a backup after checking every file in it."""
    from ..local import restore, stopped
    installation = _installation()
    with stopped(installation, _probe, create=True):
        result = restore(installation, folder, force=force)

    def human(out):
        out.line(f"Restored {result['database']} and {result['data_files']} data files from the backup made "
                 f"{local_time(result.get('created_at'))}.")
        if result['needs_upgrade']:
            out.line('Run faxbot system migrate before starting Faxbot.')
    state.out().result(result, human)
