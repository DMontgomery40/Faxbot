"""Pure route ordering: a verified direct route, then the cheapest reliable provider.

The policy never contacts a provider and never decides whether a send may be
repeated; it only orders the routes a delivery may try for one destination.
The first route's reason says what decided it: a route whose cost is unknown
is never called the cheapest, and a flat monthly plan is "included".

Rate cards rank routes until there is evidence. A caller may pass ``prices``
instead (``routing.pricing``): what one more fax adds on each route, from the
shared predictor and each plan's budget. A route past its normal-use budget
then goes after every route within budget; then known cost before unknown,
cheaper first; between equal costs a route that uses no plan budget first. When at least two reliable
routes each have ``min_delivered`` delivered faxes to the destination in the
window, every attempt priced and one currency, those routes are ranked by what
they really cost per delivered fax (``delivered.py``), failed and repeated calls
included. They swap only among the places the rate cards gave them, so a route
without that evidence, such as a flat plan, keeps its place.
"""
from dataclasses import dataclass

from .costs import estimate_cost
from .delivered import MIN_DELIVERED


DIRECT = 'direct'
# ``configured``: the installation's outbound provider, recorded without a choice.
# ``known_cheapest``: cheapest among routes with a known price while another route's price is unknown.
# ``included``: a flat monthly plan with nothing charged per fax.
# ``reliable``: first because cheaper routes often failed here; its own cost is unknown.
# ``unknown_cost``: no route has a known price, so the configured order decides.
# ``cheapest_delivered``: the lowest observed cost per delivered fax among routes with enough delivered faxes.
# ``own_number``: one of the installation's own receiving numbers, delivered inside Faxbot without a call.
# ``rule``: first in the list a sending rule gave (its order, not cost, decides).
# ``plan_reserved``: first because a scarce plan's last pages are held for faxes they save more on (plan_allocation).
REASONS = ('direct_peer', 'preferred', 'cheapest', 'alternative', 'unreliable', 'configured', 'known_cheapest',
           'included', 'reliable', 'unknown_cost', 'cheapest_delivered', 'own_number', 'rule', 'plan_reserved')
# A partner relay (``direct.relay``) places its call at the partner; it is ranked like a provider.
CALLING = ('provider', 'relay')


@dataclass(frozen=True)
class RouteCandidate:
    """``key`` is ``local``, ``direct``, a provider account or a partner relay; one candidate per key."""
    key: str
    kind: str
    provider_id: str
    card: object = None
    bound: bool = False
    peer_id: str | None = None
    # Between routes of equal cost, the lower goes first: 1 for a route that may not reach the number it would
    # dial (its toll-free support is not published), 0 otherwise (``plan``).
    doubt: int = 0

    def __post_init__(self):
        if self.kind not in {'local', 'direct', 'provider', 'relay'}:
            raise ValueError('Unknown route kind.')


@dataclass(frozen=True)
class RouteStats:
    """Definite outcomes at one destination within the evidence window."""
    attempts: int = 0
    successes: int = 0

    @property
    def success_percent(self):
        return None if self.attempts == 0 else (100 * self.successes) // self.attempts


@dataclass(frozen=True)
class RouteChoice:
    route: RouteCandidate
    reason: str
    estimated_cost_micros: int | None
    # The route's observed cost per delivered fax (a ``DeliveredCost``), when it has one, and for
    # ``cheapest_delivered`` how many routes were compared.
    delivered: object = None
    compared: int = 0


