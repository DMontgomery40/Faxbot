"""Case packets, continued: kept originals, what the recipient acknowledged, repairs and checklists.

The commands join the ``cases`` group in ``delivery`` (``faxbot recipients cases ...``).
"""
from contextlib import ExitStack
import json
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time
from ..settings_write import write_settings
from .delivery import cases


checklist = typer.Typer(help="A recipient's checklist of documents: types, dates, versions, and which are required.",
                        no_args_is_help=True)
cases.add_typer(checklist, name='checklist')

STATE_TEXT = {'waiting': 'Waiting to be delivered', 'not_sent': 'Not delivered', 'sent': 'Delivered, not acknowledged',
              'accepted': 'Acknowledged', 'expired': 'Acknowledgement too old', 'invalidated': 'Recipient could not find it'}
HOW_TEXT = {'partner_receipt': "partner's signed receipt", 'work_acknowledged': 'receiving team',
            'received_fax': 'their acknowledgement fax', 'person': 'confirmed by a person'}
WHY_TEXT = {'new': 'new', 'sent': 'not acknowledged yet', 'waiting': 'not delivered yet', 'not_sent': 'not delivered',
            'expired': 'acknowledgement too old', 'invalidated': 'recipient could not find it',
            'references_off': 'recipient wants every document', 'accepted': 'acknowledged', 'repair': 'full packet'}


def state_text(item):
    """A document's state in the console's words."""
    text = STATE_TEXT.get(item.get('state'), item.get('state') or '-')
    if item.get('state') == 'accepted':
        how = HOW_TEXT.get(item.get('accepted_how'))
        text += f" ({how}, {local_time(item.get('accepted_at'))})" if how else ''
    return text


def detail(item):
    parts = [f"version {item['version']}" if item.get('version') else '',
             f"from {item['source']}" if item.get('source') else '']
    return ', '.join(part for part in parts if part) or '-'


def _held(case_id, to):
    return state.api().get(f'/cases/{segment(case_id)}/documents', params={'to': to})


def _choose(case_id, to, references, purpose, *, sent_only):
    """Ledger entries by title or reference, narrowed by purpose; every sent one when none are named."""
    documents = _held(case_id, to).get('documents', [])
    if purpose is not None:
        documents = [item for item in documents if item.get('purpose', '') == purpose]
    if not references:
        chosen = [item for item in documents if item.get('sent_at')] if sent_only else []
        if not chosen:
            raise CliError('Name the documents with --document, by title or reference.')
        return chosen
    chosen = []
    for reference in references:
        found = [item for item in documents if item['title'].casefold() == reference.casefold()
                 or item['reference'].startswith(reference.lower())]
        if not found:
            raise CliError(f"No document '{reference}' was sent for this case to {to}.")
        if len(found) > 1:
            raise CliError(f"Several documents match '{reference}'. Add --purpose to choose one.")
        chosen.append(found[0])
    return chosen


def _documents_out(out, result):
    out.table(['Document', 'Version and source', 'Purpose', 'Pages', 'State'],
              [[item['title'], detail(item), item.get('purpose') or '-', item['pages'], state_text(item)]
               for item in result.get('documents', [])], empty='Nothing has been sent for this case to this recipient.')


@cases.command('accept')
def cases_accept(case_id: str = typer.Argument(..., help='Your case reference.'),
                 to: str = typer.Option(..., '--to', help='Recipient fax number.'),
                 document: list[str] = typer.Option(None, '--document', help='A document, by title or reference. '
                                                    'Repeat for more. Default: every delivered document.'),
                 purpose: str = typer.Option(None, '--purpose', help='Only documents sent for this purpose.'),
                 note: str = typer.Option(None, '--note', help='Who confirmed it and how, for example '
                                          '"Their intake desk confirmed by phone".'),
                 received_fax: str = typer.Option(None, '--received-fax', help='The ID of the fax in which the '
                                                  'recipient acknowledged them, from Received.')):
    """Record that the recipient confirmed it has these documents, so later packets can list them instead of resending."""
    chosen = _choose(case_id, to, document, purpose, sent_only=True)
    body = {'to': to, 'documents': [item['id'] for item in chosen], 'note': note}
    if received_fax:
        body['received_fax_id'] = received_fax
    result = state.api().post(f'/cases/{segment(case_id)}/accept', json=body)
    state.out().result(result, lambda out: _documents_out(out, result))


