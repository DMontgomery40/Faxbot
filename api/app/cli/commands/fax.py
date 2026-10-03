"""Sending faxes, checking sent faxes and reading received faxes."""
from pathlib import Path
import sys

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time

jobs = typer.Typer(help='Sent faxes: list them, read details, download documents and check status.',
                   no_args_is_help=True)
inbound = typer.Typer(help='Received faxes: list them, read details and download documents.', no_args_is_help=True)

_TYPES = {'.pdf': 'application/pdf', '.txt': 'text/plain'}

STATE_TEXT = {
    'held': 'Held: sending is turned off, so it will not be sent.',
    'ready': 'Waiting to be sent.',
    'in_progress': 'Being sent.',
    'reconciliation_required': 'Needs checking with the provider before anything else happens.',
    'succeeded': 'Delivered.',
    'failed': 'Not delivered.',
    'abandoned': 'Stopped without delivery.',
}


def register(app):
    app.command('send')(send)
    app.command('status')(status)
    app.add_typer(jobs, name='jobs')
    app.add_typer(inbound, name='inbound')


def _fax_fields(job):
    return [('Fax ID', job.get('id')), ('To', job.get('to') or job.get('to_number')), ('Status', job.get('status')),
            ('Delivery', STATE_TEXT.get(job.get('delivery_state'), job.get('delivery_state'))),
            ('Pages', job.get('pages')), ('Provider', job.get('backend')),
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
                                                  'reference returns the first fax instead of sending twice.')):
    """Send a fax. Faxbot accepts it and sends it in the background."""
    api = state.api()
    headers = {'Idempotency-Key': idempotency_key} if idempotency_key else None
    content_type = _TYPES.get(file.suffix.lower(), 'application/octet-stream')
    with file.open('rb') as handle:
        job = api.post('/fax', data={'to': to, 'queue_only': 'true' if queue else 'false'},
                       files={'file': (file.name, handle, content_type)}, headers=headers)

    def human(out):
        out.line('Fax accepted.')
        out.fields(_fax_fields(job))
        out.line(f"Check on it with: faxbot status {job['id']}")
    state.out().result(job, human)


def status(fax_id: str = typer.Argument(..., help='Fax ID shown when the fax was sent.')):
    """Show where a sent fax is now."""
    job = state.api().get('/fax/' + segment(fax_id))
    state.out().result(job, lambda out: out.fields(_fax_fields(job)))


@jobs.command('list')
def jobs_list(status_filter: str = typer.Option(None, '--status', help='Only faxes with this status, such as queued, '
                                                                       'SUCCESS or FAILED.'),
              provider: str = typer.Option(None, '--provider', help='Only faxes sent through this provider.'),
              limit: int = typer.Option(50, '--limit', min=1, max=100, help='How many faxes to show.'),
              offset: int = typer.Option(0, '--offset', min=0, help='Skip this many of the newest faxes.')):
    """List sent faxes, newest first. Fax numbers are partly hidden."""
    page = state.api().get('/admin/fax-jobs', params={'status': status_filter, 'backend': provider,
                                                      'limit': limit, 'offset': offset})

    def human(out):
        out.table(['Fax ID', 'To', 'Status', 'Pages', 'Provider', 'Accepted'],
                  [[job['id'], job['to_number'], job['status'], job['pages'], job['backend'],
                    local_time(job['created_at'])] for job in page['jobs']], empty='No sent faxes.')
        if page['total'] > offset + len(page['jobs']):
            out.line(f"Showing {len(page['jobs'])} of {page['total']}. Use --offset to see more.")
    state.out().result(page, human)


@jobs.command('get')
def jobs_get(fax_id: str = typer.Argument(..., help='Fax ID.')):
    """Show one sent fax."""
    job = state.api().get('/admin/fax-jobs/' + segment(fax_id))
    state.out().result(job, lambda out: out.fields(_fax_fields(job)))


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
def jobs_history(fax_id: str = typer.Argument(..., help='Fax ID.')):
    """Show the delivery history of a sent fax."""
    history = state.api().get(f'/admin/fax-jobs/{segment(fax_id)}/delivery')

    def human(out):
        attempt = history.get('attempt') or {}
        out.fields([('Delivery', STATE_TEXT.get(history.get('state'), history.get('state'))),
                    ('Provider', history.get('provider_id')), ('Provider fax ID', attempt.get('provider_sid')),
                    ('Submitted', local_time(attempt.get('submitted_at'))),
                    ('Finished', local_time(attempt.get('completed_at'))),
                    ('Can record provider fax ID', history.get('can_bind_provider_identity')),
                    ('Why not', history.get('bind_refusal_reason'))])
        out.table(['When', 'Event'], [[local_time(event['created_at']), event['kind'].replace('_', ' ')]
                                      for event in history.get('events', [])], empty='No delivery events yet.')
    state.out().result(history, human)


@jobs.command('reconcile')
def jobs_reconcile(fax_id: str = typer.Argument(..., help='Fax ID.'),
                   provider_fax_id: str = typer.Option(..., '--provider-fax-id',
                                                       help="The fax's ID in the provider account that accepted it."),
                   confirm: bool = typer.Option(False, '--confirm-original-account',
                                                help='Confirm you found this ID in the same provider account that '
                                                     'accepted the fax.')):
    """Record the provider's fax ID for a fax whose delivery is uncertain. This never sends it again."""
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


def _inbound_fields(item):
    return [('Received fax ID', item.get('id')), ('From', item.get('fr')), ('To', item.get('to')),
            ('Status', item.get('status')), ('Pages', item.get('pages')), ('Mailbox', item.get('mailbox')),
            ('Provider', item.get('backend')), ('Received', local_time(item.get('received_at') or item.get('created_at')))]


@inbound.command('list')
def inbound_list(to_number: str = typer.Option(None, '--to', help='Only faxes sent to this number.'),
                 status_filter: str = typer.Option(None, '--status', help='Only faxes with this status.'),
                 mailbox: str = typer.Option(None, '--mailbox', help='Only faxes in this mailbox.')):
    """List received faxes you can see."""
    items = state.api().get('/inbound', params={'to_number': to_number, 'status': status_filter, 'mailbox': mailbox})
    state.out().result(items, lambda out: out.table(
        ['Received fax ID', 'From', 'To', 'Pages', 'Mailbox', 'Received'],
        [[item['id'], item.get('fr'), item.get('to'), item.get('pages'), item.get('mailbox'),
          local_time(item.get('received_at') or item.get('created_at'))] for item in items],
        empty='No received faxes.'))


@inbound.command('get')
def inbound_get(inbound_id: str = typer.Argument(..., help='Received fax ID.')):
    """Show one received fax."""
    item = state.api().get('/inbound/' + segment(inbound_id))
    state.out().result(item, lambda out: out.fields(_inbound_fields(item)))


@inbound.command('pdf')
def inbound_pdf(inbound_id: str = typer.Argument(..., help='Received fax ID.'),
                output: str = typer.Option(None, '--output', '-o', help="File to write. Use '-' for standard output."),
                force: bool = typer.Option(False, '--force', help='Replace the file if it exists.')):
    """Download the document of a received fax."""
    response = state.api().get(f'/inbound/{segment(inbound_id)}/pdf', raw=True, headers={'Accept': 'application/pdf'})
    _report_saved(save_document(response, output, f'inbound_{inbound_id}.pdf', force), len(response.content))
