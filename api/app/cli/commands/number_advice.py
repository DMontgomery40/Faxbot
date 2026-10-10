"""Number advice on the command line: where each number should live, your NPI record, the check before a first
fax, and US prices by where a call starts.

``faxbot savings opportunities numbers`` and ``sites``, ``faxbot admin npi``, ``faxbot recipients check`` and
``faxbot savings state-prices``. Everything here advises; nothing ports a number, changes caller ID or stops a fax.
"""
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..output import money


def _read_on(value):
    from datetime import date
    try:
        day = date.fromisoformat(str(value)[:10])
    except ValueError:
        return '-'
    return f'{day.day} {day:%B %Y}'


# -- where each number should live -------------------------------------------------------------------------------

def read_numbers(api):
    return api.get('/routing/recommendations/numbers')


def show_numbers(out, result):
    """Each number, what it costs where it is and at your other accounts, and the steps to move it."""
    out.line(result.get('sentence') or '')
    rows = result.get('numbers') or []
    if rows:
        out.table(['Number', 'Lives at', 'Faxes received', 'A month there', 'Cheapest', 'Saves a month'],
                  [[row['display'], row['account'], row['received'],
                    money(next((cost['monthly'] for cost in row['costs'] if cost['current']), []), empty='Not known'),
                    row.get('cheapest') or row['account'], money(row.get('saving'), empty='-')] for row in rows],
                  title=f"Where each number should live (estimate, last {result.get('days', 30)} days)")
    for row in rows:
        if row.get('state') == 'keep' and not row.get('notes'):
            continue
        out.line('')
        out.line(row['sentence'])
        for note in row.get('notes') or []:
            out.line(note)
        _steps(out, row.get('porting'))
    for account in result.get('accounts') or []:
        out.line('')
        out.line(account['sentence'])
        for steps in account.get('porting') or []:
            _steps(out, steps)
    if result.get('note'):
        out.line('')
        out.line(result['note'])


def _steps(out, steps):
    if not steps:
        return
    out.line(f"To move it from {steps['from']} to {steps['to']}:")
    for index, step in enumerate(steps['steps'], 1):
        out.line(f'  {index}. {step}')
    out.line(f"  Fee: {steps['fee']}")
    out.line(f"  Usual time: {steps['lead_time']}")
    if steps.get('restriction'):
        out.line(f"  Watch out: {steps['restriction']}")
    for source in steps.get('sources') or []:
        mark = ' (review site)' if source.get('secondary') else ''
        out.line(f"  Source{mark}: {source['url']} (read {_read_on(source.get('read_on'))})")


# -- which site's trunk costs less ------------------------------------------------------------------------------------

def read_sites(api):
    return api.get('/routing/recommendations/sites')


def show_sites(out, result):
    """Whether each trunk's carrier prices US calls by state, and where another site's trunk costs less."""
    out.line(result.get('sentence') or '')
    for carrier in result.get('carriers') or []:
        if carrier['sentence'] != result.get('sentence'):
            out.line(carrier['sentence'])
    for item in result.get('items') or []:
        out.line(item['sentence'])
        out.line(item['action'])
    if result.get('prices'):
        out.table(['Carrier', 'Number prefixes', 'Priced differently within a state', 'Read on', 'Source'],
                  [[item['carrier'], item['rows'], item['differ'], _read_on(item.get('read_on')),
                    item.get('source_url') or '-'] for item in result['prices']],
                  title='US prices by where a call starts')
    out.line(result.get('caller_id') or '')


# -- your NPI record --------------------------------------------------------------------------------------------------

npi = typer.Typer(help='Your NPI numbers, so Faxbot can tell you when a number you might give up is still printed on '
                       'your NPI record.', no_args_is_help=True)


def _show_npi(out, view):
    out.line(view.get('sentence') or '')
    if view.get('problem'):
        out.line(view['problem'])
    for item in view.get('npis') or []:
        title = f"NPI {item['npi']}" + (f" ({item['label']})" if item.get('label') else '') + (
            f": {item['name']}" if item.get('name') else '')
        out.line(title)
        out.table(['Number', 'Kind', 'Where'],
                  [[number['display'], 'Fax' if number['kind'] == 'fax' else 'Phone', number['where']]
                   for number in item.get('numbers') or []],
                  empty='No numbers read yet.')


@npi.command('list')
def npi_list():
    """Show your NPIs and the numbers the NPI registry (NPPES) lists for each."""
    view = state.api().get('/routing/npi')
    state.out().result(view, lambda out: _show_npi(out, view))


@npi.command('add')
def npi_add(number: str = typer.Argument(..., help='Your ten-digit NPI.'),
            label: str = typer.Option('', '--label', help='A name for this location, such as Denver office.')):
    """Add one of your NPIs (one per location if you have several) and read its record from NPPES."""
    view = state.api().post('/routing/npi', json={'npi': number, 'label': label})
    state.out().result(view, lambda out: _show_npi(out, view))


@npi.command('remove')
def npi_remove(number: str = typer.Argument(..., help='The NPI to remove.')):
    """Stop treating an NPI as yours. What Faxbot read for it stays as history."""
    view = state.api().delete('/routing/npi/' + segment(number))
    state.out().result(view, lambda out: _show_npi(out, view))


@npi.command('check')
def npi_check():
    """Read each of your NPIs from NPPES now."""
    view = state.api().post('/routing/npi/check')
    state.out().result(view, lambda out: _show_npi(out, view))


# -- the check before a first fax -------------------------------------------------------------------------------------

def recipient_check(number: str = typer.Argument(..., help='The fax number you are about to send to.'),
                    name: str = typer.Option(None, '--name', help='The provider or person the fax is for.')):
    """Before a first fax: check whether the NPI registry (NPPES) lists this number for the provider you name. It
    only warns; it never stops a fax."""
    result = recipient_lookup(state.api(), number, name)

    def human(out):
        if result.get('sentence'):
            out.line(result['sentence'])
        elif not result.get('first_send'):
            out.line('You have faxed this number before, so Faxbot does not check it again.')
        else:
            out.line('Nothing to check: give the recipient\'s name with --name.' if result.get('state') == 'no_name'
                     else 'NPPES lists only US providers, so this number is not checked.')
    state.out().result(result, human)


def recipient_lookup(api, number, name=None):
    params = {'to': number}
    if name:
        params['name'] = name
    return api.get('/routing/recipient-check', params=params)


# -- US prices by where a call starts ----------------------------------------------------------------------------------

def state_prices(carrier: str = typer.Argument(..., help='The carrier, such as anveo.'),
                 file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                             help="The carrier's price file (CSV) with a price for calls between "
                                                  'states and within one state.'),
                 source: str = typer.Option(None, '--source', help='Where you got the file, such as its web address.'),
                 read_on: str = typer.Option(None, '--read-on', metavar='DATE',
                                             help='The date you downloaded it, such as 2026-10-08.')):
    """Import a carrier's US prices for calls within one state and between states. Faxbot prices each call from the
    state of its trunk's site, and never changes caller ID to lower a charge."""
    data = {'carrier': carrier}
    if source:
        data['source_url'] = source
    if read_on:
        data['read_on'] = read_on
    with file.open('rb') as handle:
        result = state.api().post('/routing/jurisdiction-rates', data=data,
                                  files={'file': (file.name, handle, 'text/csv')})

    def human(out):
        for item in result.get('prices') or []:
            out.line(f"{item['carrier']}: {item['rows']:,} number prefixes, {item['differ']:,} priced differently "
                     'within one state.')
    state.out().result(result, human)
