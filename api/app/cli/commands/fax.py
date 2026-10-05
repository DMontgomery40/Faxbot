"""Sending faxes, checking sent faxes and reading received faxes."""
from pathlib import Path
import sys

import typer

from .. import state
from ..client import segment
from ..errors import CliError, EXIT_NOT_FOUND
from ..output import cost_amount, local_time, parse_time, yes_no
from ...provider_labels import provider_label


def _provider(identity):
    """A provider's one plain name for people; JSON output keeps the id."""
    return provider_label(identity) if identity else None

jobs = typer.Typer(help='Sent faxes: list them, read details, download documents and check status.',
                   no_args_is_help=True)
inbound = typer.Typer(help='Received faxes: list them, read details, download documents and fetch them again.',
                      no_args_is_help=True)

_TYPES = {'.pdf': 'application/pdf', '.txt': 'text/plain'}

# The console's words for each delivery state and event (JobsList.tsx).
STATUS_LABELS = {'held': 'Held test fax', 'ready': 'Ready to send', 'preparing': 'Preparing', 'submitting': 'Sending',
                 'in_progress': 'In progress', 'reconciliation_required': 'Needs review', 'success': 'Delivered',
                 'failed': 'Failed', 'cancelled': 'Cancelled'}
EVENT_LABELS = {
    'accepted': 'Fax accepted', 'legacy_migrated': 'Imported from an earlier version',
    'binding_unavailable': 'Original provider account unavailable', 'held_acceptance_restored': 'Held test fax restored',
    'claimed': 'Preparing to send', 'dispatch_paused': 'Sending paused', 'submission_authorized': 'Sending to provider',
    'submission_uncertain': 'Provider response unclear',
    'sent_together': 'Going in one call with other faxes to this number',
    'batch_split': 'Going on its own instead of with other faxes', 'preparation_failed': 'Could not prepare fax',
    'preparation_expired': 'Preparation timed out', 'provider_observation_refused': 'Provider update ignored',
    'terminal_conflict': 'Conflicting provider update ignored', 'late_observation': 'Late provider update',
    'provider_observed': 'Provider status update', 'operator_identity_bound': 'Receipt confirmed with the provider fax ID',
    'route_assigned': 'Route chosen', 'route_fallback': 'Trying the next route'}
RECEIVED_NOT_FOUND = 'No received fax you can see has that ID. See faxbot received list --ids.'


def _label(state):
    state = (state or '').lower()
    return STATUS_LABELS.get(state) or state.replace('_', ' ').capitalize() or '-'


def status_label(job):
    """A sent fax's state in the console's words; a fax waiting to go with others says so."""
    state = (job.get('delivery_state') or job.get('status') or '').lower()
    if (job.get('together') or {}).get('state') == 'waiting' and state not in {'success', 'failed', 'cancelled'}:
        return 'Waiting to go with other faxes'
    return _label(state)


def delivery_notice(job):
    """The console's sentence under the state, when there is one."""
    state = (job.get('delivery_state') or job.get('status') or '').lower()
    if state == 'held' or job.get('dispatch_mode') == 'held':
        return 'This test fax is held and will not be sent.'
    if state == 'reconciliation_required':
        return ('Imported fax with no delivery record; check your provider account for the outcome.'
                if job.get('dispatch_mode') == 'legacy' else
                "Delivery couldn't be confirmed; check your provider account and confirm receipt instead of resending.")
    return None


def _route_text(job, cost):
    """The route that carried the fax (its latest attempt) and any route tried before it, as Sent shows it."""
    routes = (cost or {}).get('routes') or []
    if not routes:
        return _provider(job.get('backend'))
    names = ['Direct delivery' if route == 'direct' else _provider(route) for route in routes]
    return names[-1] + (f" (after {', '.join(names[:-1])})" if len(names) > 1 else '')


def _fax_fields(job):
    return [('Fax ID', job.get('id')), ('To', job.get('to') or job.get('to_number')), ('Status', status_label(job)),
            *([('Delivery', delivery_notice(job))] if delivery_notice(job) else []),
            ('Pages', job.get('pages')), ('Provider', _provider(job.get('backend'))),
            ('Provider fax ID', job.get('provider_sid')), ('Problem', job.get('error')),
            ('Accepted', local_time(job.get('created_at'))), ('Updated', local_time(job.get('updated_at')))]


