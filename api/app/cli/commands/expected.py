"""Expected faxes: what should arrive, from whom and by when; what is missing; and recovery after an outage."""
from datetime import datetime
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time
from .fax import _report_saved, save_document


CODE_HELP = "The expected fax's code, from 'faxbot expected list'."
OUTAGE_HELP = "The outage's code, from 'faxbot expected outage list'."
VIEWS = ('overdue', 'proposed', 'missing', 'conflicts', 'closed', 'all')

expected = typer.Typer(help='Expected faxes: record a fax before it arrives, see what is missing or overdue, confirm '
                            'proposed matches, import open work from another system, and reconcile after that system '
                            'was down.', no_args_is_help=True)
sources = typer.Typer(help='Saved import sources: the file format and which column holds each field.',
                      no_args_is_help=True)
outage = typer.Typer(help='Outage mode: mark a source system down, record what was done by fax or email meanwhile, '
                          'and sort the next export into done, new and held lists.', no_args_is_help=True)
expected.add_typer(sources, name='sources')
expected.add_typer(outage, name='outage')


def _time(value, what):
    """A time typed by a person: with its offset, or a local date and time on this computer."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        raise CliError(f'{what} must be a time like 2026-10-12 14:00 or 2026-10-12T14:00:00-06:00.') from None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.isoformat()


def _state(item):
    """The server's sentence, with the due time in local time."""
    if item.get('state_key') == 'waiting' and item.get('due_at'):
        return f"Waiting; expected by {local_time(item['due_at'])}."
    return item.get('state_text') or '-'


def _get(api, code):
    return api.get('/expected-faxes/' + segment(code))


def _proposal(item, number):
    proposals = item.get('proposals') or []
    if not proposals:
        raise CliError('Nothing is proposed for this expected fax.')
    if number is None:
        if len(proposals) > 1:
            raise CliError(f'{len(proposals)} faxes are proposed; choose one with --proposal (1 to {len(proposals)}).')
        return proposals[0]
    if not 1 <= number <= len(proposals):
        raise CliError(f'Choose --proposal from 1 to {len(proposals)}.')
    return proposals[number - 1]


def _show(out, item, events=()):
    out.fields([('Code', item.get('code')), ('Reference', item.get('reference')), ('Expecting', item.get('kind')),
                ('From', item.get('counterparty')), ('Mailbox', item.get('mailbox')),
                ('Owner', (item.get('owner') or {}).get('name')), ('State', _state(item)),
                ('Due', item.get('due_text')), ('Must include', ', '.join(item.get('required_parts') or []) or None),
                ('Revision', item.get('required_revision')), ('Import source', item.get('source')),
                ('Fax numbers', ', '.join(item.get('fax_numbers') or []) or None),
                ('Subaddress', (item.get('keys') or {}).get('subaddress')),
                ('Email subject', (item.get('keys') or {}).get('email_subject'))])
    if item.get('missing_from_export'):
        out.line("The latest full export no longer lists it; it stays open until you decide.")
    if item.get('conflict'):
        out.line("The import changed this row without a new revision; decide with 'faxbot expected conflict'.")
    proposals = item.get('proposals') or []
    if proposals:
        out.table(['#', 'Received', 'From', 'Why it may be the one'],
                  [[str(index), local_time((proposal.get('fax') or {}).get('received_at')),
                    (proposal.get('fax') or {}).get('from_number') or 'A fax you cannot open', proposal['text']]
                   for index, proposal in enumerate(proposals, start=1)])
    if events:
        out.table(['When', 'What happened'], [[local_time(event['occurred_at']), event['text']] for event in events])