class RoutePolicy:
    def __init__(self, *, min_success_percent=80, min_attempts=3, min_delivered=MIN_DELIVERED):
        if not 0 <= min_success_percent <= 100 or min_attempts < 1 or min_delivered < 1:
            raise ValueError('Invalid route reliability requirement.')
        self.min_success_percent = min_success_percent
        self.min_attempts = min_attempts
        self.min_delivered = min_delivered

    def unreliable(self, stats):
        """Too few definite outcomes is not evidence of unreliability."""
        if stats is None or stats.attempts < self.min_attempts:
            return False
        return stats.success_percent < self.min_success_percent

    def observed(self, candidates, delivered):
        """Keys of the candidates ranked by cost per delivered fax: none unless at least two qualify."""
        found = [candidate.key for candidate in candidates
                 if candidate.kind in CALLING and not getattr(candidate.card, 'flat_plan', False)
                 and delivered.get(candidate.key) is not None
                 and delivered[candidate.key].comparable(self.min_delivered)]
        if len(found) < 2 or len({delivered[key].currency for key in found}) != 1:
            return []
        return found

    def order(self, candidates, *, stats=None, preferred=None, pages=1, delivered=None, prices=None):
        """``delivered`` maps a route key to its ``DeliveredCost`` at this destination, if known.

        ``prices`` maps a route key to its ``routing.pricing.Price`` (what one more fax adds there); without it
        the rate card's estimate ranks, exactly as before.
        """
        stats = stats or {}
        delivered = delivered or {}
        prices = prices or {}
        keys = [candidate.key for candidate in candidates]
        if len(set(keys)) != len(keys):
            raise ValueError('Each route may appear once.')
        estimates = {candidate.key: (prices[candidate.key].micros if candidate.key in prices else
                                     estimate_cost(candidate.card, pages) if candidate.card is not None else None)
                     for candidate in candidates}
        position = {key: index for index, key in enumerate(keys)}
        chosen = []

        def take(candidate, reason, compared=0):
            chosen.append(RouteChoice(candidate, reason, estimates[candidate.key], delivered.get(candidate.key),
                                      compared))

        local = next((c for c in candidates if c.kind == 'local'), None)
        direct = next((c for c in candidates if c.kind == 'direct'), None)
        providers = [c for c in candidates if c.kind in CALLING]
        override = next((c for c in providers if preferred is not None and c.key == preferred), None)
        # An explicit provider preference is an operator override, ahead of every
        # other route; otherwise one of the installation's own numbers is delivered
        # inside Faxbot, then a verified direct route goes first.
        if override is not None:
            take(override, 'preferred')
        if local is not None:
            take(local, 'own_number' if override is None else 'alternative')
        if direct is not None:
            take(direct, 'direct_peer')
        remaining = [c for c in providers if c is not override]
        reliable = [c for c in remaining if not self.unreliable(stats.get(c.key))]
        doubtful = [c for c in remaining if self.unreliable(stats.get(c.key))]

        def cost_rank(candidate):
            estimate = estimates[candidate.key]
            price = prices.get(candidate.key)
            # A plan past its normal-use budget goes last; unknown cost sorts after known cost; between equal
            # costs a route that uses no plan budget first; ties keep configured order, which puts the job's own
            # provider first.
            over = bool(price is not None and price.over_budget)
            uses = bool(price is not None and price.uses_budget)
            return (over, estimate is None, estimate if estimate is not None else 0, uses, candidate.doubt,
                    not candidate.bound, position[candidate.key])

        def first_reason(candidate):
            estimate = estimates[candidate.key]
            price = prices.get(candidate.key)
            if price is not None and price.in_plan:
                return 'included'
            if estimate is None:
                return 'reliable' if doubtful else 'unknown_cost'
            if getattr(candidate.card, 'flat_plan', False):
                return 'included'
            if len(remaining) == 1:
                return 'configured' if candidate.bound else 'cheapest'
            if any(estimates[other.key] is None for other in reliable if other is not candidate):
                return 'known_cheapest'
            return 'cheapest'

        ranked = sorted(reliable, key=cost_rank)
        observed = self.observed(ranked, delivered)
        if observed:
            # Evidence reorders only the routes that have it, within the places they already hold.
            slots = [index for index, candidate in enumerate(ranked) if candidate.key in observed]
            cheapest = sorted((ranked[index] for index in slots), key=lambda candidate: (
                delivered[candidate.key].per_delivered_micros, not candidate.bound, position[candidate.key]))
            for index, candidate in zip(slots, cheapest):
                ranked[index] = candidate
        for index, candidate in enumerate(ranked):
            if index or override is not None:
                take(candidate, 'alternative')
            elif candidate.key in observed:
                take(candidate, 'cheapest_delivered', len(observed))
            else:
                take(candidate, first_reason(candidate))
        for candidate in sorted(doubtful, key=lambda c: (-(stats[c.key].success_percent or 0),) + cost_rank(c)):
            take(candidate, 'unreliable')
        return chosen