def send(to: str = typer.Argument(..., help='Fax number to send to, for example +15551234567.'),
         file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                     help='PDF or plain text file to fax.'),
         queue: bool = typer.Option(False, '--queue',
                                    help='Accept the fax without sending it. Faxbot allows this only while '
                                         'sending is turned off (test mode).'),
         idempotency_key: str = typer.Option(None, '--idempotency-key', metavar='KEY',
                                             help='Your own reference for this fax. Sending again with the same '
                                                  'reference returns the first fax instead of sending twice.'),
         now: bool = typer.Option(False, '--now',
                                  help='Send immediately, even when this number batches faxes; faxes already '
                                       'waiting for it go in the same call.')):
    """Send a fax. Faxbot accepts it and sends it in the background."""
    api = state.api()
    headers = {'Idempotency-Key': idempotency_key} if idempotency_key else None
    content_type = _TYPES.get(file.suffix.lower(), 'application/octet-stream')
    data = {'to': to, 'queue_only': 'true' if queue else 'false'}
    if now:
        data['send_now'] = 'true'
    with file.open('rb') as handle:
        job = api.post('/fax', data=data, files={'file': (file.name, handle, content_type)}, headers=headers)
    waiting = _together(api, job['id'])

    def human(out):
        out.line('Fax accepted.')
        out.fields(_fax_fields(job))
        if waiting:
            out.line(waiting)
        out.line(f"Check on it with: faxbot status {job['id']}")
    state.out().result(job, human)


def _together_line(view):
    """One sentence for a fax that waits, or went, with other faxes to the same number."""
    if not view or not view.get('state'):
        return None
    if view['state'] == 'waiting':
        until = local_time(view.get('waiting_until'))
        return f'Waiting to go with other faxes to this number until {until}. To send it now: faxbot sent send-now ID'
    if view['state'] == 'together':
        share = (view.get('share') or {}).get('sentence')
        return view['sentence'] + (' ' + share if share else '')
    return None


def _together(api, fax_id):
    """Best effort: a sender may not be allowed to read the fax back."""
    try:
        return _together_line(api.get('/batching/faxes/' + segment(fax_id)))
    except CliError:
        return None


def status(fax_id: str = typer.Argument(..., help='Fax ID shown when the fax was sent.')):
    """Show where a sent fax is now."""
    job = state.api().get('/fax/' + segment(fax_id))
    state.out().result(job, lambda out: out.fields(_fax_fields(job)))


@jobs.command('list')
def jobs_list(status_filter: str = typer.Option(None, '--status', help='Only faxes with this status, such as queued, '
                                                                       'SUCCESS or FAILED.'),
              provider: str = typer.Option(None, '--provider', help='Only faxes sent through this provider.'),
              limit: int = typer.Option(50, '--limit', min=1, max=100, help='How many faxes to show.'),
              offset: int = typer.Option(0, '--offset', min=0, help='Skip this many of the newest faxes.'),
              ids: bool = typer.Option(False, '--ids', help="Also show each fax's ID, to use with faxbot sent show, pdf and refresh.")):
    """List sent faxes, newest first, with what each cost. Fax numbers are partly hidden."""
    api = state.api()
    page = api.get('/admin/fax-jobs', params={'status': status_filter, 'backend': provider,
                                              'limit': limit, 'offset': offset})
    costs = {}
    if page['jobs']:
        try:
            costs = api.get('/routing/fax-costs', params={'ids': ','.join(job['id'] for job in page['jobs'])})['costs']
        except CliError:
            costs = {}  # the list still shows without its cost column's amounts
    page = {**page, 'costs': costs}

    def human(out):
        out.table((['Fax ID'] if ids else []) + ['To', 'Status', 'Pages', 'Route', 'Cost', 'Accepted'],
                  [([job['id']] if ids else []) + [job['to_number'], status_label(job), job['pages'],
                                                   _route_text(job, costs.get(job['id'])),
                                                   cost_amount(costs.get(job['id'])), local_time(job['created_at'])]
                   for job in page['jobs']],
                  empty='No sent faxes.')
        if page['total'] > offset + len(page['jobs']):
            out.line(f"Showing {len(page['jobs'])} of {page['total']}. Use --offset to see more.")
    state.out().result(page, human)


