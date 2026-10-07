"""Assemble the routes one delivery may use and record the route it takes.

Candidates come only from the configuration revision the fax was accepted
under (its outbound provider and any listed extra routes) and from verified
direct peers. Nothing here reads current credentials for an accepted fax.
"""
from dataclasses import dataclass, field
import re

import sqlalchemy as sa

from .costs import plan_fee_text
from .database import read_connection
from .delivered import WINDOW_DAYS, short_money_text
from .delivered_store import DeliveredEvidence
from .policy import DIRECT, RouteCandidate, RouteChoice, RoutePolicy
from . import dialing, local as local_delivery
from .store import destination_key
from ..provider_labels import PROVIDER_LABELS, trunk_name


MIN_ATTEMPTS = 3
LABELS = {'local': 'This Faxbot', 'direct': 'Direct delivery', **PROVIDER_LABELS}
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
    'own_number': 'One of your own fax numbers: the fax goes straight into Received, with no phone call.',
}


# Why a sent fax went by its route, from the reason stored when Faxbot chose it; no amounts are stored.
DECIDED_TEXT = {
    'alternative': 'Your first-choice route was not available, so Faxbot used this one.',
    'unreliable': 'Faxes to this number often failed on this route, but no other route was available.',
    'unknown_cost': 'None of your routes had a price, so Faxbot used the first one in your list.',
    'own_number': 'This is one of your own fax numbers, so the fax went straight into Received without a phone call.',
}


def decided_text(route, reason):
    """One sentence for a sent fax's stored route reason, or None for a reason Faxbot does not know."""
    if reason == 'included':
        return f'Included in your {route_label(route)} plan.'
    return DECIDED_TEXT.get(reason) or REASON_TEXT.get(reason)


def route_label(key):
    # The trunk is named after the carrier or phone system it connects to.
    if key == 'sip':
        return trunk_name()
    return LABELS.get(key, key)


def explain(choice, destination=None, dialed=None):
    """One sentence saying what decided this route; it names the approved toll-free number the route calls."""
    sentence = _explain(choice, destination)
    if dialed and destination and dialed != destination and choice.route.kind == 'provider':
        from .dialing import is_toll_free
        kind = 'toll-free number' if is_toll_free(dialed) else 'other number'
        return sentence[:-1] + f', calling the {kind} the recipient approved.'
    return sentence


def _explain(choice, destination=None):
    if choice.reason == 'own_number' and destination:
        from .local import display_number
        return (f'{display_number(destination)} is one of your own fax numbers, so the fax goes straight into '
                'Received without a phone call.')
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
    # The number each provider route calls (route key -> E.164): the recipient's approved alternate where
    # that route may call it, else the destination. Empty when the fax has no approved alternate.
    dialed: dict = field(default_factory=dict)

    def number_for(self, key):
        return self.dialed.get(key, self.destination)

    @property
    def first(self):
        return self.choices[0]


