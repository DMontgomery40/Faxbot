"""Read-only dashboard counts from durable outbound delivery state."""
from datetime import timedelta

import sqlalchemy as sa


_ITEMS = {}


def certainty_items(connection):
    """This database's ``certainty_items`` table (0047), reflected once through ``connection``; None before it."""
    engine = connection.engine
    cached = _ITEMS.get(id(engine))
    if cached is not None and cached[0] is engine:
        return cached[1]
    try:
        table = sa.Table('certainty_items', sa.MetaData(), autoload_with=connection)
    except sa.exc.NoSuchTableError:
        table = None
    if len(_ITEMS) > 16:
        _ITEMS.clear()
    _ITEMS[id(engine)] = (engine, table)
    return table


def open_certainty_item(deliveries, items):
    """The delivery's latest certainty item while it is open, as a select correlated to ``deliveries`` (its
    ``category`` and ``created_at``)."""
    later = items.alias('later')
    return sa.select(items.c.category, items.c.created_at).where(
        items.c.job_id == deliveries.c.id, items.c.state == 'open',
        ~sa.select(later.c.id).where(later.c.job_id == items.c.job_id,
                                     later.c.created_at > items.c.created_at).exists())


def needs_settling(deliveries, items=None):
    """Faxes whose uncertain outcome waits for a person, exactly as Sent's settling list has them, as SQL: the fax's
    latest certainty item is open, whatever its delivery's state now; or its delivery is uncertain and the
    certainty worker has not filed an item yet. A fax whose latest item is settled ("can't tell" included) is done,
    though an older delivery may still be marked uncertain. Without the table (``items`` None), the delivery state."""
    state = deliveries.c.state
    if items is None:
        return state == 'reconciliation_required'
    filed = sa.select(items.c.id).where(items.c.job_id == deliveries.c.id).exists()
    return sa.or_(open_certainty_item(deliveries, items).exists(), sa.and_(state == 'reconciliation_required', ~filed))


def dashboard_counts(connection, deliveries, *, now, job_ids=None, jobs=None, items=None):
    """Read one consistent aggregate; legacy FaxJob status never authorizes a queue.

    With ``jobs`` (the fax_jobs table), a fax a person settled as can't tell (``outbound_store.CANT_TELL``) is counted
    on its own (``recent_cant_tell``), never among the recent failures: it may have arrived. With ``items`` (the
    certainty_items table, ``certainty_items``), ``reconciliation_required`` counts the faxes Sent's settling list
    waits on (``needs_settling``), as the outbound board does; without it, uncertain deliveries.
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
        'reconciliation_required': needs_settling(deliveries, items),
    }
    if jobs is not None:
        conditions['recent_cant_tell'] = sa.and_(cant_tell, recent)
    statement = sa.select(*(sa.func.coalesce(sa.func.sum(
        sa.case((condition, 1), else_=0)), 0).label(name)
        for name, condition in conditions.items())).select_from(deliveries)
    if job_ids is not None:
        statement = statement.where(deliveries.c.id.in_(job_ids))
    return {name: int(count) for name, count in connection.execute(statement).mappings().one().items()}
