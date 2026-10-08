"""Choose each attempt's route, then hand it to the transport that owns that route.

The delivery worker's lease, durable submission marker and ambiguity handling
are unchanged: this wrapper only decides which prepared operation the worker
submits once. An extra provider route is bound to the attempt before the
submission marker, so results are accepted only from the account that sent it.
A direct delivery that is refused before any document bytes leave falls back to
the conventional route inside the same submission; an outcome that may have
reached the partner raises, so the worker records the attempt as uncertain and
the partner is asked instead of sending again.
"""
from contextlib import AsyncExitStack, asynccontextmanager
import logging

from ..config_runtime import run_lifecycle_step
from ..outbound_worker import CapacityWait, PreparationFailure
from .alternates import attempt_number, claim_dial_state
from .dialing import reaches
from .policy import RouteCandidate
from .plan import RoutePlanner
from .routes import RouteUnavailable, ensure_route_artifact, route_configuration, route_ready
from .store import RouteStore


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

    def _plan(self, claim):
        revision, profile, job = self.store.load_dispatch(claim)
        routes = self.routes()
        planner = RoutePlanner(routes, direct_ready=self.direct.ready if self.direct is not None else None,
                               local_ready=self.local.ready if self.local is not None else None)
        bound = profile.configuration.provider_id
        # The number choice kept at acceptance; a route is left out only for the number it already called.
        dial = claim_dial_state(self.store, claim, job.get('dial'))
        plan = planner.plan(to_number=job['to_number'], bound=bound, values=revision.values,
                            pages=job.get('pages'), alternates=True, dial=dial,
                            tried=planner.tried(claim.job_id, claim.attempt_id),
                            by_call=bool(job.get('send_by_call')))
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
        fax waits (``CapacityWait``) before any route is bound or anything is sent.
        """
        for choice in plan.choices:
            route = choice.route
            if route.kind in ('direct', 'local', 'relay'):
                return choice, claim
            if route.provider_id == 'sip' and not self._trunk_has_room(claim, revision):
                raise CapacityWait()
            if route.bound:
                return choice, claim
            try:
                configuration = route_configuration(revision, route.provider_id)
                if not route_ready(configuration, ami=getattr(self.inner, 'ami', None)):
                    continue
                # Prepare what this route needs (a fax TIFF for SIP) before binding it.
                ensure_route_artifact(revision, configuration, claim.job_id)
                return choice, self.store.assign_route(claim, configuration)
            except RouteUnavailable:
                continue
        return None, claim

    def _record(self, claim, plan, choice):
        route = choice.route.key
        if choice.route.kind == 'relay':
            from ..direct.relay import ledger_key
            route = ledger_key(route)  # The ledger's grammar has no colon: relay.<partner>.
        self.routes().record_decision(attempt_id=claim.attempt_id, job_id=claim.job_id,
                                      destination=plan.destination, route=route,
                                      reason=choice.reason, provider_id=choice.route.provider_id)

    def _restore_bound(self, claim, plan, revision):
        """An extra route failed local preparation; use the fax's own provider instead."""
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
        bound = next(choice.route for choice in plan.choices if choice.route.bound)
        self.routes().reroute_decision(claim.attempt_id, route=bound.key, provider_id=bound.provider_id,
                                       reason='alternative')

    @asynccontextmanager
    async def prepare(self, claim):
        plan = choice = None
        assigned = claim
        try:
            plan, job, revision = await run_lifecycle_step(lambda: self._plan(claim))
            choice, assigned = await run_lifecycle_step(lambda: self._assign(claim, plan, revision))
            if choice is not None:
                dial = claim_dial_state(self.store, claim, job.get('dial'))
                await run_lifecycle_step(lambda: self._record_dialed(claim, plan, choice.route, dial, revision.values))
        except CapacityWait:
            raise
        except Exception:
            # Route evidence is optional; the accepted provider still works (and chooses its own number).
            logging.getLogger(__name__).warning('Route choice is unavailable; using the outbound provider.')
            plan = choice = None
        if choice is not None:
            try:
                await run_lifecycle_step(lambda: self._record(claim, plan, choice))
            except Exception:
                logging.getLogger(__name__).warning('Route evidence could not be recorded for a fax.')
        if choice is not None and choice.route.kind == 'local' and self.local is not None:
            async with AsyncExitStack() as stack:
                try:
                    operation = await stack.enter_async_context(self.local.prepare(claim, plan, job))
                except Exception:
                    # Nothing was recorded as received; the fax goes by its normal route as a first send.
                    await run_lifecycle_step(lambda: self.record_fallback(claim, plan))
                    operation = await stack.enter_async_context(self.inner.prepare(claim))
                yield operation
            return
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
                if conventional is None:
                    raise PreparationFailure('provider_unavailable') from None
                await run_lifecycle_step(lambda: self.record_fallback(claim, plan))
                yield conventional
                return
            yield _RoutedOperation(self, claim, plan, direct, conventional)
