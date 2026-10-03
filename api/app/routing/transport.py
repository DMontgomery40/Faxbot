"""Choose each attempt's route, then hand it to the transport that owns that route.

The delivery worker's lease, durable submission marker and ambiguity handling
are unchanged: this wrapper only decides which prepared operation the worker
submits once. A direct delivery that is refused before any document bytes
leave falls back to the conventional route inside the same submission; an
outcome that may have reached the partner raises, so the worker records the
attempt as uncertain and the partner is asked instead of sending again.
"""
from contextlib import AsyncExitStack, asynccontextmanager
import logging

from ..config_runtime import run_lifecycle_step
from ..outbound_worker import PreparationFailure
from .plan import RoutePlanner
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


class RoutedTransport:
    def __init__(self, inner, *, direct=None, route_store=None):
        self.inner = inner
        self.store = inner.store
        self.direct = direct
        self._route_store = route_store

    def routes(self):
        if self._route_store is None:
            self._route_store = RouteStore(self.store.configuration.engine)
        return self._route_store

    def _plan(self, claim):
        revision, profile, job = self.store.load_dispatch(claim)
        routes = self.routes()
        planner = RoutePlanner(routes, direct_ready=self.direct.ready if self.direct is not None else None)
        bound = profile.configuration.provider_id
        exclude = planner.tried_routes(claim.job_id, claim.attempt_id)
        plan = planner.plan(to_number=job['to_number'], bound=bound, values=revision.values,
                            pages=job.get('pages'), exclude=exclude)
        choice = plan.first
        routes.record_decision(attempt_id=claim.attempt_id, job_id=claim.job_id, destination=plan.destination,
                               route=choice.route.key, reason=choice.reason, provider_id=choice.route.provider_id)
        return plan, job

    def record_fallback(self, claim, plan):
        bound = next(choice.route for choice in plan.choices if choice.route.bound)
        self.routes().reroute_decision(claim.attempt_id, route=bound.key, provider_id=bound.provider_id,
                                       reason='alternative')

    @asynccontextmanager
    async def prepare(self, claim):
        try:
            plan, job = await run_lifecycle_step(lambda: self._plan(claim))
        except Exception:
            # Route evidence is optional; the accepted provider still works.
            logging.getLogger(__name__).warning('Route choice is unavailable; using the outbound provider.')
            async with self.inner.prepare(claim) as operation:
                yield operation
            return
        if plan.first.route.kind != 'direct' or self.direct is None:
            async with self.inner.prepare(claim) as operation:
                yield operation
            return
        async with AsyncExitStack() as stack:
            conventional = None
            if any(choice.route.bound for choice in plan.choices):
                try:
                    conventional = await stack.enter_async_context(self.inner.prepare(claim))
                except PreparationFailure:
                    conventional = None  # Direct delivery can still proceed alone.
            direct = await stack.enter_async_context(self.direct.prepare(claim, plan, job))
            yield _RoutedOperation(self, claim, plan, direct, conventional)
