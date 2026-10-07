"""Partner candidates: the numbers whose faxes cost the most, again and again.

A direct partner runs Faxbot too, so faxes to it go straight across the
internet with no call (``direct/``). This module ranks the numbers you fax by
what their faxes cost over the last ``WINDOW_DAYS`` days, every attempt
counted (failed ones too), using the same evidence as cost per delivered fax
(``DeliveredEvidence``). Numbers already enrolled as partners, your own
numbers, and faxes included in a flat monthly plan are left out: there is no
call charge to avoid there.

A number with any attempt that has no price is shown with its cost unknown and
ranked after every priced one, never as $0. The window is 30 days, so its total
is the monthly estimate. Advice only: enrolling is a separate step the
recipient must agree to, under Recipients → Partners.
"""
from .costs import format_amount
from .delivered import WINDOW_DAYS, short_money_text
from .delivered_store import DeliveredEvidence


MIN_FAXES = 3
SHOWN = 5
PARTNERS_PAGE = 'recipients/partners'


def _plural(count, one, many=None):
    return f"{count} {one if count == 1 else (many or one + 's')}"


def candidate(number, figures):
    """What faxes to one number cost over the window on paid routes, or None when nothing was paid per fax."""
    paid = [figure for figure in figures.values() if figure.state not in ('local', 'direct', 'included')]
    if not paid:
        return None
    attempts = sum(figure.attempts for figure in paid)
    delivered = sum(figure.delivered for figure in paid)
    pages = sum(figure.delivered_pages for figure in paid)
    unpriced = sum(figure.unpriced for figure in paid)
    currencies = {figure.currency for figure in paid if figure.currency}
    mixed = any(figure.mixed for figure in paid) or len(currencies) > 1
    known = not unpriced and not mixed and bool(currencies)
    total = sum(figure.cost_micros for figure in paid) if known else None
    return {'number': number, 'attempts': attempts, 'delivered': delivered,
            'average_pages': round(pages / delivered, 1) if delivered else None,
            'currency': next(iter(currencies)) if known else None, 'total_micros': total,
            'estimate': any(figure.estimated for figure in paid), 'unpriced': unpriced, 'mixed': mixed}


def _cost_text(item):
    if item['mixed']:
        return 'Charged in several currencies'
    if item['total_micros'] is None:
        return 'Unknown'
    amount = short_money_text(item['total_micros'], item['currency'])
    return f'About {amount}'


def _sentence(item, days):
    faxes = _plural(item['attempts'], 'fax', 'faxes')
    pages = (f", about {item['average_pages']:g} {'page' if item['average_pages'] == 1 else 'pages'} each"
             if item['average_pages'] else '')
    if item['total_micros'] is None:
        cost = ('some of them were charged in another currency' if item['mixed']
                else f"Faxbot has no price for {_plural(item['unpriced'], 'of them', 'of them')}")
        return (f'You sent {faxes} to this number in the last {days} days{pages}; {cost}, so their monthly cost is '
                'unknown. A direct partner gets faxes with no call.')
    money = short_money_text(item['total_micros'], item['currency'])
    return (f'You sent {faxes} to this number in the last {days} days{pages}, costing about {money} a month '
            '(estimate). If the recipient runs Faxbot and enrolls as a direct partner, those faxes go straight to '
            'them with no call charge.')


def partner_candidates(store, *, now=None):
    """The numbers with the most recurring call spend, each with an "Enroll as direct partner" link."""
    found = DeliveredEvidence(store).by_destination(now=now, timing=False)
    items, few = [], 0
    for number, figures in sorted(found.items()):
        if store.verified_peer(number, now=now) is not None:
            continue
        item = candidate(number, figures)
        if item is None:
            continue
        if item['attempts'] < MIN_FAXES:
            few += 1
            continue
        items.append(item)
    # Priced numbers by what they cost, most first; unknown cost after them, by how often they were faxed.
    items.sort(key=lambda item: (item['total_micros'] is None, -(item['total_micros'] or 0), -item['attempts'],
                                 item['number']))
    shown = []
    for item in items[:SHOWN]:
        row = store.get_destination(item['number']) or {}
        shown.append({'number': item['number'], 'display_name': row.get('display_name'), 'faxes': item['attempts'],
                      'delivered': item['delivered'], 'average_pages': item['average_pages'],
                      'monthly_cost': (None if item['total_micros'] is None
                                       else {'currency': item['currency'], 'amount': format_amount(item['total_micros'])}),
                      'cost_text': _cost_text(item), 'estimate': True, 'unpriced_faxes': item['unpriced'],
                      'sentence': _sentence(item, WINDOW_DAYS), 'link': PARTNERS_PAGE,
                      'link_label': 'Enroll as direct partner'})
    if shown:
        state = 'candidates'
        sentence = (f"{'This number costs' if len(shown) == 1 else 'These numbers cost'} the most to fax again and "
                    'again. A direct partner receives faxes from your Faxbot with no call; each recipient must agree '
                    'and run Faxbot.')
    elif few:
        state = 'too_few'
        sentence = (f'No number you fax had {MIN_FAXES} or more faxes in the last {WINDOW_DAYS} days, so there is no '
                    'partner to suggest yet.')
    else:
        state = 'none'
        sentence = (f'Nothing to suggest: in the last {WINDOW_DAYS} days no fax went by a route that charges per '
                    'call, or every number you fax is already a partner.')
    return {'days': WINDOW_DAYS, 'min_faxes': MIN_FAXES, 'estimate': True, 'state': state, 'sentence': sentence,
            'items': shown, 'items_total': len(items), 'link': PARTNERS_PAGE}
