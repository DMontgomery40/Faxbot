"""Cost per delivered fax, per destination and route, from what every attempt cost.

Pure: no I/O. Money is integer micros, and a division rounds up, like an invoice.

Each attempt's cost is, in this order:

1. the charge the carrier or provider reported for it;
2. otherwise Faxbot's estimate from when it finished: the route's rate card
   applied to the call's measured (or timed) duration and pages, with the
   card's minimum charge and billing step;
3. otherwise the route's current rate card applied the same way, an estimate too;
4. otherwise unknown. Unknown cost is never counted as zero.

Every attempt counts toward a route's cost: failed, cancelled and uncertain ones
too, because a carrier bills a call whether or not the fax got through. Only
delivered faxes divide it, so a route that often needs a second call costs more
per delivered fax than its rate card suggests.

A route whose rate card is a flat monthly plan is "included in the plan": it
has no cost per fax, never a $0 "cheapest". A direct delivery has no fax call, and
a fax to one of the installation's own numbers is delivered inside Faxbot with no call.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .costs import MICROS, attempt_cost, estimate_cost


# The same evidence window as route reliability (``store.WINDOW_DAYS``).
WINDOW_DAYS = 30
# Route choice compares routes only when each has at least this many delivered faxes
# in the window: the same bar as the reliability rule (``plan.MIN_ATTEMPTS``). With
# fewer, one long call or one charged failure decides the average on its own.
MIN_DELIVERED = 3
OUTCOMES = ('success', 'failed', 'uncertain', 'cancelled')


@dataclass(frozen=True)
class Attempt:
    """What Faxbot knows about one placed attempt on a route to a destination."""
    route: str
    provider_id: str
    outcome: str
    reported_micros: int | None = None
    reported_currency: str | None = None
    estimated_micros: int | None = None
    currency: str | None = None
    # The fax's pages; connected seconds measured on the trunk; seconds from submission to the result.
    pages: int | None = None
    seconds: int | None = None
    elapsed_seconds: int | None = None


def short_money_text(micros, currency):
    """An average for a sentence, as the console and ``faxbot`` show money: "$0.0089", "$0.38", "0.0089 EUR".

    Two decimal places, or up to four under ten cents; the figure itself stays exact in micros.
    """
    amount = Decimal(abs(micros)) / MICROS
    places = 4 if amount and amount < Decimal('0.1') else 2
    shown = f"{amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):f}"
    while places > 2 and shown.endswith('0'):
        shown, places = shown[:-1], places - 1
    sign = '-' if micros < 0 else ''
    return f'{sign}${shown}' if currency == 'USD' else f'{sign}{shown} {currency}'


def attempt_figure(attempt, card=None):
    """``(micros, currency, basis)`` for one attempt, ``basis`` ``reported`` or ``estimated``; None when unknown."""
    if attempt.reported_micros is not None and attempt.reported_currency:
        return int(attempt.reported_micros), attempt.reported_currency, 'reported'
    if attempt.estimated_micros is not None and attempt.currency:
        return int(attempt.estimated_micros), attempt.currency, 'estimated'
    if card is None or card.flat_plan:
        return None
    if attempt.outcome == 'uncertain':
        # It may have been sent: estimated as a successful send, as the cost recorder does.
        return estimate_cost(card, attempt.pages), card.currency, 'estimated'
    seconds = attempt.seconds if attempt.seconds is not None else attempt.elapsed_seconds
    if seconds is None and card.per_minute_micros:
        return None
    micros = attempt_cost(card, seconds=seconds or 0, pages=attempt.pages, delivered=attempt.outcome == 'success')
    return micros, card.currency, 'estimated'


@dataclass(frozen=True)
class DeliveredCost:
    """One route's attempts to one destination in the window and what they cost."""
    route: str
    provider_id: str
    attempts: int = 0
    delivered: int = 0
    failed: int = 0
    uncertain: int = 0
    cancelled: int = 0
    # The known part of the cost, in ``currency``; ``mixed`` when attempts were billed in several currencies.
    cost_micros: int = 0
    currency: str | None = None
    mixed: bool = False
    # How many attempts were priced by a reported charge, by an estimate, or not at all.
    reported: int = 0
    estimated: int = 0
    unpriced: int = 0
    plan: bool = False
    direct: bool = False
    local: bool = False
    # Delivered faxes: their pages, and connected seconds for those with a measured call.
    delivered_pages: int = 0
    connected_seconds: int = 0
    timed: int = 0

    @property
    def state(self):
        """``local``, ``direct``, ``included``, ``mixed``, ``unpriced``, ``undelivered`` or ``priced``."""
        if self.local:
            return 'local'
        if self.direct:
            return 'direct'
        if self.plan:
            return 'included'
        if self.mixed:
            return 'mixed'
        if self.unpriced:
            return 'unpriced'
        if not self.delivered:
            return 'undelivered'
        return 'priced'

    @property
    def per_delivered_micros(self):
        """Every attempt's cost divided by delivered faxes, rounded up; None unless every attempt is priced."""
        if self.state != 'priced':
            return None
        return -(-self.cost_micros // self.delivered)

    @property
    def estimate(self):
        """True when any attempt's cost is Faxbot's estimate rather than a reported charge."""
        return self.estimated > 0

    @property
    def delivered_percent(self):
        return None if not self.attempts else (100 * self.delivered) // self.attempts

    @property
    def average_pages(self):
        return None if not self.delivered else round(self.delivered_pages / self.delivered, 1)

    @property
    def average_seconds(self):
        return None if not self.timed else round(self.connected_seconds / self.timed)

    def comparable(self, minimum=MIN_DELIVERED):
        """Priced throughout in one currency, with at least ``minimum`` delivered faxes."""
        return self.state == 'priced' and self.delivered >= minimum


def delivered_costs(attempts, cards=None):
    """``{route: DeliveredCost}`` for one destination's attempts; ``cards`` maps a provider to its current rate card."""
    cards = cards or {}
    totals = {}
    for attempt in attempts:
        if attempt.outcome not in OUTCOMES:
            continue
        entry = totals.setdefault(attempt.route, {
            'route': attempt.route, 'provider_id': attempt.provider_id, 'attempts': 0, 'delivered': 0, 'failed': 0,
            'uncertain': 0, 'cancelled': 0, 'costs': {}, 'reported': 0, 'estimated': 0, 'unpriced': 0,
            'delivered_pages': 0, 'connected_seconds': 0, 'timed': 0})
        card = cards.get(attempt.provider_id)
        entry['attempts'] += 1
        delivered = attempt.outcome == 'success'
        entry['delivered'] += delivered
        entry['failed'] += attempt.outcome == 'failed'
        entry['uncertain'] += attempt.outcome == 'uncertain'
        entry['cancelled'] += attempt.outcome == 'cancelled'
        if delivered:
            entry['delivered_pages'] += attempt.pages if isinstance(attempt.pages, int) and attempt.pages > 0 else 0
            if attempt.seconds is not None:
                entry['connected_seconds'] += int(attempt.seconds)
                entry['timed'] += 1
        figure = attempt_figure(attempt, card)
        if figure is None:
            entry['unpriced'] += 1
            continue
        micros, currency, basis = figure
        entry['costs'][currency] = entry['costs'].get(currency, 0) + micros
        entry[basis] += 1
    result = {}
    for route, entry in totals.items():
        costs = entry.pop('costs')
        card = cards.get(entry['provider_id'])
        currency = next(iter(costs)) if len(costs) == 1 else None
        if currency is None and not costs and card is not None:
            currency = card.currency
        result[route] = DeliveredCost(
            **entry, cost_micros=costs.get(currency, 0) if currency else 0, currency=currency,
            mixed=len(costs) > 1, plan=bool(card is not None and card.flat_plan), direct=route == 'direct', local=route == 'local')
    return result
