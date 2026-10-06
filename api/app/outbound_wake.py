"""Idle back-off for the delivery loops, and the wake-up that keeps a new fax's start immediate.

The outbound worker and the status poller check for work every second while
there is work. When a check finds nothing, each waits longer before the next
one (1, 2, 4, 8, then 10 seconds), so an idle installation does not read the
database every second. A fax accepted for sending (or put back to send again)
wakes them at once, so a new fax never waits for the back-off; a wake-up that
arrives while a loop is busy is kept for its next wait, never lost.
"""
import asyncio
import threading


FIRST_SECONDS = 1.0
LONGEST_SECONDS = 10.0


class IdleBackoff:
    """1, 2, 4, 8, then 10 seconds between checks that find nothing; back to 1 after any work."""

    def __init__(self, first=FIRST_SECONDS, longest=LONGEST_SECONDS):
        if first <= 0 or longest < first:
            raise ValueError('Back-off intervals must be positive and growing.')
        self.first, self.longest = first, longest
        self.current = first

    def next(self):
        seconds = self.current
        self.current = min(self.longest, self.current * 2)
        return seconds

    def reset(self):
        self.current = self.first


class Wake:
    """A signal the delivery loops wait on; ``notify`` is safe from any thread, with or without a loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self._waiters = []  # (loop, event) for each loop waiting now
        self.generation = 0  # counts notifications, so one that arrives between waits is not lost

    def notify(self):
        with self._lock:
            self.generation += 1
            waiters = list(self._waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # that loop has closed

    async def wait(self, seconds, seen):
        """Wait up to ``seconds``; True at once if notified since ``seen`` (a ``generation``), or when notified."""
        event = asyncio.Event()
        entry = (asyncio.get_running_loop(), event)
        with self._lock:
            if self.generation != seen:
                return True
            self._waiters.append(entry)
        try:
            await asyncio.wait_for(event.wait(), seconds)
            return True
        except TimeoutError:
            return False
        finally:
            with self._lock:
                self._waiters.remove(entry)


wake = Wake()


async def idle_loop(step, *, backoff, warning, logger, signal=wake):
    """Run ``step`` forever: again at once after work, otherwise after the back-off or a wake-up."""
    while True:
        seen = signal.generation
        try:
            worked = await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            # No provider payload, recipient, URL, credentials or traceback.
            logger.warning(warning)
            worked = False
        if worked:
            backoff.reset()
            continue
        if await signal.wait(backoff.next(), seen):
            backoff.reset()
