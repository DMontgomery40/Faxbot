"""Assemble the routes one delivery may use and record the route it takes.

Candidates come only from the configuration revision the fax was accepted
under (its outbound provider and any listed extra routes) and from verified
direct peers. Nothing here reads current credentials for an accepted fax.
"""
from dataclasses import dataclass
import re

import sqlalchemy as sa

from .database import read_connection
from .policy import DIRECT, RouteCandidate, RouteChoice, RoutePolicy
from .store import destination_key


MIN_ATTEMPTS = 3
LABELS = {
    'direct': 'Direct delivery', 'sip': 'Your SIP trunk (Asterisk)', 'freeswitch': 'Your SIP trunk (FreeSWITCH)',
    'phaxio': 'Phaxio', 'sinch': 'Sinch', 'documo': 'Documo', 'humblefax': 'HumbleFax', 'signalwire': 'SignalWire',
}
REASON_TEXT = {
    'direct_peer': 'Delivered straight to a verified partner, with no fax call.',
    'preferred': 'You chose this route for this number.',
    'cheapest': 'The cheapest route that works reliably for this number.',
    'known_cheapest': 'The cheapest route with a known price that works reliably for this number.',
    'reliable': 'More reliable for this number; its cost is unknown.',
    'alternative': 'Used if the routes above it are unavailable.',
    'unreliable': 'Recent faxes to this number often failed on this route.',
    'configured': 'Your outbound fax provider.',
}


def route_label(key):
    return LABELS.get(key, key)


def explain(choice):
    """One sentence saying what decided this route."""
    if choice.reason == 'included':
        return f'Included in your {route_label(choice.route.key)} plan.'
    if choice.reason == 'unknown_cost':
        return ('Your outbound fax provider; its cost is unknown.' if choice.route.bound
                else 'First in your list of routes; its cost is unknown.')
    return REASON_TEXT[choice.reason]


def extra_routes(values, bound):
    """Provider identities listed in ``FAX_OUTBOUND_ROUTES``, excluding the outbound provider."""
    return [identity for identity in values.outbound_route_providers
            if identity not in {bound, DIRECT} and re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', identity)]


@dataclass(frozen=True)
class RoutePlan:
    destination: str
    choices: tuple
    peer: dict | None

    @property
    def first(self):
        return self.choices[0]


class RoutePlanner:
    def __init__(self, store, *, direct_ready=None):
        self.store = store
        self.direct_ready = direct_ready or (lambda: False)

    def tried_routes(self, job_id, current_attempt):
        """Routes earlier submitted attempts of this fax already used."""
        costs, attempts = self.store.costs, self.store.attempts
        with read_connection(self.store.engine) as connection:
            return set(connection.execute(sa.select(costs.c.route).join(attempts, attempts.c.id == costs.c.id).where(
                costs.c.job_id == job_id, costs.c.id != current_attempt,
                attempts.c.submitted_at.is_not(None))).scalars())

    def plan(self, *, to_number, bound, values, pages, alternates=False, exclude=(), card_for=None):
        destination = destination_key(to_number, getattr(values, 'fax_default_country', 'US'))
        card_for = card_for or self.store.card_for
        candidates = [RouteCandidate(bound, 'provider', bound, card_for(bound), bound=True)]
        if alternates:
            candidates += [RouteCandidate(identity, 'provider', identity, card_for(identity))
                           for identity in extra_routes(values, bound)]
        peer = None
        if getattr(values, 'direct_delivery_enabled', False) and self.direct_ready():
            peer = self.store.verified_peer(destination)
            if peer is not None:
                candidates.insert(0, RouteCandidate(DIRECT, 'direct', DIRECT, None, peer_id=peer['id']))
        candidates = [candidate for candidate in candidates if candidate.key not in set(exclude)]
        row = self.store.get_destination(destination)
        policy = RoutePolicy(min_success_percent=values.route_min_success_percent, min_attempts=MIN_ATTEMPTS)
        choices = policy.order(candidates, stats=self.store.route_stats(destination),
                               preferred=row['preferred_route'] if row else None, pages=pages)
        if not choices:
            fallback = RouteCandidate(bound, 'provider', bound, card_for(bound), bound=True)
            choices = [RouteChoice(fallback, 'configured', None)]
        return RoutePlan(destination, tuple(choices), peer if any(c.route.kind == 'direct' for c in choices) else None)