@jobs.command('get')
def jobs_get(fax_id: str = typer.Argument(..., help='Fax ID.')):
    """Show one sent fax."""
    api = state.api()
    job = api.get('/admin/fax-jobs/' + segment(fax_id))
    together = job.get('together') or {}
    line = _together(api, fax_id) if together else None

    def human(out):
        out.fields(_fax_fields(job) + ([('Reference on its separator page', together.get('reference'))]
                                       if together.get('state') == 'together' else []))
        if line:
            out.line(line)
        # Over the SIP trunk: SSL Fax's line, or why the built-in fax engine carried it.
        if (job.get('fax_engine') or {}).get('sentence'):
            out.line(job['fax_engine']['sentence'])
    state.out().result(job, human)


@jobs.command('send-now')
def jobs_send_now(fax_id: str = typer.Argument(..., help='Fax ID of a fax waiting to go with others.')):
    """Send a waiting fax now; the faxes waiting with it go in the same call."""
    view = state.api().post(f'/batching/faxes/{segment(fax_id)}/send-now')
    state.out().result(view, lambda out: out.line('Sending now, together with the faxes waiting with it.'))


def save_document(response, output, default_name, force):
    """Write downloaded bytes to a file (or standard output for '-'); never overwrite without --force."""
    if output == '-':
        sys.stdout.buffer.write(response.content)
        sys.stdout.buffer.flush()
        return None
    target = Path(output or default_name)
    if target.exists() and not force:
        raise CliError(f'{target} already exists. Choose another --output or add --force.')
    try:
        target.write_bytes(response.content)
    except OSError:
        raise CliError(f'Cannot write {target}.') from None
    return target


def _report_saved(target, size):
    if target is not None:
        state.out().result({'saved_to': str(target), 'bytes': size},
                           lambda out: out.line(f'Saved {size} bytes to {target}.'))


@jobs.command('pdf')
def jobs_pdf(fax_id: str = typer.Argument(..., help='Fax ID.'),
             output: str = typer.Option(None, '--output', '-o', help="File to write. Use '-' for standard output."),
             force: bool = typer.Option(False, '--force', help='Replace the file if it exists.')):
    """Download the document of a sent fax."""
    response = state.api().get(f'/admin/fax-jobs/{segment(fax_id)}/pdf', raw=True,
                               headers={'Accept': 'application/pdf'})
    _report_saved(save_document(response, output, f'fax_{fax_id}.pdf', force), len(response.content))


@jobs.command('refresh')
def jobs_refresh(fax_id: str = typer.Argument(..., help='Fax ID.')):
    """Ask the provider for the latest status of a sent fax. This never sends it again."""
    job = state.api().post(f'/admin/fax-jobs/{segment(fax_id)}/refresh')
    state.out().result(job, lambda out: out.fields(_fax_fields(job)))


@jobs.command('history')
def jobs_history(fax_id: str = typer.Argument(..., help="Fax ID, from 'faxbot sent list --ids'.")):
    """Show the evidence of a sent fax's delivery: each step and what the fax service reported."""
    history = state.api().get(f'/admin/fax-jobs/{segment(fax_id)}/delivery')

    def human(out):
        attempt = history.get('attempt') or {}
        out.fields([('Status', _label(history.get('state'))),
                    ('Provider of the latest attempt', _provider(history.get('provider_id'))),
                    ('Provider fax ID', attempt.get('provider_sid')),
                    ('Submitted', local_time(attempt.get('submitted_at'))),
                    ('Finished', local_time(attempt.get('completed_at')))])
        if history.get('state') == 'reconciliation_required':
            if history.get('can_bind_provider_identity'):
                out.line('If the fax service shows this fax, confirm it with: faxbot sent confirm-receipt '
                         f'{fax_id} --provider-fax-id ID --confirm-original-account')
            elif history.get('bind_refusal_reason'):
                out.line(history['bind_refusal_reason'])
        out.table(['When', 'Event'], [[local_time(event['created_at']),
                                       EVENT_LABELS.get(event['kind'], event['kind'].replace('_', ' ').capitalize())]
                                      for event in history.get('events', [])], empty='No delivery events yet.')
    state.out().result(history, human)


