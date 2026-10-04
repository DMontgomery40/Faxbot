"""The work queue: who owns each received document, acknowledgement targets, evidence and imports."""
import json
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..errors import CliError, EXIT_NOT_FOUND
from ..output import local_time, parse_time
from .fax import _report_saved, save_document

work = typer.Typer(help='The work queue: received documents with an owner, an acknowledgement target and a '
                        'history.', no_args_is_help=True)


def short_time(value):
    """3 Oct 14:05 in the local time zone."""
    moment = parse_time(value)
    if moment is None:
        return '-'
    local = moment.astimezone()
    return f'{local.day} {local:%b %H:%M}'


def state_sentence(item):
    """The server's sentence, with any time in it shown in local time."""
    if item.get('state_key') == 'assigned' and item.get('due_at'):
        owner = (item.get('owner') or {}).get('name') or 'someone'
        return f"Assigned to {owner}; acknowledge by {short_time(item['due_at'])}."
    return item.get('state_text') or '-'


def duplicate_sentence(item):
    duplicate = item.get('duplicate_of')
    return f"Same document as the one received {short_time(duplicate['available_at'])}." if duplicate else None


def _item(api, item_id):
    """An owners-list item, given its own ID or the received fax's ID."""
    try:
        return api.get('/work/' + segment(item_id))
    except CliError as failure:
        if failure.status != 404:
            raise
    found = api.get('/work', params={'inbound_fax_id': item_id, 'limit': 1}).get('items', [])
    if not found:
        raise CliError("No received fax you can see has that ID. See faxbot received list --ids.", EXIT_NOT_FOUND)
    return found[0]


def _person(people, wanted, *, where):
    wanted_key = wanted.strip().lower()
    for person in people:
        if wanted_key in {person['id'].lower(), (person.get('login') or '').lower(), (person.get('name') or '').lower()}:
            return person
    names = ', '.join(person.get('login') or person['name'] for person in people) or 'nobody yet'
    raise CliError(f"{wanted} cannot see {where}, or there is no such person. People who can: {names}.")


def _fields(item):
    owner = (item.get('owner') or {}).get('name')
    return [('From', item.get('from_number')), ('To', item.get('to_number')), ('Mailbox', item.get('mailbox')),
            ('Arrived', local_time(item.get('available_at'))), ('Pages', item.get('pages')),
            ('State', state_sentence(item)), ('Owner', owner), ('Target', item.get('due_text')),
            ('Due', local_time(item.get('due_at'))), ('Same document', duplicate_sentence(item)),
            ('Done note', item.get('done_note'))]


@work.command('list')
def work_list(mine: bool = typer.Option(False, '--mine', help='Only items you own that are not done.'),
              unassigned: bool = typer.Option(False, '--unassigned', help='Only open items without an owner.'),
              overdue: bool = typer.Option(False, '--overdue', help='Only open items past their target.'),
              mailbox: str = typer.Option(None, '--mailbox', help='Only items in this mailbox.'),
              limit: int = typer.Option(100, '--limit', min=1, max=200, help='How many items to show.'),
              ids: bool = typer.Option(False, '--ids', help='Also show item IDs, for assign, acknowledge, done, reopen, history and export.')):
    """List received faxes with their owner and state: open ones first, soonest target first."""
    chosen = [name for name, flag in (('mine', mine), ('unassigned', unassigned), ('overdue', overdue)) if flag]
    if len(chosen) > 1:
        raise CliError('Choose one of --mine, --unassigned or --overdue.')
    result = state.api().get('/work', params={'view': chosen[0] if chosen else 'all', 'mailbox': mailbox,
                                              'limit': limit})
    items = result.get('items', [])
    state.out().result(result, lambda out: out.table(
        (['Item ID'] if ids else []) + ['From', 'Mailbox', 'Arrived', 'Owner', 'Due', 'State'],
        [([item['id']] if ids else []) + [item.get('from_number'), item.get('mailbox') or item.get('to_number'),
                                          local_time(item.get('available_at')),
                                          (item.get('owner') or {}).get('name') or '-',
                                          local_time(item.get('due_at')), state_sentence(item)]
         for item in items], empty='No work items.'))


COUNT_SENTENCES = (('unassigned', 'waiting for an owner'), ('mine', 'assigned to you and not done'),
                   ('overdue', 'past their acknowledgement target'), ('open', 'open'),
                   ('acknowledged', 'acknowledged'), ('done', 'done'))


