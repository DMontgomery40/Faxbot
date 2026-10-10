"""Exchange rates: `faxbot savings plans rates list|set|clear|refresh|reference` (brief 99, item 1).

``delivery.py`` attaches ``rates`` to the plans group, as the console shows them on Prices & plans. Rates only
compare prices in different currencies; no stored price changes currency.
"""
import typer

from .. import state


rates = typer.Typer(help='Exchange rates Faxbot compares prices in different currencies with: the ECB reference '
                         'rates and your own rates.', no_args_is_help=True)


def _show(out, result):
    reference = result['reference']
    out.line(reference['sentence'])
    own = result.get('own') or []
    if own:
        out.table(['Your rate', 'For', 'In use'],
                  [[f"1 {item['base']} = {item['rate']} {item['quote']}", item['date_text'],
                    'Yes' if item['current'] else f"No, more than {result['own_max_age_days']} days old"]
                   for item in own], title='Your own rates')
    else:
        out.line('You have not set any rates of your own. Set one with: faxbot savings plans rates set GBP USD 1.27')
    if reference.get('rates'):
        out.table(['Currency', 'Per euro'], [[item['currency'], item['per_euro']] for item in reference['rates']],
                  title=f"ECB reference rates of {reference['date_text']}")


@rates.command('list')
def rates_list():
    """Show the exchange rates Faxbot compares prices with, and how old each may be."""
    result = state.api().get('/routing/exchange-rates')
    state.out().result(result, lambda out: _show(out, result))


@rates.command('set')
def rates_set(base: str = typer.Argument(..., metavar='FROM', help='The currency one unit of which the rate is for, '
                                                                    'such as GBP.'),
              quote: str = typer.Argument(..., metavar='TO', help='The currency the rate is in, such as USD.'),
              rate: str = typer.Argument(..., metavar='RATE', help='How many TO one FROM is worth, such as 1.27.'),
              rate_date: str = typer.Option(None, '--date', metavar='YYYY-MM-DD',
                                            help='The day the rate is for, such as the date on your invoice; today '
                                                 'when left out.'),
              note: str = typer.Option(None, '--note', help='Where the rate comes from, such as "May invoice".')):
    """Set your own rate for a currency pair, such as the one on your invoice; it is used instead of the ECB rate."""
    body = {'base': base.upper(), 'quote': quote.upper(), 'rate': rate}
    if rate_date:
        body['rate_date'] = rate_date
    if note:
        body['note'] = note
    result = state.api().put('/routing/exchange-rates/own', json=body)

    def human(out):
        mine = next((item for item in result.get('own') or [] if {item['base'], item['quote']}
                     == {body['base'], body['quote']}), None)
        out.line(mine['sentence'] if mine else 'Saved.')
    state.out().result(result, human)


@rates.command('clear')
def rates_clear(base: str = typer.Argument(..., metavar='FROM', help='One currency of the pair, such as GBP.'),
                quote: str = typer.Argument(..., metavar='TO', help='The other currency, such as USD.')):
    """Stop using your own rate for a currency pair; the ECB rate counts again."""
    result = state.api().post('/routing/exchange-rates/own/clear', json={'base': base.upper(), 'quote': quote.upper()})
    state.out().result(result, lambda out: out.line(
        f'Faxbot no longer uses your rate between {base.upper()} and {quote.upper()}; the ECB rate counts again.'))


@rates.command('refresh')
def rates_refresh():
    """Read the ECB reference rates now."""
    result = state.api().post('/routing/exchange-rates/refresh')

    def human(out):
        found = result.get('refreshed') or {}
        if found.get('outcome') == 'failed':
            out.line(f"Faxbot could not read the ECB reference rates: {found.get('error')}")
        elif found.get('outcome') == 'unchanged':
            out.line(f"The ECB has published nothing newer than the rates of {result['reference']['date_text']}.")
        else:
            out.line(f"Read the ECB reference rates of {result['reference']['date_text']}.")
    state.out().result(result, human)


@rates.command('reference')
def rates_reference(switch: str = typer.Argument(..., metavar='on|off',
                                                 help='on reads the ECB reference rates; off stops reading them.')):
    """Turn reading the ECB reference rates on or off (on by default)."""
    value = switch.strip().lower()
    if value not in ('on', 'off'):
        raise typer.BadParameter('Use on or off.')
    result = state.api().put('/routing/exchange-rates/reference', json={'enabled': value == 'on'})
    state.out().result(result, lambda out: out.line(result['reference']['sentence']))