@jobs.command('reconcile')
def jobs_reconcile(fax_id: str = typer.Argument(..., help="Fax ID, from 'faxbot sent list --ids'."),
                   provider_fax_id: str = typer.Option(..., '--provider-fax-id',
                                                       help="The fax's ID in the provider account that accepted it."),
                   confirm: bool = typer.Option(False, '--confirm-original-account',
                                                help='Confirm you found this ID in the same provider account that '
                                                     'accepted the fax.')):
    """Confirm receipt of a fax whose delivery is uncertain, by recording the ID the fax service gave it. This never sends it again."""
    if not confirm:
        raise CliError('Check the fax in the provider account that accepted it, then add '
                       '--confirm-original-account.')
    api = state.api()
    current = api.get('/admin/fax-jobs/' + segment(fax_id))
    history = api.post(f'/admin/fax-jobs/{segment(fax_id)}/reconcile', json={
        'expected_version': current['delivery_version'], 'provider_sid': provider_fax_id,
        'confirm_original_account': True})
    state.out().result(history, lambda out: out.line('Provider fax ID recorded. Faxbot will follow this fax '
                                                     'with the provider.'))


def _inbound_status(item):
    """The server's one sentence, with a retry time in this computer's time zone."""
    retry = parse_time(item.get('retry_at'))
    if item.get('status') == 'waiting' and retry is not None:
        return f"The document could not be fetched; Faxbot will try again at {retry.astimezone().strftime('%H:%M')}."
    if item.get('status') == 'failed':
        return 'Faxbot stopped trying to fetch this document; run faxbot received fetch to try again.'
    return item.get('status_text') or 'Received.'


def _size(value):
    if not isinstance(value, int):
        return None
    if value >= 1024 * 1024:
        return f'{value / (1024 * 1024):.1f} MB'
    return f'{max(1, round(value / 1024))} KB'


def _document(item):
    if item.get('status') in ('waiting', 'failed'):
        return 'Not received yet'
    pages = item.get('pages')
    parts = [f"{pages} {'page' if pages == 1 else 'pages'}" if isinstance(pages, int) else None,
             _size(item.get('size_bytes')),
             f"SHA-256 {item['sha256'][:12]}…" if item.get('sha256') else None]
    return ', '.join(part for part in parts if part) or '-'




def arrived(item):
    """When the fax arrived (the provider's time when known), marked when Faxbot brought it in later."""
    when = local_time(item.get('source_received_at') or item.get('received_at') or item.get('created_at'))
    return f'{when} · brought in later' if item.get('recovered') else when


def _inbound_fields(item):
    return [('Received fax ID', item.get('id')), ('From', item.get('fr') or 'Unknown'),
            ('To', item.get('to') or 'Unknown'),
            ('Status', _inbound_status(item)), ('Problem', item.get('problem')), ('Mailbox', item.get('mailbox')),
            ('Received through', _provider(item.get('backend'))),
            ('Provider fax ID', item.get('provider_fax_id')),
            ('Sent', local_time(item.get('source_received_at'))),
            ('Received', arrived(item)),
            *([('Brought in', local_time(item.get('received_at')))] if item.get('recovered') else []),
            ('Document', _document(item)), ('Test fax', yes_no(bool(item.get('is_test')))),
            *([('Provider copy', item['provider_note'])] if item.get('provider_note') else [])]


def _provider_copies(items):
    """One sentence for received faxes still stored at eFax, as the eFax settings section says it."""
    from ...efax_service import PENDING_DELETION_NOTE, STOPPED_DELETION_NOTE, deletion_sentences
    notes = [item.get('provider_note') for item in items]
    return deletion_sentences(notes.count(PENDING_DELETION_NOTE), notes.count(STOPPED_DELETION_NOTE))


@inbound.command('list')
def inbound_list(to_number: str = typer.Option(None, '--to', help='Only faxes sent to this number.'),
                 status_filter: str = typer.Option(None, '--status', help='Only faxes with this status: waiting, '
                                                                          'received or failed.'),
                 mailbox: str = typer.Option(None, '--mailbox', help='Only faxes in this mailbox.'),
                 ids: bool = typer.Option(False, '--ids', help="Also show each received fax's ID, to use with faxbot received show and pdf.")):
    """List received faxes you can see."""
    items = state.api().get('/inbound', params={'to_number': to_number, 'status': status_filter, 'mailbox': mailbox})

    def human(out):
        out.table(
            (['Received fax ID'] if ids else []) + ['From', 'To', 'Status', 'Pages', 'Mailbox', 'Received'],
            [([item['id']] if ids else []) + [item.get('fr') or 'Unknown', item.get('to') or 'Unknown',
                                              _inbound_status(item), item.get('pages'), item.get('mailbox'),
                                              arrived(item)]
             for item in items], empty='No received faxes.')
        for sentence in _provider_copies(items):
            out.line(sentence)
    state.out().result(items, human)


