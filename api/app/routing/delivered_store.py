"""Read each destination's attempts in the evidence window, for cost per delivered fax.

Evidence only: nothing here sends, repeats or changes a fax or any record. A fax
that rode in another fax's call (sent together) has no cost row outcome of its
own; the call is counted once, on the attempt that placed it, so it is left out
here as it is from every other cost figure.
"""
from datetime import timedelta

import sqlalchemy as sa

from .database import read_connection, reflect, utcnow
from .delivered import OUTCOMES, WINDOW_DAYS, Attempt, delivered_costs


class DeliveredEvidence:
    def __init__(self, store):
        """``store`` is a ``RouteStore``; its rate cards price attempts that have neither charge nor estimate."""
        self.store = store
        self._calls = None

    def _call_records(self):
        if self._calls is None:
            self._calls = reflect(self.store.engine, ('sip_call_records',))['sip_call_records']
        return self._calls

    def attempts(self, *, number=None, now=None, timing=True):
        """``{destination: [Attempt]}`` in the window; ``timing`` adds connected seconds measured on the trunk."""
        since = (now or utcnow()) - timedelta(days=WINDOW_DAYS)
        c, j = self.store.costs, self.store.jobs
        columns = [c.c.destination, c.c.route, c.c.provider_id, c.c.outcome, c.c.reported_cost_micros,
                   c.c.reported_currency, c.c.estimated_cost_micros, c.c.currency, c.c.started_at, c.c.ended_at,
                   j.c.pages]
        if timing:
            calls = self._call_records()
            columns.append(sa.select(sa.func.max(calls.c.connected_seconds))
                           .where(calls.c.attempt_id == c.c.id, calls.c.direction == 'outbound')
                           .correlate(c).scalar_subquery().label('seconds'))
        query = (sa.select(*columns).select_from(c.outerjoin(j, j.c.id == c.c.job_id))
                 .where(c.c.created_at >= since, c.c.outcome.in_(OUTCOMES))
                 .order_by(c.c.created_at, c.c.id))
        if number is not None:
            query = query.where(c.c.destination == number)
        with read_connection(self.store.engine) as connection:
            rows = connection.execute(query).mappings().all()
        found = {}
        for row in rows:
            elapsed = None
            if row['started_at'] is not None and row['ended_at'] is not None:
                elapsed = max(0, int((row['ended_at'] - row['started_at']).total_seconds()))
            found.setdefault(row['destination'], []).append(Attempt(
                route=row['route'], provider_id=row['provider_id'], outcome=row['outcome'],
                reported_micros=row['reported_cost_micros'], reported_currency=row['reported_currency'],
                estimated_micros=row['estimated_cost_micros'], currency=row['currency'], pages=row['pages'],
                seconds=row.get('seconds') if timing else None, elapsed_seconds=elapsed))
        return found

    def by_destination(self, *, number=None, now=None, timing=True):
        """``{destination: {route: DeliveredCost}}`` over the last ``WINDOW_DAYS`` days."""
        found = self.attempts(number=number, now=now, timing=timing)
        providers = {attempt.provider_id for attempts in found.values() for attempt in attempts}
        cards = {provider: self.store.card_for(provider) for provider in sorted(providers)}
        return {destination: delivered_costs(attempts, cards) for destination, attempts in found.items()}

    def for_destination(self, number, *, now=None, timing=False):
        """``{route: DeliveredCost}`` for one destination; route choice needs no call timing."""
        return self.by_destination(number=number, now=now, timing=timing).get(number, {})
