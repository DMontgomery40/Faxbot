"""Local administration of a stopped installation on this computer.

These commands read the same settings as the server (DATABASE_URL, FAX_DATA_DIR,
FAXBOT_INSTALLATION_KEY_PATH, FAXBOT_DIRECT_KEY_PATH) and refuse to run while a
Faxbot server answers on the configured address or holds the installation.
"""
import os

import typer

from .. import state
from ..client import Api
from ..errors import EXIT_RUNNING, EXIT_UNAVAILABLE, CliError
from ..output import local_time

admin = typer.Typer(help='Run a stopped installation on this computer: recover owner access, back up, restore, '
                         'upgrade the database and check its state.', no_args_is_help=True)


def register(app):
    app.add_typer(admin, name='admin')


@admin.callback()
def admin_options(ctx: typer.Context,
                  database_url: str = typer.Option(None, '--database-url', envvar='DATABASE_URL', show_envvar=True,
                                                   metavar='URL', show_default=False,
                                                   help='Installation database. Default: ./faxbot.db, as the server.'),
                  data_dir: str = typer.Option(None, '--data-dir', envvar='FAX_DATA_DIR', metavar='FOLDER',
                                               help='Installation data folder. Default: ./faxdata, as the server.'),
                  key_file: str = typer.Option(None, '--key-file', envvar='FAXBOT_INSTALLATION_KEY_PATH', metavar='FILE',
                                               help='Installation encryption key. Default: .configuration.key in '
                                                    'the data folder.'),
                  direct_key_file: str = typer.Option(None, '--direct-key-file', envvar='FAXBOT_DIRECT_KEY_PATH',
                                                      metavar='FILE', help='Direct delivery key. Default: '
                                                                           '.direct-identity.key in the data folder.')):
    state.current().admin_options = {'database_url': database_url, 'data_dir': data_dir, 'key_path': key_file,
                                     'direct_key_path': direct_key_file}


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
    """Show the database schema version, saved configuration state and record counts."""
    from ..local import status, stopped
    installation = _installation()
    with stopped(installation, _probe):
        result = status(installation)

    def human(out):
        configuration = result.get('configuration') or {}
        key_state = configuration.get('installation_key_set')
        out.fields([('Database', result['database']), ('Data folder', result['data_dir']),
                    ('Schema version', result['schema_revision'] or 'not created'),
                    ('Schema up to date', result['schema_current']),
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
            out.line('Run faxbot admin migrate to upgrade the database.')
    state.out().result(result, human)


@admin.command('migrate')
def admin_migrate():
    """Upgrade the installation database to this version of Faxbot."""
    from ..local import migrate, stopped
    installation = _installation()
    with stopped(installation, _probe):
        result = migrate(installation)
    state.out().result(result, lambda out: out.line(
        f"Database upgraded from {result['before'] or 'empty'} to {result['after']}." if result['changed']
        else f"The database is already up to date ({result['after']})."))


@admin.command('recover-owner')
def admin_recover_owner(yes: bool = typer.Option(False, '--yes', '-y', help='Do not ask for confirmation.')):
    """Recover owner access: set a fresh installation key and show it once.

    For when no owner can sign in and the installation key (API_KEY) is empty or
    lost. Run it with Faxbot stopped. Anything using the old installation key
    stops working. Then start Faxbot and create an owner with faxbot owner enroll.
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
    out.line(f'  2. Run: FAXBOT_API_KEY=<the key above> faxbot --url {current.url} owner enroll '
             '--login <name> --name "<Your Name>"')
    out.line('  3. Sign in to the console with that name and the temporary password, and choose a new password.')


@admin.command('backup')
def admin_backup(folder: str = typer.Argument(..., help='New, empty folder for the backup.')):
    """Back up the database, the data folder and the installation keys, with a manifest of checksums.

    The backup contains private keys and fax documents. Keep it as safe as the
    installation itself.
    """
    from ..local import backup, stopped
    installation = _installation()
    with stopped(installation, _probe):
        result = backup(installation, folder)
    state.out().result(result, lambda out: out.line(
        f"Backup saved in {result['folder']}: {result['database']}, {result['data_files']} data files and the "
        f"installation key{' and direct delivery key' if result['direct_delivery_key'] else ''}."))


@admin.command('restore')
def admin_restore(folder: str = typer.Argument(..., help='Backup folder made by faxbot admin backup.'),
                  force: bool = typer.Option(False, '--force', help='Replace an existing database, data folder and '
                                                                    'keys.')):
    """Restore a backup after checking every file against its manifest."""
    from ..local import restore, stopped
    installation = _installation()
    with stopped(installation, _probe, create=True):
        result = restore(installation, folder, force=force)

    def human(out):
        out.line(f"Restored {result['database']} and {result['data_files']} data files from the backup made "
                 f"{local_time(result.get('created_at'))}.")
        if result['needs_upgrade']:
            out.line('Run faxbot admin migrate before starting Faxbot.')
    state.out().result(result, human)
