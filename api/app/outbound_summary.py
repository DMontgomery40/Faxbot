"""Read-only dashboard counts from durable outbound delivery state."""
from datetime import timedelta

import sqlalchemy as sa


def dashboard_counts(connection, deliveries, *, now):
    """Read one consistent aggregate; legacy FaxJob status never authorizes a queue."""
    state = deliveries.c.state
    conditions = {
        'queued': state.in_(['ready', 'preparing']),
        'in_progress': state.in_(['submitting', 'in_progress']),
        'recent_failures': sa.and_(state == 'failed',
            deliveries.c.updated_at >= now - timedelta(hours=24),
            deliveries.c.updated_at <= now),
        'held': state == 'held',
        'reconciliation_required': state == 'reconciliation_required',
    }
    statement = sa.select(*(sa.func.coalesce(sa.func.sum(
        sa.case((condition, 1), else_=0)), 0).label(name)
        for name, condition in conditions.items())).select_from(deliveries)
    return {name: int(count) for name, count in connection.execute(statement).mappings().one().items()}
