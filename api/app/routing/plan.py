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
from .alternates import attempt_number
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
    'rule': 'Your sending rule chose this account first.',
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


def ledger_key(key):
    """The key the delivery ledger records a route under: a partner relay's ``relay:<id>`` as ``relay.<id>``."""
    from ..direct.relay import ledger_key as relay_ledger_key
    return relay_ledger_key(key)


def _accounts(values):
    """The revision's sending accounts by key (``accounts.sending_accounts``); empty when they cannot be read."""
    try:
        from ..accounts import all_accounts
        return {account.key: account for account in all_accounts(values) if account.sends}
    except Exception:
        return {}


def _card(card_for, key, provider, account):
    """The rate card for one account: its own card first, then its provider's (a second trunk by its own carrier)."""
    if key == provider:
        return card_for(key)
    card = card_for(key)
    if card is None and provider == 'sip' and account is not None:
        preset = (getattr(account, 'settings', None) or {}).get('preset')
        card = card_for(f'sip-{preset}') if preset else None
    return card if card is not None else card_for(provider)


def _in_order(candidates, prices=None, pages=1):
    """Choices in the given order (a rule's list): delivery inside Faxbot and direct delivery first, as always."""
    from .costs import estimate_cost
    local = [candidate for candidate in candidates if candidate.kind == 'local']
    direct = [candidate for candidate in candidates if candidate.kind == 'direct']
    calls = [candidate for candidate in candidates if candidate.kind not in ('local', 'direct')]
    choices = [RouteChoice(candidate, 'own_number', None) for candidate in local]
    choices += [RouteChoice(candidate, 'direct_peer', None) for candidate in direct]
    for index, candidate in enumerate(calls):
        price = (prices or {}).get(candidate.key)
        estimate = price.micros if price is not None else (
            estimate_cost(candidate.card, pages) if candidate.card is not None else None)
        choices.append(RouteChoice(candidate, 'rule' if index == 0 and not choices else 'alternative', estimate))
    return choices


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
    # Accounts the fax's rules allow that this attempt could not use, with why (``routing.envelope.SKIPS``).
    skipped: tuple = ()
    # Accounts kept in the administrator's order although faxes to this number often failed on them.
    unreliable: tuple = ()
    # The fax's routing decision (``routing.envelope.Pinned``), or None for a fax accepted before rules.
    pinned: object = None

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
             dial=None, tried=None, pinned=None, current=None, prices=None, job_id=None, now=None):
        """``by_call``: the sender asked for a real call, so an own number is not delivered inside Faxbot.

        ``bound`` is the fax's own account: the default sending account it was
        accepted with (its key is the provider id for a provider's first account).
        ``dial`` is the number choice kept with the fax at acceptance
        (``OutboundStore.dial_state``): each provider route that may call the
        approved alternate calls it, priced for its class (a toll-free call by
        the route's toll-free price, unknown when unpublished); the others call
        the destination. ``tried`` holds ``(route, number)`` pairs earlier
        attempts used (``tried``); a route is left out only for the number it
        already called.

        ``pinned`` is the fax's routing decision (``routing.envelope``): only the
        accounts its envelope allows are candidates, in the administrator's order
        for ``one`` and ``ordered`` (an unreliable account keeps its place and is
        noted), ranked by ``RoutePolicy`` for ``cheapest`` and ``automatic``.
        ``current`` is the configuration in force now: an account turned off, or
        at its daily spending limit, is skipped, as is one over the decision's
        cost cap with today's price. ``prices`` (``routing.pricing``) ranks by
        what one more fax adds on each route instead of the bare rate card.
        Without ``pinned`` the plan is exactly what it was before rules.
        """
        destination = destination_key(to_number, getattr(values, 'fax_default_country', 'US'))
        card_for = card_for or self.store.card_for
        alternate = (dial or {}).get('alternate')
        if (dial or {}).get('refused') or alternate == destination:
            alternate = None
        preset = getattr(values, 'sip_trunk_preset', '') or ''
        dialed = {}
        owners = _accounts(values)
        skipped = []

        def provider_of(key):
            account = owners.get(key)
            return account.provider if account is not None else key

        def provider(identity, is_bound=False):
            kind = provider_of(identity)
            # The same rule the attempt records its number by (``alternates.attempt_number``).
            number, _ = attempt_number(destination, alternate=alternate, route_reaches=bool(alternate) and
                                       dialing.reaches(kind, alternate, values, sip_preset=preset))
            dialed[identity] = number
            card = _card(card_for, identity, kind, owners.get(identity))
            doubt = 0
            if number != destination:
                card = dialing.class_card(card, kind, number, sip_preset=preset)
                terms = dialing.terms_for(kind, preset)
                # A route that publishes that it calls toll-free numbers goes before one that does not say, at
                # equal cost (Telnyx's free toll-free calls before a flat plan that is $0 a fax).
                doubt = int(dialing.is_toll_free(number) and (terms is None or terms.reaches != 'yes'))
            return RouteCandidate(identity, 'provider', kind, card, bound=is_bound, doubt=doubt)

        if pinned is None or pinned.automatic:
            keys = [bound] + (extra_routes(values, bound) if alternates else [])
            if pinned is not None:
                keys = [key for key in keys if pinned.allows(key)]
        else:
            keys = [key for key in pinned.envelope.accounts if alternates or key == bound]
        candidates = []
        for key in keys:
            why = self._unusable(key, current, pinned, prices)
            if why is not None:
                skipped.append((key, why))
                continue
            candidates.append(provider(key, key == bound))
        if pinned is None or pinned.allows(DIRECT):
            peer = None
            if getattr(values, 'direct_delivery_enabled', False) and self.direct_ready():
                peer = self.store.verified_peer(destination, covered=True)
                if peer is not None:
                    candidates.insert(0, RouteCandidate(DIRECT, 'direct', DIRECT, None, peer_id=peer['id']))
        else:
            peer = None
        if (pinned is None or pinned.allows(local_delivery.LOCAL)) and self.local_ready() and \
                local_delivery.applies(values, destination, by_call=by_call):
            candidates.insert(0, RouteCandidate(local_delivery.LOCAL, 'local', local_delivery.LOCAL, None))
        relays, relay_prices = self._relays(destination, pages, pinned, job_id, now,
                                            home=getattr(values, 'fax_default_country', None))
        candidates += relays
        if relay_prices:
            # A relay is ranked by its partner's signed price like any account; an unknown price sorts last.
            prices = {**(prices or {}), **relay_prices}
        candidates = [candidate for candidate in candidates if candidate.key not in set(exclude)]
        if tried:
            done = {(route, number or destination) for route, number in tried}
            kept = []
            for candidate in candidates:
                if (ledger_key(candidate.key), dialed.get(candidate.key, destination)) in done:
                    if candidate.kind == 'provider':
                        skipped.append((candidate.key, 'tried'))
                    continue
                kept.append(candidate)
            candidates = kept
        if destination in _trunk_numbers(values):
            # One of the trunk's own numbers: an extra route over that trunk only calls itself back
            # (seen live on 2026-10-04 when a fallback faxed the Telnyx number over the Telnyx trunk).
            candidates = [candidate for candidate in candidates if candidate.bound or candidate.key != 'sip']
        row = self.store.get_destination(destination)
        policy = RoutePolicy(min_success_percent=values.route_min_success_percent, min_attempts=MIN_ATTEMPTS)
        stats = self.store.route_stats(destination)
        unreliable = ()
        if pinned is not None and pinned.envelope.mode in ('one', 'ordered'):
            # The administrator's order wins; an account that often failed here keeps its place and is noted.
            choices = _in_order(candidates, prices, pages)
            unreliable = tuple(candidate.key for candidate in candidates
                               if candidate.kind == 'provider' and policy.unreliable(stats.get(candidate.key)))
        else:
            # What each route really cost per delivered fax here; it decides only with enough evidence.
            providers = sum(candidate.kind == 'provider' for candidate in candidates)
            # Costs observed calling the destination say nothing about calling its approved alternate.
            calls_alternate = any(dialed.get(candidate.key, destination) != destination for candidate in candidates)
            delivered = (DeliveredEvidence(self.store).for_destination(destination)
                         if providers > 1 and not calls_alternate else {})
            if pinned is None:
                preferred = row['preferred_route'] if row else None
            else:
                # Pinned when the fax was accepted; a rule that chose "cheapest" ranks only its own accounts.
                preferred = pinned.envelope.preferred if pinned.automatic else None
            choices = policy.order(candidates, stats=stats, preferred=preferred, pages=pages, delivered=delivered,
                                   prices=prices)
        held_back = {key for key, why in skipped if why != 'tried'}
        if not choices and (pinned is None or (pinned.allows(bound) and bound not in held_back)):
            choices = [RouteChoice(provider(bound, True), 'configured', None)]
        return RoutePlan(destination, tuple(choices), peer if any(c.route.kind == 'direct' for c in choices) else None,
                         {key: number for key, number in dialed.items() if number != destination},
                         skipped=tuple(skipped), unreliable=unreliable, pinned=pinned)

    def _unusable(self, key, current, pinned, prices):
        """Why an allowed account cannot take this attempt now, or None: turned off, at its daily spending limit,
        or over the decision's cost cap with today's price (an unknown price fails a cap)."""
        if current is not None:
            from ..accounts import account_named, over_daily_limit
            account = account_named(current, key)
            if account is not None and not account.enabled:
                return 'turned_off'
            if account is not None and account.daily_spend_micros is not None:
                try:
                    if over_daily_limit(self.store.engine, current, account):
                        return 'spending_limit'
                except Exception:
                    pass
        if pinned is not None and pinned.envelope.caps:
            price = (prices or {}).get(key)
            micros, currency = (price.micros, price.currency) if price is not None else (None, None)
            for cap in pinned.envelope.caps:
                if micros is None or currency != cap.currency:
                    return 'unknown_cost'
                if micros > cap.micros:
                    return 'over_cap'
        return None

    def _relays(self, destination, pages, pinned, job_id, now, *, home=None):
        """Partner relays (``direct.relay.relay_candidates``) the decision allows, with their signed prices.

        A fax this installation is itself relaying for a partner (``job_id``) is never relayed again, and a rule's
        "never relay" (or ``never: [relay:<partner>]``) removes them. Direct-only and encrypted-only faxes never
        go through a relay: its call is an ordinary call at the partner, who sees the pages.
        """
        from ..direct.relay import relay_candidates
        from .pricing import Price
        from .predict import Shape
        if pinned is not None and (pinned.envelope.require_direct or pinned.envelope.require_encryption):
            return [], {}
        never = ()
        if pinned is not None:
            never = tuple(item.account for item in pinned.decision.excluded
                          if item.why == 'never' and (item.account == 'relay' or item.account.startswith('relay:')))
        try:
            shape = Shape(max(int(pages or 1), 1), None, 'standard', 'normal')
            found = relay_candidates(destination, shape, now, engine=self.store.engine, job_id=job_id, never=never,
                                     home=home)
        except Exception:
            return [], {}  # relay records unreadable: the fax goes by its own routes
        candidates, prices = [], {}
        for item in found:
            if pinned is not None and not pinned.allows(item.key):
                continue
            candidates.append(RouteCandidate(item.key, 'relay', 'relay', None, peer_id=item.peer_id))
            cost = item.cost
            prices[item.key] = Price(item.key, cost.micros if cost is not None else None,
                                     cost.currency if cost is not None else None, sentence=item.sentence)
        return candidates, prices
