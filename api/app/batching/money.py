"""What a shared call cost each fax, and what sending together saved a number (an estimate).

Reads the delivery-route ledger only through ``RouteStore`` read functions. The
call's charge is on the attempt that placed it: the carrier's reported amount
when there is one, otherwise Faxbot's rate-card estimate from the measured call.
"""
from datetime import timedelta

from ..routing.costs import Money, estimate_cost, money_text, split_by_weight
from ..routing.database import utcnow
from . import policy
from .store import call_members, calls_to


WINDOW_DAYS = 30


def call_charge(routes, batch_id):
    """``(micros, currency, basis)`` for the call ``batch_id`` placed, or None while it is unknown."""
    row = routes.decision(batch_id)
    if row is None or row['outcome'] in ('pending', 'cancelled'):
        return None
    if row['reported_cost_micros'] is not None and row['reported_currency']:
        return row['reported_cost_micros'], row['reported_currency'], 'reported'
    if row['estimated_cost_micros'] is not None and row['currency']:
        return row['estimated_cost_micros'], row['currency'], 'estimated'
    return None


def is_index_page(member):
    return member.get('layout') == policy.LAYOUT_INDEX_PAGE


def weight(member):
    """A fax's pages in its call: its own pages, plus its separator page when the call used separators.

    With one index page, the index page is shared in proportion to each fax's own pages.
    """
    return member['pages'] if is_index_page(member) else member['pages'] + 1


def share(routes, engine, member):
    """This fax's share of its call's charge, split by pages (see ``weight``).

    The shares of one call sum exactly to its charge: largest remainder, in call order then fax ID
    (``routing.costs.split_by_weight``).
    """
    if member is None or member['state'] != 'together' or not member['batch_id']:
        return None
    charge = call_charge(routes, member['batch_id'])
    if charge is None:
        return None
    micros, currency, basis = charge
    members = sorted(call_members(engine, member['batch_id']),
                     key=lambda row: (row['document_number'] or 0, row['id']))
    positions = [row['id'] for row in members]
    if member['id'] not in positions or not sum(weight(row) for row in members):
        return None
    part = split_by_weight(micros, [weight(row) for row in members])[positions.index(member['id'])]
    estimate = '' if basis == 'reported' else ' (estimate)'
    return {'amount_micros': part, 'call_micros': micros, 'currency': currency, 'basis': basis,
            'sentence': f"Its share of the call's charge, split by pages: {money_text(part, currency)} "
                        f'of {money_text(micros, currency)}{estimate}.'}


def index_page_saving(card, members):
    """The pages one index page saved a call compared with a separator before each fax, and their price.

    ``(pages, micros)``, priced with ``card`` the way Faxbot estimates any call; micros is None when
    the card cannot price it (no card, or a flat plan): unknown stays unknown, never 0.
    """
    pages = len(members) - 1
    if card is None or card.flat_plan:
        return pages, None
    own = sum(member['pages'] for member in members)
    return pages, estimate_cost(card, own + len(members)) - estimate_cost(card, own + 1)


def savings(routes, engine, number, *, now=None, days=WINDOW_DAYS):
    """Calls saved and the estimated money saved by sending together to ``number`` in the last ``days``.

    Separate calls are priced with the SIP trunk's rate card the way Faxbot
    estimates any fax (setup plus time per page, with the card's minimum and
    rounding); the shared call at its reported charge, or its rate-card cost.
    A shared call that cost more than the separate calls is a negative saving:
    summed as it is, never turned into 0.

    A call that started with one index page also left out its separator
    pages. Those are counted apart, in ``index_page``, for calls whose every
    fax was delivered, and priced with the rate card. The call saving then
    leaves that part out, so the two together are still the separate calls'
    price less what the shared call cost.
    """
    calls = calls_to(engine, number, (now or utcnow()) - timedelta(days=days))
    card = routes.card_for('sip')
    result = {'calls': 0, 'faxes': 0, 'calls_saved': 0, 'saved': {}, 'priced_calls': 0,
              'index_page': {'calls': 0, 'pages_saved': 0, 'priced_calls': 0, 'saved': {}}}
    index = result['index_page']
    for batch_id, members in calls.items():
        charge = call_charge(routes, batch_id)
        if charge is None:
            continue
        result['calls'] += 1
        result['faxes'] += len(members)
        result['calls_saved'] += len(members) - 1
        micros, currency, _ = charge
        priced = card is not None and card.currency == currency
        pages_saving = None
        if is_index_page(members[0]) and all(member.get('delivery_state') == 'success' for member in members):
            pages, pages_saving = index_page_saving(card if priced else None, members)
            index['calls'] += 1
            index['pages_saved'] += pages
            if pages_saving is not None:
                index['priced_calls'] += 1
                index['saved'][currency] = index['saved'].get(currency, 0) + pages_saving
        if not priced:
            continue
        separate = Money(0, currency)
        for member in members:
            separate += Money(estimate_cost(card, member['pages']), currency)
        saved = separate - Money(int(micros), currency) - Money(pages_saving or 0, currency)
        result['priced_calls'] += 1
        result['saved'][currency] = result['saved'].get(currency, 0) + saved.micros
    return result


def savings_sentence(result):
    if not result['calls']:
        return 'No faxes to this number have been sent together in the last 30 days.'
    calls = '1 call' if result['calls_saved'] == 1 else f"{result['calls_saved']} calls"
    faxes = f"{result['faxes']} faxes in {result['calls']} call" + ('' if result['calls'] == 1 else 's')
    saved = ' + '.join(money_text(micros, currency) for currency, micros in sorted(result['saved'].items())
                       if micros >= 0)
    more = ' + '.join(money_text(-micros, currency) for currency, micros in sorted(result['saved'].items())
                      if micros < 0)
    if more and saved:
        return (f'Last 30 days: {faxes}, {calls} saved, about {saved} saved, and sending together cost about '
                f'{more} more (estimate).')
    if more:
        return f'Last 30 days: {faxes}, {calls} saved, but sending together cost about {more} more (estimate).'
    if saved:
        return f'Last 30 days: {faxes}, {calls} saved, about {saved} saved (estimate).'
    return f'Last 30 days: {faxes}, {calls} saved.'


def index_page_sentence(part, *, days=WINDOW_DAYS):
    """One sentence for the separator pages one index page left out (a ``savings(...)['index_page']`` part)."""
    if not part['calls']:
        return f'No call in the last {days} days started with one index page instead of separator pages.'
    pages = '1 separator page' if part['pages_saved'] == 1 else f"{part['pages_saved']} separator pages"
    calls = '1 call' if part['calls'] == 1 else f"{part['calls']} calls"
    sentence = f'One index page instead of a separator before each document left out {pages} in {calls}'
    amounts = ' + '.join(money_text(micros, currency) for currency, micros in sorted(part['saved'].items())
                         if micros > 0)
    if amounts:
        sentence += f', about {amounts} saved (estimate).'
    elif part['saved']:
        sentence += ", but on your carrier's billing those pages cost nothing extra."
    else:
        sentence += '.'
    if part['priced_calls'] < part['calls']:
        sentence += " Some of those pages have no price, because your carrier's prices are not entered in Costs."
    return sentence
