"""Line inventory commands (N19): your lines matched to carrier discontinuance lists and contract end dates.

They hang off ``faxbot delivery numbers move``; ``fact_advice.py`` imports this module so they register with that group.
Nothing here orders, ports or cancels a line.
"""
from pathlib import Path

import typer

from .. import state
from ..errors import CliError
from .fact_advice import move
from .trunk import local_date


def _show(out, result):
    out.line(result.get('sentence') or '')
    found = result.get('inventory')
    if found:
        out.line(f"Inventory: {found['lines']:,} lines from {found.get('file_name') or 'your file'}"
                 + (f", imported by {found['imported_by']}." if found.get('imported_by') else '.'))
    for item in result.get('lists') or []:
        out.line(f"{item['label']}: {item['areas']:,} areas in {item['wire_centers']:,} wire centers, dated "
                 f"{local_date(item['first'])} to {local_date(item['last'])}"
                 + (f"; file of {local_date(item['file_date'])}" if item.get('file_date') else '') + '.')
        if item.get('source_url'):
            out.line(f"  Source: {item['source_url']}")
    if not result.get('lists'):
        out.line("No carrier list yet. Import AT&T's workbook with 'faxbot delivery numbers move import-carrier-list FILE' "
                 f"(download it from {result['sources']['att_workbook']}).")
    out.line(result.get('keyed') or '')
    out.table(['Number', 'Use', 'Carrier', 'Wire center', 'In Faxbot', 'What is known'],
              [[line['display'], line['use_label'], line.get('carrier') or '-', line.get('wire_center') or '-',
                line['account'] or 'No',
                ' '.join([item['sentence'] for item in line['dates']]
                         + ([line['match']['sentence']] if line.get('match') and line['match']['state'] != 'listed'
                            else [])) or '-']
               for line in result.get('lines') or []], empty='No lines in your inventory.')
    out.line(result.get('note') or '')


def _skipped(out, result):
    if result.get('skipped_count'):
        count = result['skipped_count']
        out.line(f"{count:,} {'row was' if count == 1 else 'rows were'} not read:")
        for reason in result.get('skipped') or []:
            out.line(f'  {reason}')


@move.command('inventory')
def line_inventory():
    """Your fax lines with their carrier, wire center and contract, each matched to the carrier lists you imported,
    lines with a date first."""
    result = state.api().get('/routing/line-inventory')
    state.out().result(result, lambda out: _show(out, result))


@move.command('import-inventory', short_help='Import your line inventory (CSV or Excel); it replaces the current one.')
def import_inventory(file: Path = typer.Argument(..., metavar='FILE', exists=True, dir_okay=False,
                                                 help='Your line inventory, as CSV or Excel (.xlsx).'),
                     date_order: str = typer.Option('mdy', '--date-order', metavar='mdy|dmy',
                                                    help='How the file writes dates: mdy (11/4/2026 is 4 November) '
                                                         'or dmy (4/11/2026 is 4 November).')):
    """Import your line inventory. It replaces the current one, which is kept as history; contract end dates
    become each line's contract-end date. Columns: number, service address, city, state, postal code, country,
    carrier, product (or USOC), wire center, distribution area, contract end, use (fax, alarm, elevator, emergency
    or other), monthly price, currency, note. Only the number is required."""
    if date_order not in ('mdy', 'dmy'):
        raise CliError('Choose the date order mdy or dmy.')
    with file.open('rb') as handle:
        result = state.api().post('/routing/line-inventory/files', data={'date_order': date_order},
                                  files={'file': (file.name, handle, 'application/octet-stream')})

    def human(out):
        out.line(f"Imported {result['imported']:,} lines.")
        _skipped(out, result)
        _show(out, result)
    state.out().result(result, human)


@move.command('import-carrier-list',
              short_help="Import a carrier's list of discontinued or grandfathered service areas.")
def import_carrier_list(file: Path = typer.Argument(..., metavar='FILE', exists=True, dir_okay=False,
                                                    help="AT&T's Discontinued TDM Service Areas workbook as "
                                                         "downloaded, or another carrier's list as CSV."),
                        carrier: str = typer.Option(None, '--carrier', metavar='NAME',
                                                    help='The carrier whose list this is, when the file has no '
                                                         'carrier column (not needed for AT&T\'s workbook).'),
                        kind: str = typer.Option(None, '--kind', metavar='discontinued|grandfathered',
                                                 help='What the list says about its areas, when the file has no '
                                                      'kind column. Discontinued unless you say otherwise.'),
                        source_url: str = typer.Option(None, '--source-url', metavar='URL',
                                                       help='Where you downloaded it.'),
                        file_date: str = typer.Option(None, '--file-date', metavar='DATE',
                                                      help="The list's own date, such as 2026-08-17."),
                        date_order: str = typer.Option('mdy', '--date-order', metavar='mdy|dmy',
                                                       help='How the file writes dates.')):
    """Import a carrier's list of discontinued or grandfathered service areas. The same carrier's earlier list of
    that kind is kept as history. AT&T's workbook: https://clec.att.com/clec_documents/unrestr/clec/common/
    PrimeAccess_Model-Discontinued_Service_Areas.xlsx. Another carrier's list is a CSV with the columns wire center
    and effective date, and optionally carrier, kind, state, city, wire center name, distribution area and place."""
    if kind is not None and kind not in ('discontinued', 'grandfathered'):
        raise CliError('Choose the kind discontinued or grandfathered.')
    data = {key: value for key, value in (('carrier', carrier), ('kind', kind), ('source_url', source_url),
                                          ('file_date', file_date), ('date_order', date_order)) if value}
    with file.open('rb') as handle:
        result = state.api().post('/routing/carrier-lists/files', data=data,
                                  files={'file': (file.name, handle, 'application/octet-stream')})

    def human(out):
        out.line(f"Imported {result['imported']:,} areas.")
        _skipped(out, result)
        _show(out, result)
    state.out().result(result, human)
