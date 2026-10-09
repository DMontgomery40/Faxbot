"""faxbot costs charges and faxbot costs invoices: what each provider charged, faxes it billed that Faxbot has no
record of, and monthly invoices compared with your faxes."""
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time, money, text


charges = typer.Typer(help='What each provider charged: how Faxbot reads it, received fax charges, and faxes a '
                           'provider billed that Faxbot has no record of. Run it alone to show them.',
                      invoke_without_command=True)
invoices = typer.Typer(help="Your providers' monthly invoices, and the part your faxes don't explain.",
                       no_args_is_help=True)


def _show_charges(out, result):
    out.table(['Account', 'How Faxbot reads its charges', 'Faxes last checked'],
              [[item['label'], item['sentence'],
                local_time(item['last_checked']) if item.get('last_checked') else
                ('-' if not item.get('listing') else 'Not yet')] for item in result.get('accounts', [])],
              empty='No provider accounts are set up.')
    received = [item for item in result.get('received', []) if item.get('summary')]
    for item in received:
        out.line(f"Received faxes: {item['summary']}")
    trunk = result.get('trunk') or {}
    if trunk.get('sentence') and not any(item['provider_id'] == 'sip' for item in result.get('accounts', [])):
        out.line(trunk['sentence'])
    rows = result.get('unrecorded') or []
    if rows:
        out.line(f"Faxes your providers billed that Faxbot has no record of ({len(rows)}):")
        for item in rows:
            out.line(f"  {item['summary']}")
    else:
        out.line('Your providers listed no fax that Faxbot has no record of.')


@charges.callback()
def charges_show(context: typer.Context,
                 days: int = typer.Option(30, '--days', min=1, max=366, help='How many days back to show.')):
    """Show how each account's charges are read, received fax charges, and faxes Faxbot has no record of."""
    if context.invoked_subcommand is not None:
        return
    result = state.api().get('/routing/charges', params={'days': days})
    state.out().result(result, lambda out: _show_charges(out, result))


@charges.command('sweep')
def charges_sweep(account: str = typer.Option(None, '--account', metavar='KEY',
                                              help="One account's key, from 'faxbot providers accounts list'. "
                                                   'Default: every account whose provider lists its faxes.'),
                  days: int = typer.Option(7, '--days', min=1, max=31, help='How many days back to list.')):
    """List each account's faxes at its provider now and show any Faxbot has no record of. This never sends, fetches or changes a fax."""
    result = state.api().post('/routing/charges/sweep', json={'account': account, 'days': days})

    def human(out):
        out.line(result['summary'])
        if any(item.get('unrecorded') for item in result.get('results', [])):
            out.line("Run 'faxbot costs charges' to see each one.")
    state.out().result(result, human)


def _invoice_lines(out, item):
    out.line(f"{item['label']}, {item['period_name']}: invoice {money([item['total']])}.")
    out.line(item['summary'])
    for part in item.get('parts', []):
        out.line(f"  {part['label']}: {money([part['amount']])}")
    for note in item.get('notes', []):
        out.line(note)


@invoices.command('list')
def invoices_list(account: str = typer.Option(None, '--account', metavar='KEY', help='Only this account.')):
    """List the invoices you entered, each with the part your faxes don't explain, and what to do when that recurs."""
    result = state.api().get('/routing/invoices', params={'account': account})

    def human(out):
        out.table(['Invoice ID', 'Account', 'Period', 'Invoice', 'Explained by faxes', 'Not explained'],
                  [[item['id'], item['label'], item['period_name'], money([item['total']]), money([item['explained']]),
                    money([item['residual']]) + (' (incomplete)' if not item['complete'] else '')]
                   for item in result.get('invoices', [])],
                  empty="No invoices entered yet. Add one with 'faxbot costs invoices add'.")
        for advice in result.get('recommendations', []):
            out.line(advice['text'])
    state.out().result(result, human)


@invoices.command('show')
def invoices_show(invoice_id: str = typer.Argument(..., help="Invoice ID, from 'faxbot costs invoices list'."),
                  save: Path = typer.Option(None, '--save-file', metavar='PATH',
                                            help='Save the invoice file entered with it to this path.')):
    """Show one invoice: what your faxes explain, what they don't, and each version entered."""
    api = state.api()
    result = api.get(f'/routing/invoices/{segment(invoice_id)}')
    if save is not None:
        if not result.get('file'):
            raise CliError('No file was entered with this invoice.')
        if save.exists():
            raise CliError(f'{save} already exists; choose another path.')
        response = api.get(f'/routing/invoices/{segment(invoice_id)}/file', raw=True)
        save.write_bytes(response.content)

    def human(out):
        _invoice_lines(out, result)
        if result.get('note'):
            out.line(f"Note: {result['note']}")
        if result.get('file'):
            out.line(f"File: {text(result['file'].get('name'))}" + (f', saved to {save}.' if save else ''))
        if len(result.get('history', [])) > 1:
            out.table(['Version', 'Total', 'Entered by', 'Entered'],
                      [[entry['version'], money([entry['total']]), text(entry.get('entered_by')),
                        local_time(entry['entered_at'])] for entry in result['history']])
    state.out().result(result, human)


@invoices.command('add')
def invoices_add(account: str = typer.Option(..., '--account', metavar='KEY',
                                             help="The account the invoice is for, from 'faxbot providers accounts "
                                                  "list', such as humblefax."),
                 total: str = typer.Option(..., '--total', metavar='AMOUNT', help='The invoice total, such as 13.20.'),
                 month: str = typer.Option(None, '--month', metavar='YYYY-MM',
                                           help="The month the invoice covers, such as 2026-09. Faxbot starts it on "
                                                "the plan's billing day."),
                 first_day: str = typer.Option(None, '--from', metavar='YYYY-MM-DD',
                                               help='Instead of --month: the first day the invoice covers.'),
                 last_day: str = typer.Option(None, '--to', metavar='YYYY-MM-DD',
                                              help='With --from: the last day the invoice covers.'),
                 currency: str = typer.Option('USD', '--currency', metavar='CODE', help='The three-letter currency code.'),
                 note: str = typer.Option(None, '--note', metavar='TEXT', help='A short note, such as the invoice number.'),
                 file: Path = typer.Option(None, '--file', metavar='PATH', exists=True, dir_okay=False, readable=True,
                                           help='The invoice itself: a PDF, PNG, JPEG or CSV file.')):
    """Enter an invoice total for one account and month. Entering the same month again adds a corrected version and keeps the earlier one."""
    if not month and not (first_day and last_day):
        raise CliError('Give the month with --month, or the first and last day with --from and --to.')
    form = {'account': account, 'total': total, 'currency': currency, 'month': month, 'first_day': first_day,
            'last_day': last_day, 'note': note}
    files = None
    if file is not None:
        files = {'file': (file.name, file.read_bytes(), 'application/octet-stream')}
    result = state.api().post('/routing/invoices', data={key: value for key, value in form.items() if value},
                              files=files)
    state.out().result(result, lambda out: _invoice_lines(out, result))
