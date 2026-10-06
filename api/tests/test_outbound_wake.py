"""Idle delivery loops back off and wake at once for a new fax; an idle check takes no write lock.

The audit of 2026-10-05 measured the API at about 3% of one core while idle:
the worker and poller each took the database write lock every second and the
worker decrypted the configuration every second, even with fax disabled.
"""
import asyncio
import logging
import threading
import time

import pytest

from api.app.outbound_wake import IdleBackoff, Wake, idle_loop
from api.tests.test_outbound_store import accept, installation  # noqa: F401 (fixtures)
from api.tests.test_schema import database  # noqa: F401 (fixture)


def test_the_back_off_doubles_to_ten_seconds_and_starts_over_after_work():
    backoff = IdleBackoff()
    assert [backoff.next() for _ in range(6)] == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
    backoff.reset()
    assert backoff.next() == 1.0
    with pytest.raises(ValueError):
        IdleBackoff(0, 10)


class Recorder:
    """A wake signal that records each wait and never sleeps; ``woken`` waits return True."""

    def __init__(self, woken=()):
        self.generation, self.waits, self.woken = 0, [], set(woken)

    async def wait(self, seconds, seen):
        self.waits.append(seconds)
        if len(self.waits) > 8:
            raise asyncio.CancelledError
        return len(self.waits) in self.woken


def run(step, signal):
    async def main():
        with pytest.raises(asyncio.CancelledError):
            await idle_loop(step, backoff=IdleBackoff(), warning='x', logger=logging.getLogger('t'), signal=signal)
    asyncio.run(main())


def test_an_idle_loop_waits_longer_each_time_and_work_or_a_wake_up_starts_it_over():
    results = iter([False, False, False, False, False, True, False, False, False, False, False])

    async def step():
        return next(results)
    signal = Recorder(woken={7})
    run(step, signal)
    # Five idle checks, then work (no wait), then idle again from 1 s; the 7th wait was a wake-up, so 1 s again.
    assert signal.waits == [1.0, 2.0, 4.0, 8.0, 10.0, 1.0, 2.0, 1.0, 2.0]


def test_a_failing_check_counts_as_idle_and_is_logged_without_details(caplog):
    async def step():
        raise RuntimeError('provider said something private')
    signal = Recorder()
    with caplog.at_level(logging.WARNING):
        run(step, signal)
    assert signal.waits[:3] == [1.0, 2.0, 4.0]
    assert 'private' not in caplog.text and 'x' in caplog.text


def test_a_new_fax_wakes_an_idle_loop_at_once_even_from_another_thread():
    signal = Wake()
    calls = []

    async def main():
        async def step():
            calls.append(time.monotonic())
            return False
        task = asyncio.create_task(idle_loop(step, backoff=IdleBackoff(1.0, 10.0), warning='x',
                                             logger=logging.getLogger('t'), signal=signal))
        await asyncio.sleep(3.2)  # checks at 0, 1 and 3 s; the next wait is 4 s
        waited = time.monotonic()
        threading.Thread(target=signal.notify).start()
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return waited
    woken = asyncio.run(main())
    assert len(calls) == 4 and calls[-1] - woken < 0.3


def test_a_wake_up_that_arrives_during_a_check_is_kept_for_the_next_wait():
    signal = Wake()
    seen = signal.generation
    signal.notify()  # while the loop was busy
    assert asyncio.run(signal.wait(10, seen)) is True
    assert asyncio.run(asyncio.wait_for(signal.wait(0.05, signal.generation), 1)) is False


def test_an_idle_store_takes_no_write_lock_and_reads_the_configuration_once_per_revision(installation):  # noqa: F811
    from api.app.outbound_wake import wake
    configuration, delivery, snapshot = installation
    locks, revisions = [], []
    original_locked, original_revision = configuration._locked, configuration._revision

    def locked(*args, **kwargs):
        locks.append(1)
        return original_locked(*args, **kwargs)

    def revision(*args, **kwargs):
        revisions.append(1)
        return original_revision(*args, **kwargs)
    configuration._locked, configuration._revision = locked, revision
    try:
        for _ in range(3):
            assert delivery.recover_expired() == 0
            assert delivery.claim('worker') is None
            assert delivery.reserve_poll() is None
        assert locks == [] and revisions == []
        # A new fax wakes the loops, and the next check takes the lock and reads the configuration once.
        before = wake.generation
        configuration._locked = original_locked
        job = accept(installation)
        configuration._locked = locked
        assert wake.generation > before
        claim = delivery.claim('worker')
        assert claim is not None and claim.job_id == job and len(locks) == 1
        # Whether the active revision sends is decrypted once, then remembered for that revision.
        delivery._values_for = None
        revisions.clear()
        with configuration.engine.connect() as connection:
            assert delivery._enabled(connection) is True and delivery._enabled(connection) is True
        assert len(revisions) == 1
    finally:
        configuration._locked, configuration._revision = original_locked, original_revision


def test_reading_the_configuration_decrypts_each_revision_once(installation):  # noqa: F811
    configuration, _, _ = installation
    opened = []
    original = configuration._open_revision

    def open_revision(*args, **kwargs):
        opened.append(1)
        return original(*args, **kwargs)
    configuration._open_revision = open_revision
    configuration._revision_cache = {}
    first, second = configuration.read(), configuration.read()
    assert len(opened) == 1 and first.active is second.active
