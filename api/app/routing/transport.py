"""Choose each attempt's route, then hand it to the transport that owns that route.

The delivery worker's lease, durable submission marker and ambiguity handling
are unchanged: this wrapper only decides which prepared operation the worker
submits once. An extra provider route is bound to the attempt before the
submission marker, so results are accepted only from the account that sent it.
A direct delivery that is refused before any document bytes leave falls back to
the conventional route inside the same submission; an outcome that may have
reached the partner raises, so the worker records the attempt as uncertain and
the partner is asked instead of sending again.

A fax accepted under sending rules goes only by a route its envelope allows
(``routing.envelope``). Every drop-back to the fax's own account (the default
sending account it was accepted with) checks the envelope first; when nothing
the envelope allows can take the fax, it is held in Sent with one sentence
(``OutboundStore.hold_no_route``) and nothing is sent.
"""
from contextlib import AsyncExitStack, asynccontextmanager
import logging

from ..config_runtime import run_lifecycle_step
from ..outbound_worker import CapacityWait, PreparationFailure
from dataclasses import replace

from .alternates import attempt_number, claim_dial_state
from .dialing import reaches
from .policy import RouteCandidate
from .plan import RoutePlan, RoutePlanner, ledger_key
from .routes import RouteUnavailable, ensure_route_artifact, route_ready
from .store import RouteStore
from . import envelope as envelopes, holds as hold_store


class RouteHeld(CapacityWait):
    """Nothing the fax's rules allow can take it now: the delivery store already gave the claim back and holds the
    fax in Sent. The worker's capacity handling then finds nothing left to give back."""

    def __init__(self):
        super().__init__(seconds=5)


def _pinned(plan):
    """The plan's routing decision (``routing.envelope.Pinned``), or None for a fax without one."""
    return getattr(plan, 'pinned', None)


def _skipped(plan):
    return tuple(getattr(plan, 'skipped', ()) or ())


def _account_route_configuration(revision, key):
    """The ProviderConfiguration for sending by account ``key`` (``accounts.route_configuration``)."""
    from ..accounts import route_configuration
    return route_configuration(revision, key)


class DirectRefused(RuntimeError):
    """The partner definitely did not receive the document; nothing was accepted."""


class _RoutedOperation:
    def __init__(self, transport, claim, plan, direct, conventional):
        self.transport, self.claim, self.plan = transport, claim, plan
        self.direct, self.conventional = direct, conventional

    async def submit(self):
        if self.direct is not None:
            try:
                return await self.direct.submit()
            except DirectRefused:
                if self.conventional is None:
                    raise
                # Nothing reached the partner, so the fax route is a first send.
                await run_lifecycle_step(lambda: self.transport.record_fallback(self.claim, self.plan))
        return await self.conventional.submit()


_AUTOMATIC = object()


def _installation_direct_route(inner):
    """The installation's direct route, built from the captured transport's runtime."""
    runtime = getattr(inner, 'runtime', None)
    store = getattr(getattr(runtime, 'manager', None), 'store', None)
    if store is None:
        return None
    try:
        from ..direct.service import DirectRoute, DirectService
        return DirectRoute(DirectService(store.engine, values=lambda: store.read().active.values,
                                         environment=getattr(runtime, 'environment', {})))
    except Exception:
        logging.getLogger(__name__).warning('Direct delivery is unavailable; faxes use their providers.')
        return None


def _installation_relay_route(inner):
    """The installation's partner relay route (``direct/relay_route.py``), built like the direct route."""
    runtime = getattr(inner, 'runtime', None)
    store = getattr(getattr(runtime, 'manager', None), 'store', None)
    if store is None:
        return None
    try:
        from ..direct.relay_route import RelayRoute
        from ..direct.service import DirectService
        from ..outbound_store import OutboundStore
        return RelayRoute(DirectService(store.engine, values=lambda: store.read().active.values,
                                        environment=getattr(runtime, 'environment', {})),
                          delivery=lambda: OutboundStore(store))
    except Exception:
        logging.getLogger(__name__).warning('Partner relays are unavailable; faxes use their providers.')
        return None


