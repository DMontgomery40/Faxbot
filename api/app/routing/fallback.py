"""Send a definitely failed fax again on its next route, at most twice per fax.

Only a final failure reported by the provider qualifies. Uncertain attempts are
never retried here: they wait for the provider, a partner, or an operator.
"""
from datetime import timedelta

import sqlalchemy as sa

from .database import read_connection, utcnow
from .plan import RoutePlanner
from .routes import RouteUnavailable, route_configuration, route_ready


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
        if route.kind == 'direct' or route.bound:
            return True
        try:
            return route_ready(route_configuration(revision, route.provider_id), ami=self.ami)
        except RouteUnavailable:
            return False

    def next_route(self, job_id, attempt_id, failed_route):
        revision, bound = self.delivery.configuration.outbound_context(job_id)
        jobs = self.routes.jobs
        with read_connection(self.routes.engine) as connection:
            job = connection.execute(sa.select(jobs.c.to_number, jobs.c.pages).where(jobs.c.id == job_id)).one()
        planner = RoutePlanner(self.routes)
        exclude = planner.tried_routes(job_id, attempt_id) | {failed_route}
        plan = planner.plan(to_number=job.to_number, bound=bound.configuration.provider_id, values=revision.values,
                            pages=job.pages, alternates=True, exclude=exclude)
        return next((choice for choice in plan.choices
                     if choice.route.key not in exclude and self._usable(choice, revision)), None)

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