@expected.command('list')
def expected_list(view: str = typer.Option(None, '--show', metavar='WHICH',
                                           help='overdue, proposed (waiting for you to confirm), missing (no longer '
                                                'in the latest export), conflicts, closed or all. Default: waiting.'),
                  mailbox: str = typer.Option(None, '--mailbox', help='Only this mailbox, by name.'),
                  search: str = typer.Option(None, '--search', help='Only references containing this text.'),
                  limit: int = typer.Option(100, '--limit', min=1, max=500, help='How many to show.')):
    """List expected faxes, the ones waiting first, with their state and due time."""
    if view is not None and view not in VIEWS:
        raise CliError(f"Choose --show from: {', '.join(VIEWS)}.")
    api = state.api()
    params = {'view': view or 'waiting', 'limit': limit, 'mailbox': mailbox, 'search': search}
    items = api.get('/expected-faxes', params={key: value for key, value in params.items() if value})['expected']
    counts = api.get('/expected-faxes/counts')

    def human(out):
        out.line(f"{counts.get('waiting', 0)} waiting, {counts.get('overdue', 0)} overdue, "
                 f"{counts.get('proposed', 0)} to confirm.")
        out.table(['Code', 'Reference', 'Expecting', 'From', 'Mailbox', 'State'],
                  [[item['code'], item['reference'], item['kind'], item.get('counterparty') or '-',
                    item.get('mailbox') or '-', _state(item)] for item in items], empty='No expected faxes.')
    state.out().result({'expected': items, 'counts': counts}, human)


@expected.command('show')
def expected_show(code: str = typer.Argument(..., help=CODE_HELP),
                  history: bool = typer.Option(False, '--history', help='Also show everything that happened.')):
    """Show one expected fax, any received faxes proposed as its answer, and optionally its history."""
    api = state.api()
    item = _get(api, code)
    events = api.get(f"/expected-faxes/{segment(item['id'])}/history")['events'] if history else []
    state.out().result({**item, 'history': events} if history else item, lambda out: _show(out, item, events))


@expected.command('add')
def expected_add(reference: str = typer.Argument(..., help='The business reference, such as "PO 483".'),
                 kind: str = typer.Option(..., '--expecting', help='What should arrive, such as "Signed '
                                                                   'acknowledgement".'),
                 mailbox: str = typer.Option(..., '--mailbox', help='The mailbox that owns it, by name.'),
                 sender: str = typer.Option(None, '--from-name', help='Who will send it, such as "Acme Supply".'),
                 numbers: list[str] = typer.Option(None, '--fax-number', help='A fax number it may come from. '
                                                                              'Repeat for more.'),
                 due: str = typer.Option(None, '--due', help='When it is due, such as 2026-10-12 17:00 (local time).'),
                 due_hours: int = typer.Option(None, '--due-hours', min=1, max=8760,
                                               help='Due this many hours from now, instead of --due.'),
                 revision: str = typer.Option(None, '--revision', help='The revision it must be, such as B.'),
                 parts: list[str] = typer.Option(None, '--must-include', help='A part it must include, such as '
                                                                              '"Signature page". Repeat for more.'),
                 subaddress: str = typer.Option(None, '--subaddress', help='The subaddress the sender will dial. '
                                                                           'Default: the reference, when it is digits.'),
                 subject: str = typer.Option(None, '--email-subject', help='Text the email subject will contain, such '
                                                                           'as "PO 483".'),
                 message_id: str = typer.Option(None, '--message-id', help='The Direct or email message ID it will '
                                                                           'carry, when you know it.'),
                 direct_address: str = typer.Option(None, '--direct-address', help='The Direct address it may come '
                                                                                   'from.'),
                 note: str = typer.Option(None, '--note', help='A short description.')):
    """Expect a fax: record what should arrive, from whom and by when, before it arrives."""
    if due and due_hours:
        raise CliError('Choose --due or --due-hours, not both.')
    api = state.api()
    boxes = api.get('/expected-faxes/mailboxes')['mailboxes']
    box = next((entry for entry in boxes if (entry.get('label') or '').casefold() == mailbox.strip().casefold()), None)
    if box is None:
        names = ', '.join(entry['label'] for entry in boxes) or 'none'
        raise CliError(f'You cannot add expected faxes to {mailbox}. Mailboxes you can use: {names}.')
    body = {'reference': reference, 'kind': kind, 'mailbox_id': box['id'], 'counterparty': sender,
            'fax_numbers': numbers or [], 'due_at': _time(due, '--due'), 'due_hours': due_hours,
            'required_revision': revision, 'required_parts': parts or [], 'subaddress': subaddress,
            'email_subject': subject, 'message_id': message_id, 'direct_address': direct_address, 'description': note}
    item = api.post('/expected-faxes', json={key: value for key, value in body.items() if value not in (None, [])})
    state.out().result(item, lambda out: (out.line(f"Expecting {item['reference']} (code {item['code']})."),
                                          _show(out, item)))


