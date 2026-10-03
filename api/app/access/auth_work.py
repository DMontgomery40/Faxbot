"""Bound authentication work without abandoning its running database/KDF thread.

One instance belongs to one application worker. This is secondary concurrency
protection; AuthenticationAdmission owns the shared cross-worker rate bound.
The submitted synchronous operation owns the entire reservation, snapshot, KDF
and completion sequence. It must sanitize domain/storage failures itself.
"""
import asyncio
from contextvars import copy_context
from threading import BoundedSemaphore

from .types import AccessError


class AuthenticationBusyError(AccessError):
    code = 'authentication_busy'


class AuthenticationWork:
    def __init__(self):
        self._permits = BoundedSemaphore(4)

    async def run(self, operation):
        if not self._permits.acquire(blocking=False):
            raise AuthenticationBusyError()
        try:
            # Use the executor Future directly. A to_thread child Task could
            # itself be cancelled by shutdown while its thread still runs.
            task = asyncio.get_running_loop().run_in_executor(None, copy_context().run, operation)
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Repeated disconnect/shutdown cancellation cannot release a
                # permit while its thread still owns costly or committing work.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    task.exception()
                raise
        finally:
            self._permits.release()
