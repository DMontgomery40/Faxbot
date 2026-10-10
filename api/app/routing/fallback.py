"""Send a definitely failed fax again on its next route, at most twice per fax.

Only a final failure reported by the provider qualifies. Uncertain attempts are
never retried here: they wait for the provider, a partner, or an operator. Nor is
a call that broke after pages went (``partly_sent``: the built-in and SSL Fax
engines and FreeSWITCH with pages confirmed, Sinch, Documo, HumbleFax): it waits for a person,
who may send only its remaining pages (``routing/continuation.py``).

A fax accepted under sending rules moves only to the next route its envelope
allows (``routing.envelope``), and when a rule chose its route, only after a
call that ended before any fax data (the delivery store checks that on the
failed attempt). The next route keeps the envelope's dialed number and layout.
"""
from datetime import timedelta

import sqlalchemy as sa

from .database import read_connection, utcnow
from .plan import RoutePlanner, ledger_key
from .routes import RouteUnavailable, route_ready
from . import envelope as envelopes


MAX_FALLBACKS = 2


class FallbackScheduler:
    def __init__(self, delivery, routes, *, ami=None, window=timedelta(hours=1), batch=20):
        self.delivery, self.routes = delivery, routes
        self.ami, self.window, self.batch = ami, window, batch

    def _candidates(self, now):
        deliveries = self.delivery.deliveries
        attempts, costs = self.routes.attempts, self.routes.costs
        query = (sa.select(deliveries.c.id, attempts.c.id.label('attempt_id'), costs.c.route)
                 .select_from(deliveries.join(attempts, attempts.c.id == deliveries.c.attempt_id)
                              .join(costs, costs.c.id == attempts.c.id))
                 .where(deliveries.c.state == 'failed', deliveries.c.dispatch_mode == 'normal',
                        attempts.c.phase == 'failed', attempts.c.error_category.is_(None),
                        attempts.c.submitted_at.is_not(None), attempts.c.completed_at >= now - self.window)
                 .order_by(attempts.c.completed_at.desc()).limit(self.batch))
        with read_connection(self.routes.engine) as connection:
            return connection.execute(query).all()

    def _usable(self, choice, revision):
        route = choice.route
        if route.kind in ('direct', 'relay') or route.bound:
            return True
        try:
            from ..accounts import route_configuration
            return route_ready(route_configuration(revision, route.key), ami=self.ami)
        except RouteUnavailable:
            return False

    def next_route(self, job_id, attempt_id, failed_route):
        revision, bound = self.delivery.configuration.outbound_context(job_id)
        jobs = self.routes.jobs
        # A fax that asked for a real call keeps asking: the trunk may then call one of its own numbers (plan).
        by_call = jobs.c.send_by_call if 'send_by_call' in jobs.c else sa.literal(None)
        with read_connection(self.routes.engine) as connection:
            job = connection.execute(sa.select(jobs.c.to_number, jobs.c.pages, by_call.label('by_call')).where(
                jobs.c.id == job_id)).one()
        planner = RoutePlanner(self.routes)
        # Every submitted attempt, the failed one included, leaves out its route for the number it called. A
        # definite failure calling the approved alternate moves the fax to the number the sender entered.
        tried = planner.tried(job_id)
        dial = dict(self.delivery.dial_state(job_id))
        if dial['alternate'] and 'dialed_number' in self.routes.attempts.c:
            # Asked inside the failure's own transaction, the failed attempt does not read as failed yet.
            with read_connection(self.routes.engine) as connection:
                failed_number = connection.scalar(sa.select(self.routes.attempts.c.dialed_number).where(
                    self.routes.attempts.c.id == attempt_id))
            dial['refused'] = dial['refused'] or failed_number == dial['alternate']
        try:
            pinned = envelopes.load(self.routes.engine, job_id)
        except envelopes.UnreadableDecision:
            return None  # Without a readable decision there is no allowed next route.
        key, prices = bound.configuration.provider_id, None
        if pinned is not None:
            from ..accounts import default_sending_key
            from .pricing import prices_for
            key = default_sending_key(revision.values) or key
            prices = prices_for(self.routes, revision.values, job.to_number, job.pages, pinned=pinned, bound=key,
                                dial=dial, job_id=job_id)
        plan = planner.plan(to_number=job.to_number, bound=key, values=revision.values,
                            pages=job.pages, alternates=True, dial=dial, tried=tried, pinned=pinned, prices=prices,
                            job_id=job_id, by_call=bool(job.by_call))
        done = {(route, number or plan.destination) for route, number in tried}
        return next((choice for choice in plan.choices
                     if (ledger_key(choice.route.key), plan.number_for(choice.route.key)) not in done
                     and self._usable(choice, revision)), None)

    def step(self):
        moved = 0
        for job_id, attempt_id, failed_route in self._candidates(utcnow()):
            if self.delivery.fallback_count(job_id) >= MAX_FALLBACKS:
                continue
            if self.next_route(job_id, attempt_id, failed_route) is None:
                continue
            if self.delivery.requeue_after_failure(job_id, attempt_id=attempt_id, category='provider_failed',
                                                   max_fallbacks=MAX_FALLBACKS):
                moved += 1
        return moved > 0


class FallbackPolicy:
    """Installed on the delivery store: True when a failed routed attempt has another usable route."""

    def __init__(self, scheduler):
        self.scheduler = scheduler

    def __call__(self, job_id, attempt_id):
        decision = self.scheduler.routes.decision(attempt_id)
        if decision is None:
            return False  # Not routed by Faxbot; the failure stands.
        return self.scheduler.next_route(job_id, attempt_id, decision['route']) is not None