@cases.command('invalidate')
def cases_invalidate(case_id: str = typer.Argument(..., help='Your case reference.'),
                     to: str = typer.Option(..., '--to', help='Recipient fax number.'),
                     document: list[str] = typer.Option(..., '--document', help='A document the recipient could '
                                                        'not find, by title or reference. Repeat for more.'),
                     purpose: str = typer.Option(None, '--purpose', help='Only documents sent for this purpose.'),
                     note: str = typer.Option(None, '--note', help='What the recipient said.')):
    """The recipient could not find these documents: stop listing them, and send them in full in the next packet."""
    chosen = _choose(case_id, to, document, purpose, sent_only=False)
    result = state.api().post(f'/cases/{segment(case_id)}/invalidate', json={
        'to': to, 'documents': [item['id'] for item in chosen], 'note': note})
    state.out().result(result, lambda out: _documents_out(out, result))


@cases.command('repair')
def cases_repair(case_id: str = typer.Argument(..., help='Your case reference.'),
                 to: str = typer.Option(..., '--to', help='Recipient fax number.'),
                 reason: str = typer.Option(None, '--reason', help='Why the recipient needs every document again. '
                                            'Required to send.'),
                 preview: bool = typer.Option(False, '--preview', help='Show what would be sent without sending.')):
    """Send every document of the case to this recipient again, as a new fax. Faxbot never does this by itself."""
    if not preview and not (reason or '').strip():
        raise CliError('Add --reason to say why the recipient needs every document again.')
    result = state.api().post(f'/cases/{segment(case_id)}/repair',
                              json={'to': to, 'reason': reason or '', 'preview': preview})

    def human(out):
        out.table(['Document', 'Version and source', 'Pages'],
                  [[item['title'], detail(item), item['pages']] for item in result.get('documents', [])])
        for title in result.get('missing') or []:
            out.line(f"Not included: '{title}' was sent before Faxbot kept original documents. Add it with "
                     "'faxbot recipients cases add'.")
        if result.get('packets_in_flight'):
            out.line(f"{result['packets_in_flight']} earlier packet for this case has not finished sending.")
        out.line(f"{result['pages']} pages.")
        if result.get('fax_id'):
            out.line(f"Sent as fax {result['fax_id']}. Check on it with: faxbot status {result['fax_id']}")
        else:
            out.line('Preview only; nothing was sent.')
    state.out().result(result, human)


@cases.command('reuse')
def cases_reuse(to: str = typer.Argument(..., help='Recipient fax number.'),
                days: int = typer.Option(None, '--days', min=1, max=3650, help='Trust its acknowledgements for '
                                         'this many days, then send those documents in full again.'),
                no_limit: bool = typer.Option(False, '--no-limit', help='Trust its acknowledgements with no time '
                                              'limit.'),
                default: bool = typer.Option(False, '--default', help="Use Faxbot's default period.")):
    """Show or set how long this recipient's acknowledgements are trusted before documents are sent in full again."""
    if sum((days is not None, no_limit, default)) > 1:
        raise CliError('Use only one of --days, --no-limit and --default.')
    api = state.api()
    if days is None and not no_limit and not default:
        result = api.get(f'/case-recipients/{segment(to)}')
    else:
        current = api.get(f'/case-recipients/{segment(to)}')
        value = None if default else 0 if no_limit else days
        result = api.patch(f'/case-recipients/{segment(to)}', json={'reuse_days': value,
                                                                   'version': current['version']})

    def human(out):
        if result['reuse_days'] == 0:
            out.line(f"{result['to']}: acknowledgements are trusted with no time limit.")
        else:
            out.line(f"{result['to']}: acknowledgements are trusted for {result['reuse_days']} days"
                     + ('.' if result['reuse_days_set'] else " (Faxbot's default)."))
    state.out().result(result, human)