class RoutePlanner:
    def __init__(self, store, *, direct_ready=None, local_ready=None):
        self.store = store
        self.direct_ready = direct_ready or (lambda: False)
        # Whether this process can deliver inside Faxbot (the worker has the received-fax records).
        self.local_ready = local_ready or (lambda: False)

    def tried_routes(self, job_id, current_attempt):
        """Routes earlier submitted attempts of this fax already used."""
        costs, attempts = self.store.costs, self.store.attempts
        with read_connection(self.store.engine) as connection:
            return set(connection.execute(sa.select(costs.c.route).join(attempts, attempts.c.id == costs.c.id).where(
                costs.c.job_id == job_id, costs.c.id != current_attempt,
                attempts.c.submitted_at.is_not(None))).scalars())

    def tried(self, job_id, current_attempt=None):
        """``(route, number)`` pairs earlier submitted attempts of this fax already used.

        The number is the one the attempt dialed, or None for the fax's own
        number (an attempt recorded before dialed numbers, or a route with no
        call). A route that failed calling the approved alternate may still call
        the number the sender entered.
        """
        costs, attempts = self.store.costs, self.store.attempts
        dialed = attempts.c.dialed_number if 'dialed_number' in attempts.c else sa.null()
        query = sa.select(costs.c.route, dialed).join(attempts, attempts.c.id == costs.c.id).where(
            costs.c.job_id == job_id, attempts.c.submitted_at.is_not(None))
        if current_attempt is not None:
            query = query.where(costs.c.id != current_attempt)
        with read_connection(self.store.engine) as connection:
            return {(route, number) for route, number in connection.execute(query).all()}

    def plan(self, *, to_number, bound, values, pages, alternates=False, exclude=(), card_for=None, by_call=False,
             dial=None, tried=None):
        """``by_call``: the sender asked for a real call, so an own number is not delivered inside Faxbot.

        ``dial`` is the number choice kept with the fax at acceptance
        (``OutboundStore.dial_state``): each provider route that may call the
        approved alternate calls it, priced for its class (a toll-free call by
        the route's toll-free price, unknown when unpublished); the others call
        the destination. ``tried`` holds ``(route, number)`` pairs earlier
        attempts used (``tried``); a route is left out only for the number it
        already called.
        """
        destination = destination_key(to_number, getattr(values, 'fax_default_country', 'US'))
        card_for = card_for or self.store.card_for
        alternate = (dial or {}).get('alternate')
        if (dial or {}).get('refused') or alternate == destination:
            alternate = None
        preset = getattr(values, 'sip_trunk_preset', '') or ''
        dialed = {}

        def provider(identity, is_bound=False):
            number = destination
            if alternate and dialing.reaches(identity, alternate, values, sip_preset=preset):
                number = alternate
            dialed[identity] = number
            card = card_for(identity)
            if number != destination:
                card = dialing.class_card(card, identity, number, sip_preset=preset)
            return RouteCandidate(identity, 'provider', identity, card, bound=is_bound)
        candidates = [provider(bound, True)]
        if alternates:
            candidates += [provider(identity) for identity in extra_routes(values, bound)]
        peer = None
        if getattr(values, 'direct_delivery_enabled', False) and self.direct_ready():
            peer = self.store.verified_peer(destination)
            if peer is not None:
                candidates.insert(0, RouteCandidate(DIRECT, 'direct', DIRECT, None, peer_id=peer['id']))
        if self.local_ready() and local_delivery.applies(values, destination, by_call=by_call):
            candidates.insert(0, RouteCandidate(local_delivery.LOCAL, 'local', local_delivery.LOCAL, None))
        candidates = [candidate for candidate in candidates if candidate.key not in set(exclude)]
        if tried:
            done = {(route, number or destination) for route, number in tried}
            candidates = [candidate for candidate in candidates
                          if (candidate.key, dialed.get(candidate.key, destination)) not in done]
        if destination in _trunk_numbers(values):
            # One of the trunk's own numbers: an extra route over that trunk only calls itself back
            # (seen live on 2026-10-04 when a fallback faxed the Telnyx number over the Telnyx trunk).
            candidates = [candidate for candidate in candidates if candidate.bound or candidate.key != 'sip']
        row = self.store.get_destination(destination)
        policy = RoutePolicy(min_success_percent=values.route_min_success_percent, min_attempts=MIN_ATTEMPTS)
        # What each route really cost per delivered fax here; it decides only with enough evidence.
        providers = sum(candidate.kind == 'provider' for candidate in candidates)
        # Costs observed calling the destination say nothing about calling its approved alternate.
        calls_alternate = any(dialed.get(candidate.key, destination) != destination for candidate in candidates)
        delivered = (DeliveredEvidence(self.store).for_destination(destination)
                     if providers > 1 and not calls_alternate else {})
        choices = policy.order(candidates, stats=self.store.route_stats(destination),
                               preferred=row['preferred_route'] if row else None, pages=pages, delivered=delivered)
        if not choices:
            choices = [RouteChoice(provider(bound, True), 'configured', None)]
        return RoutePlan(destination, tuple(choices), peer if any(c.route.kind == 'direct' for c in choices) else None,
                         {key: number for key, number in dialed.items() if number != destination})
