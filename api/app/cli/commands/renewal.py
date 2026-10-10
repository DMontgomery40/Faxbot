"""Fax server renewal commands (N20, N24): channels at measured peak, and one page for the renewal.

They hang off ``faxbot costs recommendations``; ``line_inventory.py`` imports this module so they register with that
group. Nothing here changes a licence, cancels a renewal or contacts a vendor.
"""
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from .delivery import recommendations
from .trunk import local_date


def _channels(out, result):
    if result.get('sentence'):
        out.line(result['sentence'])
    for system in result.get('systems') or []:
        found = system['report']
        out.line(f"{system['system']} ({system['source']}): {found['sentence']}")
        busy = [item for item in found.get('profile') or [] if item['peak']]
        if busy:
            out.table(['Hour', '99 hours in 100', 'Most at once'],
                      [[f"{item['hour']:02d}:00", item['p99'], item['peak']] for item in busy])
        for item in system.get('imports') or []:
            out.line(f"  {item['format_label']}: {item['file_name'] or 'a file'}, {item['calls']:,} calls "
                     f"(import code {item['id']}, to leave it out with --remove)")
    out.line(result.get('note') or '')


def _skipped(out, result):
    count = result.get('skipped_count') or 0
    if count:
        out.line(f"{count:,} {'line was' if count == 1 else 'lines were'} not read:")
        for reason in result.get('skipped') or []:
            out.line(f'  {reason}')


@recommendations.command('channels')
def channels(remove: str = typer.Option(None, '--remove', metavar='IMPORT',
                                        help='Leave one imported file out of the report, by the import code shown '
                                             'beside it; its calls stay as history.')):
    """Show how many calls your fax systems carried at once: at their peak, in 99 hours out of 100, and by hour of
    the day, with the licensed channels never needed."""
    api = state.api()
    result = api.delete(f'/routing/channels/imports/{segment(remove.strip())}') if remove else \
        api.get('/routing/channels')
    state.out().result(result, lambda out: _channels(out, result))


@recommendations.command('import-calls',
                         short_help="Import another fax server's or phone system's call records for one system.")
def import_calls(file: Path = typer.Argument(..., metavar='FILE', exists=True, dir_okay=False,
                                             help='The call records file.'),
                 system: str = typer.Option(..., '--system', metavar='NAME',
                                            help='The system they come from, such as "RightFax at HQ".'),
                 source_format: str = typer.Option(None, '--format', metavar='asterisk|cucm|rightfax|faxmaker|csv',
                                                   help='The file\'s format; Faxbot recognises it when left out.'),
                 licensed: int = typer.Option(None, '--licensed', min=1, max=10_000, metavar='CHANNELS',
                                              help='The channels the system is licensed for.'),
                 time_zone: str = typer.Option(None, '--time-zone', metavar='ZONE',
                                               help='The time zone of the times in the file, such as America/Denver; '
                                                    "the installation's when left out."),
                 numbers: str = typer.Option(None, '--numbers', metavar='NUMBERS',
                                             help='Only calls to or from these numbers (comma-separated), for a '
                                                  "phone system's records that include voice calls.")):
    """Import call records for one system. Formats: Asterisk's Master.csv, Cisco Unified CM CDR files, RightFax's
    DocTransport audit log (level 3 or 4), GFI FaxMaker's activity export, or a CSV with the columns start, end or
    duration, direction, channel and number."""
    if source_format is not None and source_format not in ('asterisk', 'cucm', 'rightfax', 'faxmaker', 'csv'):
        raise CliError('Choose the format asterisk, cucm, rightfax, faxmaker or csv.')
    data = {key: value for key, value in (('system', system), ('source_format', source_format),
                                          ('licensed', str(licensed) if licensed else None),
                                          ('time_zone', time_zone), ('numbers', numbers)) if value}
    with file.open('rb') as handle:
        result = state.api().post('/routing/channels/files', data=data,
                                  files={'file': (file.name, handle, 'application/octet-stream')})

    def human(out):
        out.line(f"Imported {result['imported']:,} calls.")
        _skipped(out, result)
        _channels(out, result)
    state.out().result(result, human)


