"""Read-only dashboard counts from durable outbound delivery state."""
from datetime import timedelta

import sqlalchemy as sa


def dashboard_counts(connection, deliveries, *, now, job_ids=None, jobs=None):
    """Read one consistent aggregate; legacy FaxJob status never authorizes a queue.

    With ``jobs`` (the fax_jobs table), a fax a person settled as can't tell (``outbound_store.CANT_TELL``) is counted
    on its own (``recent_cant_tell``), never among the recent failures: it may have arrived.
    """
    state = deliveries.c.state
    recent = sa.and_(deliveries.c.updated_at >= now - timedelta(hours=24), deliveries.c.updated_at <= now)
    cant_tell = sa.false()
    if jobs is not None:
        from .outbound_store import CANT_TELL
        cant_tell = sa.and_(state == 'failed', sa.select(jobs.c.error).where(
            jobs.c.id == deliveries.c.id).scalar_subquery() == CANT_TELL)
    conditions = {
        'queued': state.in_(['ready', 'preparing']),
        'in_progress': state.in_(['submitting', 'in_progress']),
        'recent_failures': sa.and_(state == 'failed', recent, sa.not_(cant_tell)),
        'held': state == 'held',
        'reconciliation_required': state == 'reconciliation_required',
    }
    if jobs is not None:
        conditions['recent_cant_tell'] = sa.and_(cant_tell, recent)
    statement = sa.select(*(sa.func.coalesce(sa.func.sum(
        sa.case((condition, 1), else_=0)), 0).label(name)
        for name, condition in conditions.items())).select_from(deliveries)
    if job_ids is not None:
        statement = statement.where(deliveries.c.id.in_(job_ids))
    return {name: int(count) for name, count in connection.execute(statement).mappings().one().items()}