class RoutedTransport:
    def __init__(self, inner, *, direct=_AUTOMATIC, route_store=None, local=None, relay=_AUTOMATIC):
        """``local`` delivers faxes to the installation's own numbers inside Faxbot (``routing.local``);
        ``relay`` sends a fax through a partner's relay when the plan chose ``relay:<partner>``."""
        self.inner = inner
        self.store = inner.store
        self.direct = _installation_direct_route(inner) if direct is _AUTOMATIC else direct
        self.relay = _installation_relay_route(inner) if relay is _AUTOMATIC else relay
        self.local = local
        self._route_store = route_store

    def routes(self):
        if self._route_store is None:
            self._route_store = RouteStore(self.store.configuration.engine)
        return self._route_store

    def _pinned(self, claim):
        """The fax's routing decision, or None for a fax accepted before rules. Raises UnreadableDecision."""
        return envelopes.load(self.store.configuration.engine, claim.job_id)

    def _bound_key(self, revision, profile):
        try:
            from ..accounts import default_sending_key
            return default_sending_key(revision.values) or profile.configuration.provider_id
        except Exception:
            return profile.configuration.provider_id

    def _current(self):
        """The configuration in force now (an account turned off stops new attempts at once), or None."""
        try:
            return self.store.configuration.read().active.values
        except Exception:
            return None

    def _plan(self, claim):
        revision, profile, job = self.store.load_dispatch(claim)
        routes = self.routes()
        planner = RoutePlanner(routes, direct_ready=self.direct.ready if self.direct is not None else None,
                               local_ready=self.local.ready if self.local is not None else None)
        pinned = self._pinned(claim)
        bound = self._bound_key(revision, profile) if pinned is not None else profile.configuration.provider_id
        # The number choice kept at acceptance; a route is left out only for the number it already called.
        dial = claim_dial_state(self.store, claim, job.get('dial'))
        prices = None
        if pinned is not None:
            from .pricing import prices_for
            prices = prices_for(routes, revision.values, job['to_number'], job.get('pages'), pinned=pinned,
                                bound=bound, dial=dial)
        plan = planner.plan(to_number=job['to_number'], bound=bound, values=revision.values,
                            pages=job.get('pages'), alternates=True, dial=dial,
                            tried=planner.tried(claim.job_id, claim.attempt_id),
                            by_call=bool(job.get('send_by_call')), pinned=pinned,
                            current=self._current() if pinned is not None else None, prices=prices,
                            job_id=claim.job_id)
        return plan, job, revision

    def _record_dialed(self, claim, plan, route, dial, values):
        """Record the number this attempt calls on ``route`` before its durable submission marker.

        The same rule the plan priced the route by (``alternates.attempt_number``); a route with no call records none.
        """
        record = getattr(self.store, 'record_dialed', None)
        if record is None or route.kind != 'provider':
            return
        alternate = (dial or {}).get('alternate')
        number, _ = attempt_number(plan.destination, alternate=alternate, refused=bool((dial or {}).get('refused')),
                                   route_reaches=bool(alternate) and reaches(route.provider_id, alternate, values))
        approvals = (dial or {}).get('approvals') if number != plan.destination else None
        record(claim, number, approvals or None)

    def _trunk_has_room(self, claim, revision):
        """Whether the trunk can take this call now (the claim gate covers faxes bound to it; this covers the rest)."""
        capacity = getattr(self.store, 'capacity', lambda: None)()
        if capacity is None:
            return True
        from datetime import datetime
        with self.store.configuration.engine.connect() as connection:
            room = capacity.room(connection, revision.values, datetime.utcnow(),
                                 exclude=[member.job_id for member in claim.everyone])
        return not (room.trunk_full or room.rate_full)

    def _assign(self, claim, plan, revision):
        """Bind the first usable provider route; the fax's own provider needs no change.

        A route over the trunk is used only while the trunk has room; otherwise the
        fax waits (``CapacityWait``) before any route is bound or anything is sent,
        unless its rule says to use the next allowed account when the trunk is
        busy (``when_busy: next``). What could not be used is kept on the plan's
        ``skipped`` list for the attempt's record.
        """
        pinned = _pinned(plan)
        skipped = list(_skipped(plan))
        for place, choice in enumerate(plan.choices):
            route = choice.route
            if route.kind in ('direct', 'local', 'relay'):
                return self._chosen(plan, choice, skipped), claim
            if route.provider_id == 'sip' and not self._trunk_has_room(claim, revision):
                if pinned is not None and pinned.envelope.when_busy == 'next':
                    skipped.append((route.key, 'busy'))
                    continue
                raise CapacityWait()
            if route.bound:
                return self._chosen(plan, choice, skipped), claim
            try:
                configuration = _account_route_configuration(revision, route.key)
                if not route_ready(configuration, ami=getattr(self.inner, 'ami', None)):
                    skipped.append((route.key, 'not_ready'))
                    continue
                # Prepare what this route needs (a fax TIFF for SIP) before binding it.
                ensure_route_artifact(revision, configuration, claim.job_id)
                chosen = self._chosen(plan, choice, skipped)
                return chosen, self.store.assign_route(claim, configuration, account_key=route.key, choice={
                    'place': self._place(plan, route.key), 'skipped': tuple(skipped),
                    'unreliable': getattr(plan, 'unreliable', ())})
            except RouteUnavailable:
                skipped.append((route.key, 'unavailable'))
                continue
        self._skipped = tuple(skipped)
        return None, claim

    def _chosen(self, plan, choice, skipped):
        self._skipped = tuple(skipped)
        return choice

    @staticmethod
    def _place(plan, key):
        accounts = _pinned(plan).envelope.accounts if _pinned(plan) is not None else ()
        return accounts.index(key) if key in accounts else 0

    def _record(self, claim, plan, choice):
        # The ledger's grammar has no colon: a partner relay is recorded as relay.<partner> (direct.relay.ledger_key).
        self.routes().record_decision(attempt_id=claim.attempt_id, job_id=claim.job_id,
                                      destination=plan.destination, route=ledger_key(choice.route.key),
                                      reason=choice.reason, provider_id=choice.route.provider_id)

    def _record_choice(self, claim, plan, choice):
        """The route this attempt was given, with what was skipped, before anything is sent (rules only).

        ``assign_route`` records an account it binds; this records the fax's own account, delivery inside Faxbot,
        direct delivery and a partner relay. Without the record, a fax whose rules exclude its own account is
        refused at the submission marker: nothing goes out unrecorded.
        """
        if _pinned(plan) is None:
            return
        key = choice.route.key
        self.store.record_route_choice(claim, account_key=key, place=self._place(plan, key),
                                       skipped=_skipped(plan), unreliable=getattr(plan, 'unreliable', ()))

    def _bound_allowed(self, claim):
        """Whether the fax may go by its own account: no rules, or its rules allow it. False when unreadable."""
        try:
            pinned = self._pinned(claim)
            if pinned is None:
                return True
            revision, profile = self.store.attempt_context(claim.job_id, claim.attempt_id)
            return pinned.allows(self._bound_key(revision, profile))
        except Exception:
            return False

    def _hold(self, claim, plan, reason=None):
        """Hold the fax in Sent: nothing its rules allow can take it now. Raises RouteHeld."""
        if reason is None:
            labels = {}
            if plan is not None and _pinned(plan) is not None:
                try:
                    from ..accounts import sending_accounts
                    revision, _ = self.store.configuration.outbound_context(claim.job_id)
                    labels = {account.key: account.label for account in sending_accounts(revision.values)}
                except Exception:
                    labels = {}
            reason = hold_store.no_route_sentence(lambda key: labels.get(key) or key,
                                                  _skipped(plan) if plan is not None else ())
        self.store.hold_no_route(claim, reason=reason, skipped=_skipped(plan) if plan is not None else ())
        raise RouteHeld()

    def _restore_bound(self, claim, plan, revision):
        """An extra route failed local preparation; use the fax's own provider instead, when its rules allow it."""
        if plan is not None and _pinned(plan) is not None and not _pinned(plan).allows(
                self._bound_key(revision, self.store.attempt_context(claim.job_id, claim.attempt_id)[1])):
            self._hold(claim, plan, 'The account your rules chose could not prepare this fax, and no other account '
                                    'your rules allow could take it. It waits for you in Sent; nothing was sent.')
        bound = next((choice for choice in plan.choices if choice.route.bound), None)
        _, accepted = self.store.configuration.outbound_context(claim.job_id)
        restored = self.store.assign_route(claim, accepted.configuration)
        if bound is not None:
            self.routes().reroute_decision(claim.attempt_id, route=bound.route.key,
                                           provider_id=bound.route.provider_id, reason='alternative')
        # The fax's own provider calls the number it may call, recorded again before submission.
        _, _, job = self.store.load_dispatch(restored)
        route = RouteCandidate(accepted.configuration.provider_id, 'provider', accepted.configuration.provider_id)
        self._record_dialed(restored, plan, route, claim_dial_state(self.store, restored, job.get('dial')),
                            revision.values)
        return restored

    def record_fallback(self, claim, plan):
        # Only reached with a conventional route prepared, which exists only when the plan kept the fax's own account.
        bound = next(choice.route for choice in plan.choices if choice.route.bound)
        self.routes().reroute_decision(claim.attempt_id, route=bound.key, provider_id=bound.provider_id,
                                       reason='alternative')

    @asynccontextmanager
    async def prepare(self, claim):
        plan = choice = None
        assigned = claim
        try:
            plan, job, revision = await run_lifecycle_step(lambda: self._plan(claim))
            self._skipped = _skipped(plan)
            choice, assigned = await run_lifecycle_step(lambda: self._assign(claim, plan, revision))
            # What this attempt could not use, for its record and for "send anyway" on a held fax.
            if isinstance(plan, RoutePlan):
                plan = replace(plan, skipped=self._skipped)
            if choice is not None:
                dial = claim_dial_state(self.store, claim, job.get('dial'))
                await run_lifecycle_step(lambda: self._record_dialed(claim, plan, choice.route, dial, revision.values))
        except CapacityWait:
            raise
        except Exception:
            # Route evidence is optional; the accepted provider still works (and chooses its own number), unless
            # the fax's rules exclude it: then the fax waits in Sent instead of going outside its rules.
            if not await run_lifecycle_step(lambda: self._bound_allowed(claim)):
                logging.getLogger(__name__).warning('Route choice is unavailable; the fax waits for a route its '
                                                    'rules allow.')
                await run_lifecycle_step(lambda: self._hold(claim, None, (
                    'Faxbot could not choose a route your rules allow just now, so nothing was sent. It waits for '
                    'you in Sent; check again in a moment.')))
            logging.getLogger(__name__).warning('Route choice is unavailable; using the outbound provider.')
            plan = choice = None
        if choice is None and plan is not None and _pinned(plan) is not None and \
                not await run_lifecycle_step(lambda: self._bound_allowed(claim)):
            await run_lifecycle_step(lambda: self._hold(claim, plan))
        if choice is not None:
            try:
                await run_lifecycle_step(lambda: self._record_choice(claim, plan, choice))
            except Exception:
                # Without the record a fax whose rules exclude its own account is refused at submission.
                logging.getLogger(__name__).warning('The route choice could not be recorded for a fax.')
            try:
                await run_lifecycle_step(lambda: self._record(claim, plan, choice))
            except Exception:
                logging.getLogger(__name__).warning('Route evidence could not be recorded for a fax.')
        if choice is not None and choice.route.kind == 'local' and self.local is not None:
            async with AsyncExitStack() as stack:
                try:
                    operation = await stack.enter_async_context(self.local.prepare(claim, plan, job))
                except Exception:
                    # Nothing was recorded as received; the fax goes by its normal route as a first send, when its
                    # rules allow its own account.
                    if not any(c.route.bound for c in plan.choices):
                        await run_lifecycle_step(lambda: self._hold(claim, plan, (
                            'This fax could not be delivered inside Faxbot, and your rules allow no call for it. It '
                            'waits for you in Sent; nothing was sent.')))
                    await run_lifecycle_step(lambda: self.record_fallback(claim, plan))
                    operation = await stack.enter_async_context(self.inner.prepare(claim))
                yield operation
            return
        if choice is not None and plan is not None and _pinned(plan) is not None and (
                (choice.route.kind == 'relay' and self.relay is None)
                or (choice.route.kind == 'direct' and self.direct is None)) \
                and not await run_lifecycle_step(lambda: self._bound_allowed(claim)):
            # The route the rules allow cannot be prepared here, and the fax's own account is not allowed.
            await run_lifecycle_step(lambda: self._hold(claim, plan, (
                'The route your rules chose for this fax is not available on this Faxbot, and no other account your '
                'rules allow could take it. It waits for you in Sent; nothing was sent.')))
        if choice is not None and choice.route.kind == 'relay' and self.relay is not None:
            # A partner relays it as a local call; a signed refusal (nothing accepted) lets the fax's own
            # route send it in the same attempt, recorded as a fallback.
            async with AsyncExitStack() as stack:
                conventional = None
                if any(c.route.bound for c in plan.choices):
                    try:
                        conventional = await stack.enter_async_context(self.inner.prepare(claim))
                    except PreparationFailure:
                        conventional = None
                try:
                    relayed = await stack.enter_async_context(self.relay.prepare(claim, plan, job, choice))
                except Exception:
                    if conventional is None and _pinned(plan) is not None:
                        # Nothing was sent: under sending rules the fax waits in Sent rather than failing.
                        await run_lifecycle_step(lambda: self._hold(claim, plan, (
                            'The partner relay your rules chose could not take this fax, and no other account '
                            'your rules allow could. It waits for you in Sent; nothing was sent.')))
                    if conventional is None:
                        raise PreparationFailure('provider_unavailable') from None
                    await run_lifecycle_step(lambda: self.record_fallback(claim, plan))
                    yield conventional
                    return
                yield _RoutedOperation(self, claim, plan, relayed, conventional)
            return
        if choice is None or choice.route.kind != 'direct' or self.direct is None:
            async with AsyncExitStack() as stack:
                try:
                    operation = await stack.enter_async_context(self.inner.prepare(assigned))
                except PreparationFailure:
                    if assigned is claim:
                        raise
                    assigned = await run_lifecycle_step(lambda: self._restore_bound(claim, plan, revision))
                    operation = await stack.enter_async_context(self.inner.prepare(assigned))
                yield operation
            return
        async with AsyncExitStack() as stack:
            conventional = None
            if any(c.route.bound for c in plan.choices):
                try:
                    conventional = await stack.enter_async_context(self.inner.prepare(claim))
                except PreparationFailure:
                    conventional = None  # Direct delivery can still proceed alone.
            try:
                direct = await stack.enter_async_context(self.direct.prepare(claim, plan, job))
            except Exception:
                # Nothing was sent; the conventional route is still a first send.
                if conventional is None and _pinned(plan) is not None:
                    await run_lifecycle_step(lambda: self._hold(claim, plan, (
                        'Direct delivery to the partner could not start, and your rules allow no call for this fax. '
                        'It waits for you in Sent; nothing was sent.')))
                if conventional is None:
                    raise PreparationFailure('provider_unavailable') from None
                await run_lifecycle_step(lambda: self.record_fallback(claim, plan))
                yield conventional
                return
            yield _RoutedOperation(self, claim, plan, direct, conventional)
