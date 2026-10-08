"""Connectors: email mailboxes and folders that bring documents in or send faxes, each document once.

Lives at `faxbot numbers connectors`. Secrets are never taken from the command
line: Faxbot asks for them without showing what you type, or reads a key file.
"""
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..errors import CliError, EXIT_FAILURE
from ..output import local_time, text


connectors = typer.Typer(help='Email mailboxes and folders that bring documents into Faxbot or send faxes, each '
                              'document only once.', no_args_is_help=True)

SECRET_PROMPTS = {
    'password': ('password', 'Mailbox password or app password'),
    'microsoft_app': ('client_secret', 'Client secret of the Microsoft Entra app'),
    'oauth_refresh': ('client_secret', 'Client secret'),
}


def _find(api, name):
    items = api.get('/intake/sources')['connectors']
    matches = [item for item in items if item['id'] == name or item['name'].casefold() == name.casefold()]
    if len(matches) != 1:
        raise CliError(f"No single connector is called '{name}'. See 'faxbot numbers connectors list'.")
    return matches[0]


def _counts(item):
    counts = item.get('counts') or {}
    return f"{counts.get('items', 0)} items, {counts.get('duplicates', 0)} seen again, {counts.get('refused', 0)} refused"


@connectors.command('list')
def connectors_list():
    """List connectors with what each does, its status and its counts."""
    items = state.api().get('/intake/sources')['connectors']
    state.out().result(items, lambda out: out.table(
        ['Name', 'What it does', 'Status', 'Last check', 'Counts'],
        [[item['name'], item['what'], item['status'], local_time(item.get('last_checked_at')), _counts(item)]
         for item in items], empty='No connectors yet. Add one with: faxbot numbers connectors add'))


def _resolve_mailbox(choices, name):
    matches = [box for box in choices['mailboxes'] if box['id'] == name or box['label'].casefold() == name.casefold()]
    if len(matches) != 1:
        raise CliError(f"No single mailbox is called '{name}'. See 'faxbot numbers mailboxes list'.")
    return matches[0]['id']


def _resolve_senders(choices, entries):
    senders = []
    for entry in entries:
        address, _, login = entry.partition('=')
        if not address or not login:
            raise CliError(f"Write each sender as address=login, such as jane@example.com=jane, not '{entry}'.")
        matches = [person for person in choices['people']
                   if login.casefold() in ((person.get('login') or '').casefold(), person['id'])]
        if len(matches) != 1:
            raise CliError(f"No single Faxbot person signs in as '{login}'. See 'faxbot access users list'.")
        senders.append({'address': address.strip(), 'principal_id': matches[0]['id']})
    return senders


def _secret(sign_in, service_account_file, ask):
    if sign_in == 'google_service_account':
        if not service_account_file:
            raise CliError('Give the service account key file with --service-account-file.')
        try:
            return {'service_account': Path(service_account_file).read_text(encoding='utf-8')}
        except OSError:
            raise CliError(f'Faxbot cannot read {service_account_file}.') from None
    name, label = SECRET_PROMPTS[sign_in]
    secret = {name: typer.prompt(label, hide_input=True)} if ask else {}
    if sign_in == 'oauth_refresh' and ask:
        secret['refresh_token'] = typer.prompt('Refresh token', hide_input=True)
    return secret