@expected.command('confirm')
def expected_confirm(code: str = typer.Argument(..., help=CODE_HELP),
                     number: int = typer.Option(None, '--proposal', min=1,
                                                help="Which proposed fax, as numbered in 'faxbot expected show'.")):
    """Confirm that a proposed received fax is the one expected. This closes it."""
    api = state.api()
    item = _get(api, code)
    chosen = _proposal(item, number)
    item = api.post(f"/expected-faxes/{segment(item['id'])}/confirm",
                    json={'proposal_id': chosen['id'], 'version': item['version']})
    state.out().result(item, lambda out: out.line(_state(item)))


@expected.command('reject')
def expected_reject(code: str = typer.Argument(..., help=CODE_HELP),
                    number: int = typer.Option(None, '--proposal', min=1,
                                               help="Which proposed fax, as numbered in 'faxbot expected show'."),
                    reason: str = typer.Option(None, '--reason', help='Why it is not the one (up to 300 characters).')):
    """Say a proposed received fax is not the one expected. The expected fax keeps waiting."""
    api = state.api()
    item = _get(api, code)
    chosen = _proposal(item, number)
    body = {'proposal_id': chosen['id'], 'version': item['version']}
    if reason:
        body['note'] = reason
    item = api.post(f"/expected-faxes/{segment(item['id'])}/reject", json=body)
    state.out().result(item, lambda out: out.line(_state(item)))


@expected.command('match')
def expected_match(code: str = typer.Argument(..., help=CODE_HELP),
                   fax_id: str = typer.Option(..., '--received-fax',
                                              help="The received fax's ID, from 'faxbot received list --ids'."),
                   note: str = typer.Option(None, '--note', help='Why it is the one (up to 300 characters).')):
    """Link a received fax to an expected fax by hand. This closes it."""
    api = state.api()
    item = _get(api, code)
    body = {'inbound_fax_id': fax_id, 'version': item['version']}
    if note:
        body['note'] = note
    item = api.post(f"/expected-faxes/{segment(item['id'])}/match", json=body)
    state.out().result(item, lambda out: out.line(_state(item)))


def _close(code, outcome, note):
    api = state.api()
    item = _get(api, code)
    item = api.post(f"/expected-faxes/{segment(item['id'])}/close",
                    json={'outcome': outcome, 'note': note, 'version': item['version']})
    state.out().result(item, lambda out: out.line(_state(item)))


@expected.command('cancel')
def expected_cancel(code: str = typer.Argument(..., help=CODE_HELP),
                    reason: str = typer.Option(..., '--reason', help='Why it is no longer expected (up to 300 '
                                                                     'characters).')):
    """Cancel an expected fax that is no longer needed."""
    _close(code, 'cancelled', reason)


@expected.command('done-elsewhere')
def expected_done_elsewhere(code: str = typer.Argument(..., help=CODE_HELP),
                            how: str = typer.Option(..., '--how', help='How it was completed, such as "Confirmed on '
                                                                       'the supplier portal" (up to 300 characters).')):
    """Record that an expected fax was completed another way, such as by phone or on a portal."""
    _close(code, 'completed_elsewhere', how)


@expected.command('conflict')
def expected_conflict(code: str = typer.Argument(..., help=CODE_HELP),
                      keep: bool = typer.Option(False, '--keep', help='Keep the first version and set the change '
                                                                      'aside.'),
                      apply: bool = typer.Option(False, '--apply', help="Use the import's changed version.")):
    """Decide about a row the import changed without a new revision."""
    if keep == apply:
        raise CliError('Choose --keep or --apply.')
    api = state.api()
    item = _get(api, code)
    item = api.post(f"/expected-faxes/{segment(item['id'])}/conflict",
                    json={'choice': 'keep' if keep else 'apply', 'version': item['version']})
    state.out().result(item, lambda out: out.line('Kept the first version.' if keep else "Used the import's version."))