def received_counts():
    """Count the received faxes you can see in each state, such as waiting for an owner or overdue."""
    counts = state.api().get('/work/counts')

    def human(out):
        for key, words in COUNT_SENTENCES:
            number = counts.get(key) or 0
            out.line(f"{number} received {'fax is' if number == 1 else 'faxes are'} {words}.")
    state.out().result(counts, human)


@work.command('show')
def work_show(item_id: str = typer.Argument(..., help="A received fax's ID, from 'faxbot received list --ids' or 'faxbot received owners --ids'.")):
    """Show who has owned a received fax and everything that happened to it."""
    api = state.api()
    item = _item(api, item_id)
    history = api.get(f"/work/{segment(item['id'])}/history")

    def human(out):
        out.fields(_fields(item))
        if item.get('owner_can_see') is False:
            out.line('The owner can no longer see this document; assign someone else.')
        out.table(['When', 'What happened'], [[local_time(event['occurred_at']), event['text']]
                                              for event in history.get('events', [])], empty='No history yet.')
    state.out().result({**item, 'history': history.get('events', [])}, human)


@work.command('assign')
def work_assign(item_id: str = typer.Argument(..., help="A received fax's ID, from 'faxbot received list --ids' or 'faxbot received owners --ids'."),
                user: str = typer.Argument(..., help='The new owner: their login or name.')):
    """Give a received fax to an owner. They must already be able to see it."""
    api = state.api()
    item = _item(api, item_id)
    people = api.get(f"/work/{segment(item['id'])}/assignees").get('people', [])
    person = _person(people, user, where='this document')
    result = api.post(f"/work/{segment(item['id'])}/assign", json={'principal_id': person['id'],
                                                                'version': item['version']})
    state.out().result(result, lambda out: out.line(state_sentence(result)))


@work.command('acknowledge')
def work_acknowledge(item_id: str = typer.Argument(..., help="A received fax's ID, from 'faxbot received list --ids' or 'faxbot received owners --ids'.")):
    """Acknowledge a received fax you own."""
    api = state.api()
    item = _item(api, item_id)
    result = api.post(f"/work/{segment(item['id'])}/acknowledge", json={'version': item['version']})
    state.out().result(result, lambda out: out.line(state_sentence(result)))


@work.command('done')
def work_done(item_id: str = typer.Argument(..., help="A received fax's ID, from 'faxbot received list --ids' or 'faxbot received owners --ids'."),
              note: str = typer.Option(..., '--note', help='What was done, up to 200 characters.')):
    """Mark a received fax done, with a short note."""
    api = state.api()
    item = _item(api, item_id)
    result = api.post(f"/work/{segment(item['id'])}/done", json={'note': note, 'version': item['version']})
    state.out().result(result, lambda out: out.line(state_sentence(result)))


@work.command('reopen')
def work_reopen(item_id: str = typer.Argument(..., help="A received fax's ID, from 'faxbot received list --ids' or 'faxbot received owners --ids'.")):
    """Reopen a received fax marked done. Its owner acknowledges it again."""
    api = state.api()
    item = _item(api, item_id)
    result = api.post(f"/work/{segment(item['id'])}/reopen", json={'version': item['version']})
    state.out().result(result, lambda out: out.line(state_sentence(result)))


@work.command('export')
def work_export(item_id: str = typer.Argument(..., help="A received fax's ID, from 'faxbot received list --ids' or 'faxbot received owners --ids'."),
                output: str = typer.Option(None, '--output', '-o', help="Zip file to write. Use '-' for standard "
                                                                        'output.'),
                force: bool = typer.Option(False, '--force', help='Replace the file if it exists.')):
    """Download a received fax's record as a zip file: its history and, if you may read documents, the fax itself."""
    api = state.api()
    response = api.get(f"/work/{segment(_item(api, item_id)['id'])}/export", raw=True,
                       headers={'Accept': 'application/zip'})
    _report_saved(save_document(response, output, f'work-{item_id}-evidence.zip', force), len(response.content))


