"""Partner relays: a partner sends your faxes as local calls in its country, or you send theirs."""
import typer

from .. import state
from ..client import segment
from ..errors import CliError
from .delivery import _peer


relay = typer.Typer(help='Partner relays: a partner office sends your faxes to its own country as local calls, '
                         'or you send theirs.', no_args_is_help=True)

ROLE = {'relay': 'You relay for them', 'sender': 'They relay for you'}


def _agreement(api, partner, *, role=None, states=None):
    peer = _peer(api, partner)
    items = [item for item in api.get('/direct/relay/agreements', params={'partner': peer['id']})['agreements']
             if (role is None or item['role'] == role) and (states is None or item['state'] in states)]
    if not items:
        raise CliError(f"There is no matching relay agreement with {peer['organization']}. "
                       "See 'faxbot recipients partners relay list'.")
    return items[-1]


def _show(out, item):
    out.line(item['summary'])
    out.line(item['status'])
    for note in item['notes']:
        out.line(note)
    if item.get('price'):
        for route in item['price']['routes']:
            out.line(route['text'])


@relay.command('list')
def relay_list():
    """List relay agreements both ways: partners who send your faxes, and partners whose faxes you send."""
    items = state.api().get('/direct/relay/agreements')['agreements']
    state.out().result(items, lambda out: out.table(
        ['Partner', 'Direction', 'State', 'Agreement'],
        [[item['partner'], ROLE[item['role']], item['state'], item['summary']] for item in items],
        empty='No relay agreements. Partners can offer to send your faxes as local calls in their country.'))


@relay.command('show')
def relay_show(partner: str = typer.Argument(..., help='Partner organization, fax number or id.')):
    """Show the relay agreements with one partner, their limits, prices and what each side should know."""
    api = state.api()
    peer = _peer(api, partner)
    items = api.get('/direct/relay/agreements', params={'partner': peer['id']})['agreements']

    def human(out):
        if not items:
            out.line(f"No relay agreement with {peer['organization']}.")
        for item in items:
            _show(out, item)
            out.line()
    state.out().result(items, human)


def _hours(text):
    if not text:
        return None
    try:
        days, times = text.split(' ', 1) if ' ' in text else ('mon-fri', text)
        start, until = times.split('-')
    except ValueError:
        raise typer.BadParameter("Write hours as 'mon-fri 08:00-18:00'.", param_hint='--hours') from None
    order = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    chosen = []
    for part in days.lower().split(','):
        if '-' in part:
            first, last = part.split('-', 1)
            if first not in order or last not in order:
                raise typer.BadParameter("Write days as mon-fri or mon,wed,fri.", param_hint='--hours')
            chosen += order[order.index(first):order.index(last) + 1]
        elif part in order:
            chosen.append(part)
        else:
            raise typer.BadParameter("Write days as mon-fri or mon,wed,fri.", param_hint='--hours')
    return {'days': chosen, 'from': start.strip(), 'until': until.strip()}


@relay.command('offer')
def relay_offer(partner: str = typer.Argument(..., help='Partner organization, fax number or id.'),
                country: list[str] = typer.Option([], '--country', help='A country they may send to through you, '
                                                  'by its two-letter code (AU). Repeat for more.'),
                region: list[str] = typer.Option([], '--region', help='A region from your sending rules. Repeat '
                                                 'for more.'),
                pages: int = typer.Option(None, '--pages', min=1, help='Most pages a month. Default: no limit.'),
                spend: str = typer.Option(None, '--spend', help="Most money a month, such as '80 AUD'. Default: no "
                                          'limit.'),
                hours: str = typer.Option(None, '--hours', help="Your hours for their faxes, such as "
                                          "'mon-fri 08:00-18:00'. Default: any time."),
                together: bool = typer.Option(False, '--together', help="Their faxes may share a call with other "
                                              "senders' faxes to the same number."),
                same_organization: bool = typer.Option(False, '--same-organization',
                                                       help='They are an office of your own organization.')):
    """Offer to send a partner's faxes as local calls from here. They accept from their Faxbot."""
    monthly_spend = None
    if spend:
        parts = spend.split()
        if len(parts) != 2:
            raise typer.BadParameter("Write the limit as an amount and a currency, such as '80 AUD'.",
                                     param_hint='--spend')
        monthly_spend = {'amount': parts[0], 'currency': parts[1].upper()}
    api = state.api()
    peer = _peer(api, partner)
    result = api.post('/direct/relay/agreements', json={
        'partner': peer['id'], 'countries': [code.upper() for code in country], 'regions': region,
        'monthly_pages': pages, 'monthly_spend': monthly_spend, 'hours': _hours(hours), 'together': together,
        'same_organization': same_organization})
    state.out().result(result, lambda out: (_show(out, result), out.line(result['detail'])))