@expected.command('report')
def expected_report(days: int = typer.Option(30, '--days', min=1, max=365, help='How many days back.')):
    """What is still missing, what is overdue, and which received faxes answered no expected fax."""
    report = state.api().get('/expected-faxes/report', params={'days': days})

    def human(out):
        out.line(report['summary'])
        out.table(['Code', 'Reference', 'From', 'Mailbox', 'State'],
                  [[item['code'], item['reference'], item.get('counterparty') or '-', item.get('mailbox') or '-',
                    _state(item)] for item in report['unmatched_expected']], empty='Nothing is missing.')
        out.table(['Received', 'From', 'Mailbox', 'Why it is listed'],
                  [[local_time(entry['received_at']), entry.get('from_number') or '-', entry.get('mailbox') or '-',
                    entry['why']] for entry in report['unmatched_arrivals']],
                  empty='No received fax carried a reference that matched nothing.')
    state.out().result(report, human)


@expected.command('export')
def expected_export(code: str = typer.Argument(..., help=CODE_HELP),
                    output: str = typer.Option(None, '--output', '-o', help="Zip file to write. Use '-' for standard "
                                                                            'output.'),
                    force: bool = typer.Option(False, '--force', help='Replace the file if it exists.')):
    """Download an expected fax's evidence as a zip file: its history, its match and what is missing."""
    api = state.api()
    item = _get(api, code)
    response = api.get(f"/expected-faxes/{segment(item['id'])}/export", raw=True, headers={'Accept': 'application/zip'})
    _report_saved(save_document(response, output, f"expected-{item['code']}-evidence.zip", force),
                  len(response.content))


@expected.command('import')
def expected_import(file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                                help='The export file (CSV or JSON, as the source says).'),
                    source: str = typer.Option(..., '--source', help="The saved import source's name, from "
                                                                     "'faxbot expected sources list'."),
                    full: bool = typer.Option(False, '--full', help='The file lists all open work, so anything '
                                                                    'missing from it is reported.')):
    """Import open work from another system as expected faxes. Importing the same file again adds nothing."""
    with file.open('rb') as handle:
        result = state.api().post('/expected-faxes/imports', data={'source': source, 'full_export': str(full).lower()},
                                  files={'file': (file.name, handle, 'application/octet-stream')})

    def human(out):
        out.line(result['summary'])
        if result.get('replay'):
            out.line('This file was imported before; nothing was added twice.')
        if result.get('note'):
            out.line(result['note'])
        out.table(['Row', 'Problem'], [[str(problem['row']), problem['problem']] for problem in result['problems']])
        if result.get('missing'):
            out.table(['Code', 'No longer in the export'], [[entry['code'], entry['reference']]
                                                             for entry in result['missing']])
        if result.get('reconciled_outage'):
            out.line(f"Sorted against outage {result['reconciled_outage']}: "
                     f"faxbot expected outage show {result['reconciled_outage']}")
    state.out().result(result, human)


@expected.command('imports')
def expected_imports(limit: int = typer.Option(20, '--limit', min=1, max=100, help='How many imports to show.')):
    """List recent imports of expected faxes, newest first, with what each one changed."""
    found = state.api().get('/expected-faxes/imports', params={'limit': limit})['imports']
    state.out().result({'imports': found}, lambda out: out.table(
        ['Imported', 'Source', 'File', 'By', 'What happened'],
        [[local_time(run.get('completed_at') or run.get('created_at')), run.get('source') or '-',
          run.get('file_name') or '-', run.get('imported_by') or '-', run['summary']] for run in found],
        empty='Nothing imported yet.'))


@sources.command('list')
def sources_list():
    """List saved import sources and their column mappings."""
    found = state.api().get('/expected-faxes/sources')['sources']
    state.out().result({'sources': found}, lambda out: out.table(
        ['Name', 'Format', 'Mailbox', 'Columns'],
        [[source['name'], source['format'].upper(), source.get('mailbox') or '-',
          ', '.join(f'{field}={column}' for field, column in sorted(source['mapping'].items()))] for source in found],
        empty='No import sources yet.'))