@cases.command('originals')
def cases_originals(case_id: str = typer.Argument(..., help='Your case reference.')):
    """List the case's original documents, kept unchanged, with their type, date, version and source."""
    result = state.api().get(f'/cases/{segment(case_id)}/originals')
    state.out().result(result, lambda out: out.table(
        ['Document', 'Type', 'Dated', 'Version and source', 'Pages', 'Reference'],
        [[item['title'], item.get('document_type') or '-', (item.get('document_date') or '-')[:10], detail(item),
          item['pages'], item['reference']] for item in result.get('originals', [])],
        empty='No documents are kept for this case yet.'))


def _per_file(values, count, name):
    values = list(values or [])
    if len(values) > count:
        raise CliError(f'Give at most one {name} per file.')
    return values


@cases.command('add')
def cases_add(case_id: str = typer.Argument(..., help='Your case reference.'),
              files: list[Path] = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                                 help='PDF documents to keep in the case.'),
              title: list[str] = typer.Option(None, '--title', help='Title for each file, in order. '
                                              'Default: the file name.'),
              kind: list[str] = typer.Option(None, '--type', help='Document type for each file, in order, as '
                                             'checklists name them, for example "Discharge summary".'),
              date: list[str] = typer.Option(None, '--date', help='The date on each document, in order, as '
                                             'year-month-day.'),
              version: list[str] = typer.Option(None, '--version', help='Version of each file, in order, for '
                                                'example "final".'),
              source: list[str] = typer.Option(None, '--source', help='Where each file came from, in order.')):
    """Keep documents in a case without sending them, for checklist packets and repairs."""
    data = {'titles': _per_file(title, len(files), '--title'), 'types': _per_file(kind, len(files), '--type'),
            'dates': _per_file(date, len(files), '--date'), 'versions': _per_file(version, len(files), '--version'),
            'sources': _per_file(source, len(files), '--source')}
    with ExitStack() as stack:
        uploads = [('documents', (path.name, stack.enter_context(path.open('rb')), 'application/pdf'))
                   for path in files]
        result = state.api().post(f'/cases/{segment(case_id)}/originals', data=data, files=uploads)
    state.out().result(result, lambda out: out.line(
        f"{len(result.get('originals', []))} documents are kept for case {result['case_id']}."))


@cases.command('suggestions')
def cases_suggestions(setting: str = typer.Argument(..., help='on or off.')):
    """Turn on or off suggestions of documents that may match a missing checklist item. Off unless you turn it on."""
    if setting not in {'on', 'off'}:
        raise CliError("Say 'on' or 'off'.")
    write_settings(state.api(), {'case_suggestions_enabled': setting == 'on'})
    state.out().result({'suggestions': setting == 'on'}, lambda out: out.line(
        'Checklist packets now suggest documents that may match a missing item. Nothing suggested is sent unless '
        'you add it.' if setting == 'on' else 'Checklist packets no longer suggest documents.'))


# -- checklists ----------------------------------------------------------------------------------------------

def _items_out(out, result):
    out.line(f"{result['name']}, version {result['version']}"
             + (f", for {result['to']}" if result.get('to') else '')
             + (f"; used for {result['used']} packets" if result.get('used') else '; not used yet') + '.')
    out.table(['Item', 'Document type', 'Required', 'Dated within', 'Version'],
              [[number, item['type'], 'yes' if item['required'] else 'no',
                f"{item['within_days']} days" if item.get('within_days') else '-', item.get('version') or '-']
               for number, item in enumerate(result['items'], start=1)])


@checklist.command('list')
def checklist_list():
    """List checklists, newest version of each."""
    result = state.api().get('/case-checklists')
    state.out().result(result, lambda out: out.table(
        ['Checklist', 'Version', 'Items', 'Used'],
        [[item['name'], item['version'], len(item['items']), item['used']] for item in result.get('checklists', [])],
        empty="No checklists yet. Start from the example with: faxbot recipients cases checklist add NAME --example"))