@work.command('settings')
def work_settings(acknowledge_hours: int = typer.Option(None, '--acknowledge-hours', min=0, max=8760,
                                                        help="The installation's acknowledgement target in hours "
                                                             '(0 for none). An operational target, not a legal '
                                                             'deadline.'),
                  mailbox: str = typer.Option(None, '--mailbox', help='Change this mailbox, by name.'),
                  hours: int = typer.Option(None, '--hours', min=0, max=8760,
                                            help="With --mailbox: this mailbox's acknowledgement target in hours (0 for none)."),
                  installation_target: bool = typer.Option(False, '--use-installation-target',
                                                           help='With --mailbox: use the installation-wide acknowledgement target for this mailbox.'),
                  backup: str = typer.Option(None, '--backup', help='With --mailbox: the backup person who takes over faxes not acknowledged in time.'),
                  no_backup: bool = typer.Option(False, '--no-backup', help='With --mailbox: remove the backup.')):
    """Show or change how soon received faxes should be acknowledged, and who covers missed ones. New faxes use the targets in place when they arrive."""
    api = state.api()
    changed = []
    if acknowledge_hours is not None:
        current = api.get('/admin/settings')
        api.put('/admin/settings', json={'work_acknowledge_hours': acknowledge_hours,
                                         'expected_revision_id': current['_meta']['desired_revision_id']})
        changed.append('installation target')
    if (hours is not None or installation_target or backup or no_backup) and not mailbox:
        raise CliError('Add --mailbox to say which mailbox to change.')
    settings = api.get('/work/settings')
    if mailbox:
        row = next((item for item in settings.get('mailboxes', []) if item['label'].lower() == mailbox.lower()), None)
        if row is None:
            raise CliError(f"There is no mailbox named '{mailbox}'.")
        entry = {'mailbox_id': row['mailbox_id'], 'acknowledge_hours': row['acknowledge_hours'],
                 'backup_principal_id': (row.get('backup') or {}).get('id'), 'version': row['version']}
        if hours is not None:
            entry['acknowledge_hours'] = hours
        if installation_target:
            entry['acknowledge_hours'] = None
        if backup:
            entry['backup_principal_id'] = _person(row.get('people', []), backup,
                                                   where=f"every document in {row['label']}")['id']
        if no_backup:
            entry['backup_principal_id'] = None
        settings = api.put('/work/settings', json={'mailboxes': [entry]})
        changed.append(row['label'])

    def target(value):
        if value is None:
            return 'installation target'
        return 'none' if value == 0 else f'{value} hours'

    def human(out):
        if changed:
            out.line('Saved: ' + ', '.join(changed) + '.')
        hours_now = settings.get('acknowledge_hours') or 0
        out.line('Installation target: ' + (f'{hours_now} hours' if hours_now else 'none') +
                 '. This is your team\'s operational target, not a legal deadline.')
        out.table(['Mailbox', 'Target', 'Backup'],
                  [[row['label'], target(row['acknowledge_hours']), (row.get('backup') or {}).get('name') or '-']
                   for row in settings.get('mailboxes', [])], empty='No mailboxes yet.')
    state.out().result(settings, human)


def import_document(file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                                help='The PDF to import.'),
                    source: str = typer.Option(..., '--source', help='Name of the source system, for example '
                                                                     'case-system.'),
                    operation_id: str = typer.Option(..., '--id', help="The document's ID in that system. "
                                                                       'Importing the same ID again does not '
                                                                       'create a second document.'),
                    revision: str = typer.Option(None, '--revision', help='A new version of a document already '
                                                                          'imported under this ID.'),
                    received_at: str = typer.Option(None, '--received-at', help='When that system received it, '
                                                                                'for example 2026-10-03T14:05:00Z.'),
                    to_number: str = typer.Option(None, '--to', help='The fax number it was sent to; decides the '
                                                                     'mailbox.'),
                    from_number: str = typer.Option(None, '--from', help='The fax number it came from.'),
                    pages: int = typer.Option(None, '--pages', min=1, help='Page count, as the source system reported it.')):
    """Import a PDF from another system as if it were a received fax."""
    manifest = {key: value for key, value in {
        'source_system': source, 'operation_id': operation_id, 'revision': revision,
        'source_received_at': received_at, 'to_number': to_number, 'from_number': from_number,
        'pages': pages}.items() if value is not None}
    with file.open('rb') as handle:
        result = state.api().post('/imports', data={'manifest': json.dumps(manifest)},
                                  files={'file': (file.name, handle, 'application/pdf')})
    message = {'received': 'Document imported.',
               'duplicate': 'Already imported; nothing new was created.'}.get(result.get('status'), 'Done.')
    state.out().result(result, lambda out: out.line(message))