@sources.command('save')
def sources_save(name: str = typer.Argument(..., help='A name for the source, such as "Open purchase orders".'),
                 file_format: str = typer.Option(..., '--format', help='csv or json.'),
                 columns: list[str] = typer.Option(..., '--column', metavar='FIELD=COLUMN',
                                                   help='Which column holds a field, such as reference="PO Number". '
                                                        'Fields: reference (required), operation_id, revision, kind, '
                                                        'description, counterparty, fax_numbers, direct_address, due, '
                                                        'mailbox, subaddress, email_subject, message_id, '
                                                        'required_parts, required_revision, window_start.'),
                 mailbox: str = typer.Option(None, '--mailbox', help='The mailbox for rows that name none.'),
                 due_hours: int = typer.Option(None, '--due-hours', min=0, max=8760,
                                               help='Hours each row has when it gives no due time.'),
                 subject: str = typer.Option(None, '--subject-pattern', help='How the reference appears in an email '
                                                                             'subject, such as "PO {reference}".'),
                 sub: str = typer.Option(None, '--subaddress-pattern', help='How the reference is dialed as a '
                                                                           'subaddress, such as "{digits}".'),
                 form_field: str = typer.Option(None, '--form-field', help="The partner form field that holds the "
                                                                           'reference.'),
                 revision_field: str = typer.Option(None, '--revision-field', help='The partner form field that holds '
                                                                                   'the revision.')):
    """Save an import source once: its format and which column holds each field. Saving again changes it."""
    if file_format not in ('csv', 'json'):
        raise CliError('Choose --format csv or json.')
    mapping = {}
    for entry in columns:
        field, _, column = entry.partition('=')
        if not field or not column:
            raise CliError(f'{entry} must be FIELD=COLUMN, such as reference="PO Number".')
        mapping[field.strip()] = column.strip()
    api = state.api()
    body = {'name': name, 'format': file_format, 'mapping': mapping, 'due_hours': due_hours,
            'subject_template': subject, 'subaddress_template': sub, 'form_field': form_field,
            'revision_field': revision_field}
    if mailbox:
        boxes = api.get('/expected-faxes/mailboxes')['mailboxes']
        box = next((entry for entry in boxes if (entry.get('label') or '').casefold() == mailbox.casefold()), None)
        if box is None:
            raise CliError(f'You cannot add expected faxes to {mailbox}.')
        body['mailbox_id'] = box['id']
    existing = next((source for source in api.get('/expected-faxes/sources')['sources']
                     if source['name'].casefold() == name.casefold()), None)
    if existing is not None:
        body.update(id=existing['id'], version=existing['version'])
    saved = api.post('/expected-faxes/sources', json={key: value for key, value in body.items() if value is not None})
    state.out().result(saved, lambda out: out.line(f"Saved {saved['name']}."))


def _outage_show(out, found):
    out.fields([('Code', found['code']), ('Source', found.get('source')), ('State', found['text']),
                ('Note', found.get('note'))])
    actions = found.get('actions') or []
    if actions:
        out.table(['When', 'Item', 'What was done', 'How', 'Outcome'],
                  [[local_time(action['occurred_at']), action['operation_id'], action['action'], action['channel'],
                    'done' if action['outcome'] == 'done' else 'may not have gone through'] for action in actions])
    lists = found.get('reconciliation')
    if lists:
        out.line(lists['summary'])
        for title, entries in (('Already done: record it, do not submit it again', lists['already_done']),
                               ('New: submit normally', lists['new']),
                               ('Held for you to decide', lists['unresolved'])):
            out.table(['Item', 'Reference', 'Mailbox', title],
                      [[entry['operation_id'], entry.get('reference') or '-', entry.get('mailbox') or '-',
                        entry['text']] for entry in entries], empty=f'{title}: none.')


