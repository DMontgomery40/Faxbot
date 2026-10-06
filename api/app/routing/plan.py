"""Assemble the routes one delivery may use and record the route it takes.

Candidates come only from the configuration revision the fax was accepted
under (its outbound provider and any listed extra routes) and from verified
direct peers. Nothing here reads current credentials for an accepted fax.
"""
from dataclasses import dataclass
import re

import sqlalchemy as sa

from .costs import plan_fee_text
from .database import read_connection
from .delivered import WINDOW_DAYS, short_money_text
from .delivered_store import DeliveredEvidence
from .policy import DIRECT, RouteCandidate, RouteChoice, RoutePolicy
from .store import destination_key
from ..provider_labels import PROVIDER_LABELS, trunk_name


MIN_ATTEMPTS = 3
LABELS = {'direct': 'Direct delivery', **PROVIDER_LABELS}
REASON_TEXT = {
    'direct_peer': 'Delivered straight to a verified partner, with no fax call.',
    'preferred': 'You chose this route for this number.',
    'cheapest': 'The cheapest route that works reliably for this number.',
    'known_cheapest': 'The cheapest route with a known price that works reliably for this number.',
    'reliable': 'More reliable for this number; its cost is unknown.',
    'alternative': 'Used if the routes above it are unavailable.',
    'unreliable': 'Recent faxes to this number often failed on this route.',
    'configured': 'Your outbound fax provider.',
    # Only the reason is stored with a sent fax, so its details use this sentence without the amount.
    'cheapest_delivered': f'The cheapest route per delivered fax to this number over the last {WINDOW_DAYS} days.',
}


def route_label(key):
    # The trunk is named after the carrier or phone system it connects to.
    if key == 'sip':
        return trunk_name()
    return LABELS.get(key, key)


def explain(choice):
    """One sentence saying what decided this route."""
    if choice.reason == 'included':
        card = choice.route.card
        return f'Included in your {route_label(choice.route.key)} plan ({plan_fee_text(card.monthly_fee_micros, card.currency)} a month).'
    if choice.reason == 'cheapest_delivered' and choice.delivered is not None and choice.compared:
        figure = choice.delivered
        about = 'about ' if figure.estimate else ''
        return (f'{route_label(choice.route.key)} cost {about}'
                f'{short_money_text(figure.per_delivered_micros, figure.currency)} per delivered fax to this number '
                f'over the last {WINDOW_DAYS} days, the cheapest of {choice.compared} routes.')
    if choice.reason == 'unknown_cost':
        return ('Your outbound fax provider; its cost is unknown.' if choice.route.bound
                else 'First in your list of routes; its cost is unknown.')
    return REASON_TEXT[choice.reason]


def extra_routes(values, bound):
    """Provider identities listed in ``FAX_OUTBOUND_ROUTES``, excluding the outbound provider."""
    return [identity for identity in values.outbound_route_providers
            if identity not in {bound, DIRECT} and re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', identity)]


def _trunk_numbers(values):
    """The numbers the carrier sends to this installation's trunk, as destination keys."""
    country = getattr(values, 'fax_default_country', 'US')
    return {destination_key(number, country) for number in getattr(values, 'sip_trunk_did_list', ())}


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
        if destination in _trunk_numbers(values):
            # One of the trunk's own numbers: an extra route over that trunk only calls itself back
            # (seen live on 2026-10-04 when a fallback faxed the Telnyx number over the Telnyx trunk).
            candidates = [candidate for candidate in candidates if candidate.bound or candidate.key != 'sip']
        row = self.store.get_destination(destination)
        policy = RoutePolicy(min_success_percent=values.route_min_success_percent, min_attempts=MIN_ATTEMPTS)
        # What each route really cost per delivered fax here; it decides only with enough evidence.
        providers = sum(candidate.kind == 'provider' for candidate in candidates)
        delivered = DeliveredEvidence(self.store).for_destination(destination) if providers > 1 else {}
        choices = policy.order(candidates, stats=self.store.route_stats(destination),
                               preferred=row['preferred_route'] if row else None, pages=pages, delivered=delivered)
        if not choices:
            fallback = RouteCandidate(bound, 'provider', bound, card_for(bound), bound=True)
            choices = [RouteChoice(fallback, 'configured', None)]
        return RoutePlan(destination, tuple(choices), peer if any(c.route.kind == 'direct' for c in choices) else None)