@checklist.command('show')
def checklist_show(name: str = typer.Argument(..., help='The checklist name.'),
                   version: int = typer.Option(None, '--version', min=1, help='A version; default: the newest.')):
    """Show a checklist's items, and its versions."""
    result = state.api().get(f'/case-checklists/{segment(name)}', params={'version': version} if version else None)
    state.out().result(result, lambda out: _items_out(out, result))


@checklist.command('add')
def checklist_add(name: str = typer.Argument(..., help='The checklist name, for example the recipient and request.'),
                  items: Path = typer.Argument(None, exists=True, dir_okay=False, readable=True,
                                               help='A JSON file: a list of items, each with "type", and optionally '
                                                    '"required", "within_days" and "version".'),
                  example: bool = typer.Option(False, '--example', help='Start from the synthetic example checklist.'),
                  to: str = typer.Option(None, '--to', help='The recipient fax number it is for.')):
    """Save a checklist. Using a name again saves a new version; earlier versions never change."""
    api = state.api()
    if example == (items is not None):
        raise CliError('Give a JSON file of items, or --example, but not both.')
    if example:
        entries = api.get('/case-checklists')['example']['items']
    else:
        try:
            entries = json.loads(items.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise CliError(f'Cannot read {items} as JSON.') from None
        if isinstance(entries, dict):
            entries = entries.get('items')
    result = api.post('/case-checklists', json={'name': name, 'items': entries, **({'to': to} if to else {})})
    state.out().result(result, lambda out: _items_out(out, result))


@cases.command('build')
def cases_build(case_id: str = typer.Argument(..., help='Your case reference.'),
                to: str = typer.Option(..., '--to', help='Recipient fax number.'),
                checklist_name: str = typer.Option(..., '--checklist', help='The checklist to follow.'),
                version: int = typer.Option(None, '--version', min=1, help='The checklist version; default: newest.'),
                as_of: str = typer.Option(None, '--as-of', help='Count date limits back from this day, as '
                                          'year-month-day. Default: today.'),
                purpose: str = typer.Option('', '--purpose', help='What the packet is for.'),
                preview: bool = typer.Option(False, '--preview', help="Show Faxbot's picks without sending."),
                allow_missing: bool = typer.Option(False, '--allow-missing', help='Send even though required '
                                                   'items are missing, when the recipient agreed.')):
    """Build a packet from a checklist with the case's kept documents: picks with reasons, then missing items."""
    api = state.api()
    found = api.get(f'/case-checklists/{segment(checklist_name)}', params={'version': version} if version else None)
    body = {'to': to, 'checklist_id': found['id'], 'as_of': as_of, 'purpose': purpose, 'preview': True}
    result = api.post(f'/cases/{segment(case_id)}/checklist-packets', json=body)
    if not preview:
        # What the preview picked is what you confirm by sending; the same inputs always pick the same documents.
        selection = [{'original_id': item['original_id'], 'item': item['item']} for item in result['selected']]
        result = api.post(f'/cases/{segment(case_id)}/checklist-packets', json={
            **body, 'as_of': result['as_of'], 'preview': False, 'selection': selection,
            'allow_missing': allow_missing})

    def human(out):
        out.line(f"{result['checklist']['name']}, version {result['checklist']['version']}, as of {result['as_of']}.")
        out.table(['Item', 'Document', 'Why'], [[item['item'] + 1 if item['item'] is not None else '-', item['title'],
                                                 item['reason']] for item in result.get('selected', [])],
                  empty='No kept document matches any item.')
        for entry in result.get('missing', []):
            out.line(f"Missing{'' if entry['required'] else ' (optional)'}: item {entry['item'] + 1}, "
                     f"{entry['type']}. {entry['reason']}")
        for entry in result.get('suggestions', []):
            out.line(f"Suggested for item {entry['item'] + 1}: {entry['title']}. {entry['reason']}")
        packet = result.get('packet')
        if packet:
            out.line(f"{packet['pages']} pages to send, {packet['pages_saved']} pages saved.")
        if result.get('fax_id'):
            out.line(f"Sent as fax {result['fax_id']}. Check on it with: faxbot status {result['fax_id']}")
        else:
            out.line('Preview only; nothing was sent.')
    state.out().result(result, human)
