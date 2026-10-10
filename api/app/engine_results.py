"""The built-in fax engine's call results: saved off the event loop, in arrival order, and never lost silently.

Asterisk reports each call's end once (the FaxResult and OriginateResponse events) and never sends it again. Their
handlers write to the delivery store, which can wait for the database's write lock and then try again
(``database_busy.retry_when_busy``). That waiting happens here, on one thread of its own, so the API never stalls
behind it and two events about one call are always handled in the order Asterisk sent them.

A result the database still refused after its last try leaves the fax in progress. ``settle_lost_results`` is the
safety net: a built-in engine fax still in progress ``capacity.HOLD`` after it was handed to Asterisk had its result
lost, so it waits for a person (uncertain, ``result_lost``) and is never sent again by itself. Its line was already
free by then (``capacity.HOLD``). A result that arrives later still replaces that with the real outcome.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextvars
from datetime import datetime
import logging
import threading

import sqlalchemy as sa


THREAD_NAME = 'faxbot-engine-results'
_executor = None
_pending = set()
_guard = threading.Lock()


def submit(handler):
    """Run ``handler()`` on the results thread after every result received before it; returns its Future."""
    global _executor
    context = contextvars.copy_context()
    with _guard:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=THREAD_NAME)
        future = _executor.submit(context.run, handler)
        _pending.add(future)
    future.add_done_callback(_done)
    return future


def _done(future):
    with _guard:
        _pending.discard(future)
    if not future.cancelled() and future.exception() is not None:
        logging.getLogger(__name__).error('A fax engine result could not be handled.', exc_info=future.exception())


async def drain(timeout=60.0):
    """Wait, at most ``timeout`` seconds, for the results already received (when the API stops)."""
    with _guard:
        pending = list(_pending)
    if pending:
        await asyncio.wait([asyncio.wrap_future(future) for future in pending], timeout=timeout)


def settle_lost_results(store, *, now=None, wait=None):
    """Mark each built-in engine fax still in progress ``wait`` (``capacity.HOLD``) after it was handed to the
    engine as uncertain (``result_lost``); returns how many. Never sends anything."""
    from .capacity import HOLD
    from .config_store import ConfigurationStoreError
    from .database_busy import retry_when_busy
    from .outbound_store import DeliveryConflict
    now = now or datetime.utcnow()
    cutoff = now - (wait or HOLD)
    deliveries, attempts = store.deliveries, store.attempts
    lost = (sa.select(deliveries.c.id, deliveries.c.attempt_id).join(attempts, attempts.c.id == deliveries.c.attempt_id)
            .where(deliveries.c.state == 'in_progress', attempts.c.submitted_at.is_not(None),
                   attempts.c.submitted_at <= cutoff).order_by(attempts.c.submitted_at).limit(100))
    with store.configuration.engine.connect() as connection:
        rows = connection.execute(lost).all()
        took = _taken_by_hylafax(connection, [attempt_id for _, attempt_id in rows])
    marked = 0
    for job_id, attempt_id in rows:
        if attempt_id in took:
            continue  # the SSL Fax engine's own sends: hylafax_http._sweep waits for them (it queues and retries)
        try:
            _, profile = store.attempt_context(job_id, attempt_id)
        except (DeliveryConflict, ConfigurationStoreError, LookupError, ValueError):
            continue
        if profile.configuration.provider_id != 'sip' or profile.configuration.manifest is not None:
            continue  # a cloud provider's fax is read again by polling; nothing is lost
        if retry_when_busy(lambda: store.record_unconfirmed(
                job_id, attempt_id=attempt_id, profile_id=profile.id, event_key=f'{attempt_id}:builtin:no-result',
                category='result_lost', now=now), what=f'the lost result of fax {job_id}', idempotent=True):
            marked += 1
            logging.getLogger(__name__).warning(
                "Fax %s: Faxbot lost this call's result, so it now waits for a person to check whether it arrived; "
                'it is not sent again.', job_id)
    return marked


def _taken_by_hylafax(connection, attempt_ids):
    if not attempt_ids:
        return set()
    try:
        calls = sa.Table('fax_engine_calls', sa.MetaData(), autoload_with=connection)
    except sa.exc.NoSuchTableError:
        return set()
    return set(connection.execute(sa.select(calls.c.call_key).where(
        calls.c.direction == 'outbound', calls.c.engine == 'hylafax', calls.c.call_key.in_(attempt_ids))).scalars())