@connectors.command('add')
def connectors_add(
        name: str = typer.Argument(..., help='A name for the connector, such as "Scanner share" or "Email to fax".'),
        kind: str = typer.Option(..., '--kind', help='email (a mailbox) or folder (a folder inside the Faxbot '
                                                    'container).'),
        direction: str = typer.Option('receive', '--direction', help='receive brings documents in; send faxes them.'),
        mailbox: str = typer.Option(None, '--mailbox', help='Mailbox that receives documents with no sidecar file.'),
        path: str = typer.Option(None, '--path', help='Folder inside the Faxbot container, such as /scans.'),
        settle_seconds: int = typer.Option(None, '--settle-seconds',
                                           help='Seconds a file must stay unchanged before Faxbot reads it.'),
        sidecar_minutes: int = typer.Option(None, '--sidecar-minutes',
                                            help='Minutes to wait for the file with the fax number before giving up.'),
        service: str = typer.Option('other', '--service', help='microsoft365, google or other.'),
        address: str = typer.Option(None, '--address', help='The mailbox address, such as fax@example.com.'),
        username: str = typer.Option(None, '--username', help='Sign-in name, if not the mailbox address.'),
        imap_host: str = typer.Option(None, '--mail-server', help='Incoming mail server, unless the service sets it.'),
        imap_port: int = typer.Option(None, '--mail-port', help='Incoming mail server port (993 unless you change '
                                                                'it).'),
        folder: str = typer.Option(None, '--folder', help='Mail folder Faxbot checks (INBOX unless you change it).'),
        processed_folder: str = typer.Option(None, '--processed-folder',
                                             help='Mail folder Faxbot moves handled messages to.'),
        sign_in: str = typer.Option(None, '--sign-in', help='password, microsoft_app, google_service_account or '
                                                            'oauth_refresh.'),
        tenant_id: str = typer.Option(None, '--tenant-id', help='Microsoft 365 directory (tenant) ID.'),
        client_id: str = typer.Option(None, '--client-id', help='Application (client) ID.'),
        token_url: str = typer.Option(None, '--token-url', help='Token address, for oauth_refresh.'),
        service_account_file: str = typer.Option(None, '--service-account-file',
                                                 help='Google service account key file (JSON).'),
        smtp_host: str = typer.Option(None, '--outgoing-server', help='Outgoing mail server for replies, unless the '
                                                                      'service sets it.'),
        smtp_port: int = typer.Option(None, '--outgoing-port', help='Outgoing mail server port.'),
        smtp_security: str = typer.Option(None, '--outgoing-security', help='tls or starttls.'),
        checked_by: str = typer.Option(None, '--checked-by', help='Name your mail server writes when it checks '
                                                                  'senders, such as mx.google.com.'),
        senders: list[str] = typer.Option(None, '--sender', help='Who may send faxes by email: address=login. '
                                                                'Repeat for more.'),
        check_seconds: int = typer.Option(None, '--check-seconds', help='How often to check, in seconds.'),
        ask_secret: bool = typer.Option(True, '--ask-secret/--no-ask-secret',
                                        help='Ask for the password or client secret without showing it.')):
    """Add a connector. Secrets are asked for without showing them, never taken from the command line."""
    api = state.api()
    settings = {key: value for key, value in {
        'path': path, 'settle_seconds': settle_seconds, 'sidecar_minutes': sidecar_minutes, 'provider': service,
        'address': address, 'username': username, 'imap_host': imap_host, 'imap_port': imap_port, 'folder': folder,
        'processed_folder': processed_folder, 'sign_in': sign_in, 'tenant_id': tenant_id, 'client_id': client_id,
        'token_url': token_url, 'smtp_host': smtp_host, 'smtp_port': smtp_port, 'smtp_security': smtp_security,
        'checked_by': checked_by, 'check_seconds': check_seconds}.items() if value is not None}
    if kind == 'folder':
        settings = {key: settings[key] for key in ('path', 'settle_seconds', 'sidecar_minutes', 'check_seconds')
                    if key in settings}
    choices = api.get('/intake/sources/choices')
    if mailbox:
        settings['mailbox_id'] = _resolve_mailbox(choices, mailbox)
    body = {'name': name, 'kind': kind, 'direction': direction, 'settings': settings,
            'senders': _resolve_senders(choices, senders or []) if direction == 'send' and kind == 'email' else []}
    if kind == 'email':
        presets = {item['id']: item for item in choices['providers']}
        method = sign_in or presets.get(service, {}).get('sign_in') or 'password'
        body['secret'] = _secret(method, service_account_file, ask_secret)
    created = api.post('/intake/sources', json=body)
    state.out().result(created, lambda out: out.line(
        f"Connector {created['name']} added. Check it with: faxbot numbers connectors test \"{created['name']}\""))


@connectors.command('update')
def connectors_update(name: str = typer.Argument(..., help='The connector name.'),
                      new_name: str = typer.Option(None, '--name', help='New name.'),
                      check_seconds: int = typer.Option(None, '--check-seconds', help='How often to check, in '
                                                                                      'seconds.'),
                      mailbox: str = typer.Option(None, '--mailbox', help='Mailbox for documents with no sidecar.'),
                      senders: list[str] = typer.Option(None, '--sender', help='Replace who may send faxes by email: '
                                                                              'address=login. Repeat for more.'),
                      new_secret: bool = typer.Option(False, '--new-secret', help='Ask for a new password or client '
                                                                                  'secret without showing it.'),
                      service_account_file: str = typer.Option(None, '--service-account-file',
                                                               help='A new Google service account key file.')):
    """Change a connector. Settings you leave out stay as they are."""
    api = state.api()
    current = _find(api, name)
    body = {'version': current['version']}
    if new_name:
        body['name'] = new_name
    changes = {}
    if check_seconds is not None:
        changes['check_seconds'] = check_seconds
    if mailbox or senders:
        choices = api.get('/intake/sources/choices')
        if mailbox:
            changes['mailbox_id'] = _resolve_mailbox(choices, mailbox)
        if senders:
            body['senders'] = _resolve_senders(choices, senders)
    if changes:
        body['settings'] = changes
    if new_secret or service_account_file:
        body['secret'] = _secret(current['settings'].get('sign_in', 'password'), service_account_file, new_secret)
    changed = api.put('/intake/sources/' + segment(current['id']), json=body)
    state.out().result(changed, lambda out: out.line(f"Connector {changed['name']} changed."))