def _pages(out, result):
    if result.get('sentence'):
        out.line(result['sentence'])
    for found in result.get('pages') or []:
        out.line(found['system'])
        for sentence in found['sentences']:
            out.line(f'  {sentence}')
        for route in ((found.get('left') or {}).get('numbers') or [])[:50]:
            out.line(f"    {route['display']}" + (f" — {route['user_name']}" if route.get('user_name') else '')
                     + (f" ({route['user_email']})" if route.get('user_email') else ''))
        for item in found.get('reference') or []:
            out.line(f"  For comparison: {item['sentence']} {item['label']}, read {local_date(item['read_on'])}: "
                     f"{item['source']}")
    out.line(result.get('note') or '')


@recommendations.command('renewal', short_help="One page for a fax server's renewal; record it with --system.")
def renewal(system: str = typer.Option(None, '--system', metavar='NAME', help='The fax server, such as "RightFax at '
                                                                              'HQ".'),
            renews: str = typer.Option(None, '--renews', metavar='DATE', help='The renewal date, such as 2027-05-31.'),
            amount: str = typer.Option(None, '--amount', metavar='AMOUNT', help='The renewal amount, such as '
                                                                                '26756.71.'),
            currency: str = typer.Option('USD', '--currency', metavar='CODE', help='The amount\'s currency.'),
            product: str = typer.Option('', '--product', metavar='NAME', help='The product, such as RightFax 22.2.'),
            channels_licensed: int = typer.Option(None, '--channels', min=1, max=10_000, metavar='CHANNELS',
                                                  help='The channels the renewal licenses.'),
            source_url: str = typer.Option(None, '--source-url', metavar='URL',
                                           help='Where the amount comes from, such as the quote.'),
            parallel: str = typer.Option('', '--parallel', metavar='NUMBERS',
                                         help='The numbers Faxbot runs beside it (comma-separated).'),
            parallel_since: str = typer.Option(None, '--parallel-since', metavar='DATE',
                                               help='When the parallel run began.'),
            remove: bool = typer.Option(False, '--remove', help="Withdraw the system's renewal.")):
    """Show one page per fax server renewal: the amount and date, the channels it really needed, what Faxbot handled
    beside it, and the numbers still to move. With --system, --renews and --amount, record the renewal."""
    api = state.api()
    if remove:
        if not system:
            raise CliError('Name the system with --system.')
        result = api.post('/routing/renewals/remove', json={'system': system})
    elif system:
        if not renews or not amount:
            raise CliError('Give the renewal date with --renews and the amount with --amount.')
        result = api.put('/routing/renewals', json={
            'system': system, 'renews_on': renews, 'amount': amount, 'currency': currency, 'product': product,
            'licensed_channels': channels_licensed, 'source_url': source_url,
            'parallel_numbers': [item.strip() for item in parallel.split(',') if item.strip()],
            'parallel_since': parallel_since})
    else:
        result = api.get('/routing/renewals')
    state.out().result(result, lambda out: _pages(out, result))


@recommendations.command('import-routing',
                         short_help="Import a fax server's number-to-user routing (number, user, email, cover sheet).")
def import_routing(file: Path = typer.Argument(..., metavar='FILE', exists=True, dir_okay=False,
                                               help='A CSV with the columns number, user, email and cover sheet.'),
                   system: str = typer.Option(..., '--system', metavar='NAME', help='The fax server it comes from.')):
    """Import a fax server's number-to-user routing, so the renewal page lists the numbers and users still to
    move. It replaces the system's earlier routing, which is kept as history."""
    with file.open('rb') as handle:
        result = state.api().post('/routing/renewals/routes', data={'system': system},
                                  files={'file': (file.name, handle, 'text/csv')})

    def human(out):
        out.line(f"Imported {result['imported']:,} numbers.")
        _skipped(out, result)
        _pages(out, result)
    state.out().result(result, human)
