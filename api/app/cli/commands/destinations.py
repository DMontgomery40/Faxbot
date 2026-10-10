"""Where Faxbot may dial: classes of numbers and countries, as Providers → In use shows them.

`faxbot providers rules destinations list` shows each class of numbers (your own country's numbers,
toll-free and mobile numbers, premium-rate, special-service and satellite numbers) and each country
Faxbot knows about, with whether it may dial it and why. `allow` and `block` change one; `--ceiling`
sets the highest price a minute a call to it may cost before the fax waits for your approval.
A fax to a class Faxbot may not dial waits in Sent for approval; nothing is dialed.
"""
import typer

from .. import state
from ..client import segment
from ..output import local_time

destinations = typer.Typer(
    help='Where Faxbot may dial: numbers in your country, other countries, and premium-rate, special-service '
         'and satellite numbers. Faxes to anything not allowed wait in Sent for your approval.',
    no_args_is_help=True)

CLASS_HELP = ('national, national-toll-free, national-mobile, premium, special-service, satellite, a two-letter '
              'country code such as GB, or a calling code such as +44.')
COLUMNS = ['Numbers', 'May dial', 'Why', 'Price ceiling']


def _row(item):
    ceiling = (item.get('ceiling') or {}).get('text') or 'No ceiling'
    why = item.get('sentence') or ''
    if item.get('first_delivered_at'):
        why += f" First delivered {local_time(item['first_delivered_at'])}."
    return [item.get('label') or item.get('key'), 'Yes' if item.get('allowed') else 'No', why, ceiling]


def _show(out, result):
    out.table(COLUMNS, [_row(item) for item in result.get('classes') or ()])
    out.table(COLUMNS, [_row(item) for item in result.get('countries') or ()], title='Countries',
              empty='No other country yet.')
    prefixes = result.get('prefixes') or ()
    if prefixes:
        out.table(['Numbers starting', 'Named by the rule'], [[item['prefix'], item['rule']] for item in prefixes])
    if result.get('other_countries'):
        out.line(result['other_countries'])


@destinations.command('list')
def destinations_list():
    """Show what Faxbot may dial and why."""
    result = state.api().get('/routing/dialing')
    state.out().result(result, lambda out: _show(out, result))


def _change(class_name, state_value, ceiling):
    body = {'state': state_value}
    if ceiling is not None:
        body['ceiling'] = '' if ceiling.strip().lower() in ('', 'none') else ceiling.strip()
    result = state.api().put(f'/routing/dialing/{segment(class_name)}', json=body)
    state.out().result(result, lambda out: out.line(result.get('sentence') or ''))


@destinations.command('allow')
def destinations_allow(
        numbers: str = typer.Argument(..., metavar='CLASS', help=f'What to allow: {CLASS_HELP}'),
        ceiling: str = typer.Option(None, '--ceiling', metavar='PRICE',
                                    help='The highest price a minute a call may cost before the fax waits for your '
                                         'approval, such as 0.25; none removes it.')):
    """Let Faxbot dial a class of numbers or a country without asking."""
    _change(numbers, 'allowed', ceiling)


@destinations.command('block')
def destinations_block(numbers: str = typer.Argument(..., metavar='CLASS', help=f'What to block: {CLASS_HELP}')):
    """Hold every fax to a class of numbers or a country in Sent for your approval."""
    _change(numbers, 'blocked', None)


@destinations.command('reset')
def destinations_reset(
        numbers: str = typer.Argument(..., metavar='CLASS', help=f'What to put back: {CLASS_HELP}'),
        ceiling: str = typer.Option(None, '--ceiling', metavar='PRICE',
                                    help='A price ceiling a minute to keep, such as 0.25; none removes it.')):
    """Put a class of numbers or a country back to Faxbot's own default."""
    _change(numbers, 'default', ceiling)