@outage.command('list')
def outage_list():
    """List declared outages, the newest first."""
    found = state.api().get('/expected-faxes/outages')['outages']
    state.out().result({'outages': found}, lambda out: out.table(
        ['Code', 'Source', 'State'], [[entry['code'], entry.get('source') or '-', entry['text']] for entry in found],
        empty='No outages recorded.'))


@outage.command('show')
def outage_show(code: str = typer.Argument(..., help=OUTAGE_HELP)):
    """Show an outage: what was done during it and the reconciliation lists."""
    found = state.api().get('/expected-faxes/outages/' + segment(code))
    state.out().result(found, lambda out: _outage_show(out, found))


@outage.command('start')
def outage_start(source: str = typer.Argument(..., help="The import source's name: the system that is down."),
                 since: str = typer.Option(None, '--since', help='When it went down (local time). Default: now.'),
                 note: str = typer.Option(None, '--note', help='A short note, such as "ERP maintenance".')):
    """Mark a source system down. Record what you do by fax or email meanwhile with 'outage record'."""
    body = {'source': source, 'started_at': _time(since, '--since'), 'note': note}
    found = state.api().post('/expected-faxes/outages', json={key: value for key, value in body.items() if value})
    state.out().result(found, lambda out: (out.line(f"{found['source']} is marked down (outage {found['code']})."),
                                           out.line(found['text'])))


@outage.command('end')
def outage_end(code: str = typer.Argument(..., help=OUTAGE_HELP),
               at: str = typer.Option(None, '--at', help='When it came back (local time). Default: now.')):
    """Mark a source system back. Then import its next export to sort the work into three lists."""
    api = state.api()
    current = api.get('/expected-faxes/outages/' + segment(code))
    body = {'version': current['version']}
    if at:
        body['ended_at'] = _time(at, '--at')
    found = api.post(f"/expected-faxes/outages/{segment(current['id'])}/end", json=body)
    state.out().result(found, lambda out: (out.line(found['text']),
                                           out.line('Import the next export with faxbot expected import --full.')))


@outage.command('record')
def outage_record(code: str = typer.Argument(..., help=OUTAGE_HELP),
                  operation_id: str = typer.Option(..., '--id', help="The item's original ID in the source system."),
                  action: str = typer.Option(..., '--did', help='What was done, such as "Order faxed to Acme".'),
                  channel: str = typer.Option(..., '--by', help='fax, email, phone or other.'),
                  revision: str = typer.Option(None, '--revision', help="The item's revision, when it has one."),
                  reference: str = typer.Option(None, '--reference', help='The business reference, such as "PO 483".'),
                  uncertain: bool = typer.Option(False, '--uncertain', help='It may not have gone through.'),
                  fax_id: str = typer.Option(None, '--sent-fax', help="The sent fax's ID, from 'faxbot sent list "
                                                                      "--ids'."),
                  note: str = typer.Option(None, '--evidence', help='Where the evidence is, such as "Fax log page 3".'),
                  at: str = typer.Option(None, '--at', help='When it was done (local time). Default: now.')):
    """Record one action done during an outage against the item's original ID. Faxbot never submits it anywhere."""
    if channel not in ('fax', 'email', 'phone', 'other'):
        raise CliError('Choose --by fax, email, phone or other.')
    body = {'operation_id': operation_id, 'action': action, 'channel': channel, 'revision': revision,
            'reference': reference, 'outcome': 'uncertain' if uncertain else 'done', 'fax_job_id': fax_id,
            'evidence_note': note, 'occurred_at': _time(at, '--at')}
    found = state.api().post(f'/expected-faxes/outages/{segment(code)}/actions',
                             json={key: value for key, value in body.items() if value is not None})
    state.out().result(found, lambda out: out.line(f'Recorded for {operation_id}.'))


@outage.command('reconcile')
def outage_reconcile(code: str = typer.Argument(..., help=OUTAGE_HELP)):
    """Sort the export imported after the outage into done, new and held lists again. Nothing is sent or submitted."""
    found = state.api().post(f'/expected-faxes/outages/{segment(code)}/reconcile')
    state.out().result(found, lambda out: _outage_show(out, found))