def received_id(api, given, request):
    """Run request with a received fax's own ID, given that ID or its ID in the owners list.

    The two lists show different IDs for the same fax; a person pastes whichever they
    see. When the server does not know the ID as a received fax, Faxbot looks it up in
    the owners list, and reports the first answer when that finds nothing either.
    """
    try:
        return request(given)
    except CliError as failure:
        if failure.status != 404:
            raise
        try:
            fax_id = api.get('/work/' + segment(given)).get('inbound_fax_id')
        except CliError:
            fax_id = None
        if fax_id and fax_id != given:
            return request(fax_id)
        if not received_exists(api, given):
            raise CliError(RECEIVED_NOT_FOUND, EXIT_NOT_FOUND) from None
        raise failure  # the fax exists; its own answer (such as a document not stored yet) stands


def received_exists(api, fax_id):
    """Whether this person can see a received fax with this ID."""
    try:
        api.get('/inbound/' + segment(fax_id))
    except CliError as failure:
        if failure.status in (403, 404):
            return False
        raise
    return True


@inbound.command('get')
def inbound_get(inbound_id: str = typer.Argument(..., help="A received fax's ID, from 'faxbot received list --ids' or 'faxbot received owners --ids'.")):
    """Show one received fax."""
    api = state.api()
    item = received_id(api, inbound_id, lambda fax_id: api.get('/inbound/' + segment(fax_id)))
    state.out().result(item, lambda out: out.fields(_inbound_fields(item)))


@inbound.command('pdf')
def inbound_pdf(inbound_id: str = typer.Argument(..., help="A received fax's ID, from 'faxbot received list --ids' or 'faxbot received owners --ids'."),
                output: str = typer.Option(None, '--output', '-o', help="File to write. Use '-' for standard output."),
                force: bool = typer.Option(False, '--force', help='Replace the file if it exists.')):
    """Download the document of a received fax."""
    api = state.api()
    response = received_id(api, inbound_id, lambda fax_id: api.get(
        f'/inbound/{segment(fax_id)}/pdf', raw=True, headers={'Accept': 'application/pdf'}))
    _report_saved(save_document(response, output, f'inbound_{inbound_id}.pdf', force), len(response.content))


@inbound.command('fetch')
def inbound_fetch(inbound_id: str = typer.Argument(..., help="A received fax's ID, from 'faxbot received list --ids' or 'faxbot received owners --ids'.")):
    """Ask Faxbot to fetch a received fax's document again now."""
    api = state.api()
    item = received_id(api, inbound_id, lambda fax_id: api.post(f'/inbound/{segment(fax_id)}/fetch', json={}))
    state.out().result(item, lambda out: out.line('Faxbot will fetch the document shortly. Check on it with: '
                                                  f'faxbot received show {inbound_id}'))


@inbound.command('recover')
def inbound_recover():
    """Bring in received faxes that reached the phone line but were not handed to Faxbot. Faxbot also does this every minute."""
    result = state.api().post('/admin/inbound/recover', json={})
    state.out().result(result, lambda out: out.line(result['message']))


@inbound.command('simulate')
def inbound_simulate(from_number: str = typer.Option('+15550000000', '--from', help='Sender fax number to show.'),
                     to_number: str = typer.Option(None, '--to', help='Your fax number the test fax arrives on, for example +15551234567.'),
                     pages: int = typer.Option(1, '--pages', min=1, help='Ignored; a test fax always has one page.',
                                               hidden=True)):
    """Add a test fax with a real one-page document, marked as a test, to check mailboxes and email delivery."""
    result = state.api().post('/admin/inbound/simulate', json={'fr': from_number, 'to': to_number, 'pages': pages})
    state.out().result(result, lambda out: out.line(f"Test fax received with ID {result['id']}."))
