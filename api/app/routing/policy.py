"""Pure route ordering: a verified direct route, then the cheapest reliable provider.

The policy never contacts a provider and never decides whether a send may be
repeated; it only orders the routes a delivery may try for one destination.
The first route's reason says what decided it: a route whose cost is unknown
is never called the cheapest, and a flat monthly plan is "included".
"""
from dataclasses import dataclass

from .costs import estimate_cost


DIRECT = 'direct'
# ``configured``: the installation's outbound provider, recorded without a choice.
# ``known_cheapest``: cheapest among routes with a known price while another route's price is unknown.
# ``included``: a flat monthly plan with nothing charged per fax.
# ``reliable``: first because cheaper routes often failed here; its own cost is unknown.
# ``unknown_cost``: no route has a known price, so the configured order decides.
REASONS = ('direct_peer', 'preferred', 'cheapest', 'alternative', 'unreliable', 'configured', 'known_cheapest',
           'included', 'reliable', 'unknown_cost')


@dataclass(frozen=True)
class RouteCandidate:
    """``key`` is ``direct`` or a provider identity; one candidate per key."""
    key: str
    kind: str
    provider_id: str
    card: object = None
    bound: bool = False
    peer_id: str | None = None

    def __post_init__(self):
        if self.kind not in {'direct', 'provider'}:
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


class RoutePolicy:
    def __init__(self, *, min_success_percent=80, min_attempts=3):
        if not 0 <= min_success_percent <= 100 or min_attempts < 1:
            raise ValueError('Invalid route reliability requirement.')
        self.min_success_percent = min_success_percent
        self.min_attempts = min_attempts

    def unreliable(self, stats):
        """Too few definite outcomes is not evidence of unreliability."""
        if stats is None or stats.attempts < self.min_attempts:
            return False
        return stats.success_percent < self.min_success_percent

    def order(self, candidates, *, stats=None, preferred=None, pages=1):
        stats = stats or {}
        keys = [candidate.key for candidate in candidates]
        if len(set(keys)) != len(keys):
            raise ValueError('Each route may appear once.')
        estimates = {candidate.key: (estimate_cost(candidate.card, pages) if candidate.card is not None else None)
                     for candidate in candidates}
        position = {key: index for index, key in enumerate(keys)}
        chosen = []

        def take(candidate, reason):
            chosen.append(RouteChoice(candidate, reason, estimates[candidate.key]))

        direct = next((c for c in candidates if c.kind == 'direct'), None)
        providers = [c for c in candidates if c.kind == 'provider']
        override = next((c for c in providers if preferred is not None and c.key == preferred), None)
        # An explicit provider preference is an operator override, ahead of
        # the direct route; otherwise a verified direct route always goes first.
        if override is not None:
            take(override, 'preferred')
        if direct is not None:
            take(direct, 'direct_peer')
        remaining = [c for c in providers if c is not override]
        reliable = [c for c in remaining if not self.unreliable(stats.get(c.key))]
        doubtful = [c for c in remaining if self.unreliable(stats.get(c.key))]

        def cost_rank(candidate):
            estimate = estimates[candidate.key]
            # Unknown cost sorts after known cost; ties keep configured order,
            # which puts the job's own provider first.
            return (estimate is None, estimate if estimate is not None else 0,
                    not candidate.bound, position[candidate.key])

        def first_reason(candidate):
            estimate = estimates[candidate.key]
            if estimate is None:
                return 'reliable' if doubtful else 'unknown_cost'
            if getattr(candidate.card, 'flat_plan', False):
                return 'included'
            if len(remaining) == 1:
                return 'configured' if candidate.bound else 'cheapest'
            if any(estimates[other.key] is None for other in reliable if other is not candidate):
                return 'known_cheapest'
            return 'cheapest'

        for index, candidate in enumerate(sorted(reliable, key=cost_rank)):
            take(candidate, first_reason(candidate) if index == 0 and override is None else 'alternative')
        for candidate in sorted(doubtful, key=lambda c: (-(stats[c.key].success_percent or 0),) + cost_rank(c)):
            take(candidate, 'unreliable')
        return chosen
