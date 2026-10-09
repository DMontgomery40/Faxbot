"""Calls Faxbot placed over the carrier trunk, with what each cost: the evidence behind trunk advice.

Read-only. One ``TrunkCall`` per outbound call record, joined to the attempt's
cost row by the attempt identity. The fax marker (``mark calls as fax``) and
billing-step advice both read calls through here, so they price a call the same
way the rest of Faxbot does:

1. the carrier's settled charge (every charge for the call final, one currency);
2. otherwise the carrier's reported charge, which may still change;
3. otherwise Faxbot's estimate from the rate card;
4. otherwise unknown. Unknown is never counted as zero.

``marker`` is True or False only for calls whose record holds what was actually
sent: the submission event fills ``called`` together with the marker. A record
made some other way (an answer from the engine that arrived before the
submission was recorded) keeps the column's default, so its marker is None.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa

from .database import read_connection, reflect, utcnow


@dataclass(frozen=True)
class TrunkCall:
    attempt_id: str | None
    destination: str | None
    started_at: datetime
    connected_seconds: int | None
    pages: int | None
    t38: str  # 'yes', 'no' or 'unknown'
    marker: bool | None
    delivered: bool | None  # None while the result is unknown
    cost_micros: int | None
    currency: str | None
    basis: str | None  # 'settled', 'reported', 'estimated' or None when unknown


def _delivered(row):
    outcome = row['outcome']
    if outcome == 'success':
        return True
    if outcome in ('failed', 'cancelled'):
        return False
    if outcome is None and row['fax_status'] in ('SUCCESS', 'FAILED'):
        return row['fax_status'] == 'SUCCESS'
    return None


def _cost(row):
    if row['settled_cost_micros'] is not None and row['reported_currency']:
        return int(row['settled_cost_micros']), row['reported_currency'], 'settled'
    if row['reported_cost_micros'] is not None and row['reported_currency']:
        return int(row['reported_cost_micros']), row['reported_currency'], 'reported'
    if row['estimated_cost_micros'] is not None and row['currency']:
        return int(row['estimated_cost_micros']), row['currency'], 'estimated'
    return None, None, None


def trunk_calls(engine, *, days, now=None):
    """Outbound trunk calls started in the last ``days`` days, oldest first."""
    since = (now or utcnow()) - timedelta(days=days)
    tables = reflect(engine, ('sip_call_records', 'delivery_attempt_costs'))
    calls, costs = tables['sip_call_records'], tables['delivery_attempt_costs']
    query = (sa.select(calls.c.attempt_id, calls.c.called, calls.c.started_at, calls.c.connected_seconds,
                       calls.c.pages, calls.c.t38, calls.c.fax_preference, calls.c.fax_status,
                       costs.c.destination, costs.c.outcome, costs.c.estimated_cost_micros, costs.c.currency,
                       costs.c.reported_cost_micros, costs.c.reported_currency, costs.c.settled_cost_micros)
             .select_from(calls.outerjoin(costs, costs.c.id == calls.c.attempt_id))
             .where(calls.c.direction == 'outbound', calls.c.started_at >= since)
             .order_by(calls.c.started_at, calls.c.id))
    with read_connection(engine) as connection:
        rows = connection.execute(query).mappings().all()
    found = []
    for row in rows:
        micros, currency, basis = _cost(row)
        found.append(TrunkCall(
            attempt_id=row['attempt_id'], destination=row['destination'] or row['called'],
            started_at=row['started_at'], connected_seconds=row['connected_seconds'], pages=row['pages'],
            t38=row['t38'] or 'unknown',
            marker=None if row['called'] is None else bool(row['fax_preference']),
            delivered=_delivered(row), cost_micros=micros, currency=currency, basis=basis))
    return found