@connectors.command('test')
def connectors_test(name: str = typer.Argument(..., help='The connector name.')):
    """Sign in to the mailbox or open the folder and look, without changing anything."""
    api = state.api()
    connector = _find(api, name)
    result = api.post(f"/intake/sources/{segment(connector['id'])}/test")
    state.out().result(result, lambda out: out.line(result.get('detail') or ''))
    if not result.get('ok'):
        raise typer.Exit(EXIT_FAILURE)  # the line above already says what failed, in --json too


@connectors.command('pause')
def connectors_pause(name: str = typer.Argument(..., help='The connector name.')):
    """Stop checking a connector. A connector that sends faxes loses its sending key until you resume it."""
    api = state.api()
    connector = _find(api, name)
    result = api.post(f"/intake/sources/{segment(connector['id'])}/pause")
    state.out().result(result, lambda out: out.line(f"{result['name']}: {result['status']}"))


@connectors.command('resume')
def connectors_resume(name: str = typer.Argument(..., help='The connector name.')):
    """Check a connector again. A connector that sends faxes gets a new sending key."""
    api = state.api()
    connector = _find(api, name)
    result = api.post(f"/intake/sources/{segment(connector['id'])}/resume")
    state.out().result(result, lambda out: out.line(f"{result['name']} is checked again."))


@connectors.command('remove')
def connectors_remove(name: str = typer.Argument(..., help='The connector name.')):
    """Remove a connector. What it brought in or sent stays listed."""
    api = state.api()
    connector = _find(api, name)
    result = api.delete('/intake/sources/' + segment(connector['id']))
    state.out().result(result, lambda out: out.line(f"Connector {connector['name']} removed."))


@connectors.command('items')
def connectors_items(name: str = typer.Argument(None, help='Only this connector.'),
                     limit: int = typer.Option(100, '--limit', min=1, max=500, help='How many to show.')):
    """List what connectors brought in or sent, newest first, with anything seen again or refused."""
    params = {'limit': limit, **({'connector': name} if name else {})}
    result = state.api().get('/intake/sources/items', params=params)
    items = result.get('items', [])

    def human(out):
        out.table(['Arrived', 'Connector', 'What', 'From', 'Fax number', 'Status', 'Reply'],
                  [[local_time(item.get('received_at') or item.get('created_at')), item.get('connector'),
                    text(item.get('what')), text(item.get('sender_name') or item.get('sender')),
                    text(item.get('to_number')), item['status'], text(item.get('reply'))] for item in items],
                  empty='Nothing has come through a connector yet.')
        seen = sum(item['duplicates'] for item in items)
        refused = sum(1 for item in items if item['refused'])
        out.line(f'{seen} seen again and never handled twice; {refused} refused.')
    state.out().result(result, human)


@connectors.command('fax')
def connectors_fax(fax_id: str = typer.Argument(..., help="Fax ID from 'faxbot sent list'.")):
    """Show who asked for a fax that came in by email or from a folder."""
    result = state.api().get(f'/intake/sources/faxes/{segment(fax_id)}')
    state.out().result(result, lambda out: out.line(result['sentence']))


@connectors.command('choices')
def connectors_choices():
    """Show the mailboxes, people and mail services you can use when adding a connector."""
    result = state.api().get('/intake/sources/choices')

    def human(out):
        out.table(['Mailbox', 'Its fax number'], [[box['label'], text(box.get('number'))]
                                                  for box in result['mailboxes']], empty='No mailboxes yet.')
        out.table(['Person', 'Signs in as'], [[person['name'], person.get('login')] for person in result['people']],
                  empty='No people yet.')
        for provider in result['providers']:
            out.line(f"{provider['id']}: {provider['guidance']}")
    state.out().result(result, human)