@relay.command('accept')
def relay_accept(partner: str = typer.Argument(..., help='Partner organization, fax number or id.'),
                 reply_number: str = typer.Option(None, '--reply-number', help='The number printed on your relayed '
                                                  'faxes for replies. Default: your reply number.'),
                 together: bool = typer.Option(False, '--together', help="Your faxes may share a call with other "
                                               "senders' faxes to the same number."),
                 same_organization: bool = typer.Option(False, '--same-organization',
                                                        help='They are an office of your own organization.'),
                 marketing_business_number: str = typer.Option(None, '--marketing-business-number',
                                                               help='For marketing faxes: your business number '
                                                                    '(such as an ABN), printed on the first page.'),
                 marketing_contact: str = typer.Option(None, '--marketing-contact',
                                                       help='For marketing faxes: how to contact you.'),
                 marketing_opt_out: str = typer.Option(None, '--marketing-opt-out',
                                                       help='For marketing faxes: where recipients ask to stop.')):
    """Accept a partner's offer to send your faxes as local calls in their country."""
    api = state.api()
    item = _agreement(api, partner, role='sender', states=('offered', 'accepting'))
    marketing = {'business_number': marketing_business_number, 'contact': marketing_contact,
                 'opt_out': marketing_opt_out}
    result = api.post(f"/direct/relay/agreements/{segment(item['id'])}/accept", json={
        'reply_number': reply_number, 'together': together, 'same_organization': same_organization,
        'marketing': marketing if any(marketing.values()) else None})
    state.out().result(result, lambda out: (_show(out, result), out.line(result['detail'] or '')))


@relay.command('withdraw')
def relay_withdraw(partner: str = typer.Argument(..., help='Partner organization, fax number or id.')):
    """End a relay agreement with a partner at once. Faxes already accepted for relaying still go."""
    api = state.api()
    item = _agreement(api, partner, states=('offered', 'accepting', 'active'))
    result = api.post(f"/direct/relay/agreements/{segment(item['id'])}/withdraw")
    state.out().result(result, lambda out: out.line(result['detail']))


@relay.command('price')
def relay_price(partner: str = typer.Argument(..., help='Partner organization, fax number or id.')):
    """Give a partner you relay for new prices from your current rate cards."""
    api = state.api()
    item = _agreement(api, partner, role='relay', states=('offered', 'active'))
    result = api.post(f"/direct/relay/agreements/{segment(item['id'])}/price")
    state.out().result(result, lambda out: out.line(result['detail']))


@relay.command('quote')
def relay_quote(partner: str = typer.Argument(..., help='Partner organization, fax number or id.'),
                country: list[str] = typer.Option(..., '--country', help='A country to price, by its two-letter '
                                                  'code (AU). Repeat for more.')):
    """Ask a partner what sending your faxes through them would cost. Nothing is relayed."""
    api = state.api()
    peer = _peer(api, partner)
    result = api.post(f"/direct/relay/partners/{segment(peer['id'])}/quote",
                      json={'countries': [code.upper() for code in country]})
    state.out().result(result, lambda out: out.line(result['detail']))


@relay.command('costs')
def relay_costs(days: int = typer.Option(30, '--days', min=1, max=366, help='How many days back to count.')):
    """Show what relays carried and cost: faxes you relayed for partners, and faxes partners sent for you."""
    result = state.api().get('/direct/relay/costs', params={'days': days})

    def human(out):
        if not result['agreements']:
            out.line(f'No faxes went through a partner relay in the last {days} days.')
        for item in result['agreements']:
            out.line(item['sentence'])
    state.out().result(result, human)


@relay.command('suggestions')
def relay_suggestions(days: int = typer.Option(30, '--days', min=1, max=366, help='How many days back to count.')):
    """Show partners whose local price would have cost less than your own calls lately."""
    result = state.api().get('/direct/relay/recommendations', params={'days': days})

    def human(out):
        if not result['recommendations']:
            out.line('No partner would have sent your recent faxes for less.')
        for item in result['recommendations']:
            out.line(f"{item['sentence']} {item['action']}")
    state.out().result(result, human)


@relay.command('faxes')
def relay_faxes(days: int = typer.Option(30, '--days', min=1, max=366, help='How many days back to list.')):
    """List faxes relayed for partners and faxes partners relayed for you, newest first."""
    items = state.api().get('/direct/relay/faxes', params={'days': days})['faxes']
    from ..output import local_time
    state.out().result(items, lambda out: out.table(
        ['Sent', 'Direction', 'Partner', 'Fax number', 'Pages', 'Result'],
        [[local_time(item['created_at']), 'For them' if item['role'] == 'relay' else 'For you', item['partner'],
          item['fax_number'], item['pages'] if item['pages'] is not None else '-', item['status']]
         for item in items], empty='No relayed faxes.'))
