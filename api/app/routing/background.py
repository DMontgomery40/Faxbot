"""Fail-safe periodic work owned by a router lifespan.

A failing step logs one plain warning with its traceback and waits; it never
stops the API, never logs SQL parameters, and joins its database thread before
shutdown completes.
"""
import asyncio
from contextlib import asynccontextmanager
import logging

from ..config_runtime import run_lifecycle_step
from ..log_safety import loggable


def _warn(warning, error):
    """The step's plain warning with its traceback (``exc_info``), SQL parameters and database DETAIL lines left out."""
    logging.getLogger(__name__).warning(warning, exc_info=loggable(error))


async def repeat(step, *, interval, warning, initial_delay=None):
    """Run ``step`` (blocking) in a worker thread; a step returning True runs again at once."""
    await asyncio.sleep(interval if initial_delay is None else initial_delay)
    while True:
        try:
            busy = await run_lifecycle_step(step)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _warn(warning, error)
            busy = False
        if not busy:
            await asyncio.sleep(interval)


async def repeat_async(step, *, interval, warning, initial_delay=None):
    """Like ``repeat`` for a coroutine step that does its own thread hand-off."""
    await asyncio.sleep(interval if initial_delay is None else initial_delay)
    while True:
        try:
            busy = await step()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _warn(warning, error)
            busy = False
        if not busy:
            await asyncio.sleep(interval)


def installation_engine(app):
    runtime = getattr(app.state, 'configuration_runtime', None)
    manager = getattr(runtime, 'manager', None)
    store = getattr(manager, 'store', None)
    return getattr(store, 'engine', None), runtime


def lifespan_tasks(build):
    """Router lifespan running the coroutines ``build(app)`` returns; never fails startup."""
    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        try:
            for name, coroutine in build(app):
                tasks.append(asyncio.create_task(coroutine, name=name))
        except Exception:
            logging.getLogger(__name__).warning('Background delivery work could not start; the API is still available.')
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return lifespan
