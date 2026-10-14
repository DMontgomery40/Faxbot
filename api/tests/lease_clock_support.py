"""A lease clock for tests whose preparation outlives a lease (tests/test_joint_optimizer.py, group 5 combinations).

Lease decisions (the claim, its renewal, the sweep for expired leases, the submission marker) read the outbound
store's clock. These tests freeze it and move it themselves, so a slow runner can never run a lease out: in CI one
claim held the write lock for 4.8 s on SQLite and 11.4 s on PostgreSQL, longer than the tests' 2 s lease, and the
store rightly refused the claim. Here only the test runs a lease out, by moving the clock past it. The workers'
heartbeats still renew on their own real-time beat; the clock moves on only once every lease being prepared reaches
past where it moves to. The capacity check reads the same clock, so the claim and the transport count a fax holding
its line at the same moment.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import threading

import sqlalchemy as sa

# Real seconds to wait for the heartbeats before calling a worker stuck. It bounds a hang, never a lease.
STUCK_SECONDS = 120.0


class LeaseClock:
    def __init__(self, env, monkeypatch):
        from api.app import capacity, outbound_store
        self.env = env
        self.now = datetime.utcnow()
        clock = self

        class Frozen(datetime):
            @classmethod
            def utcnow(cls):
                return clock.now

        monkeypatch.setattr(outbound_store, 'datetime', Frozen)
        room = capacity.Capacity.room
        monkeypatch.setattr(capacity.Capacity, 'room',
                            lambda self, connection, values, now, **kwargs: room(self, connection, values, clock.now,
                                                                                 **kwargs))
        # A claim reads the clock before it writes its lease: the clock stays put until every claim has returned.
        self._claiming, self._guard = 0, threading.Lock()
        claim = env.delivery.claim

        def counted(*args, **kwargs):
            with self._guard:
                self._claiming += 1
            try:
                return claim(*args, **kwargs)
            finally:
                with self._guard:
                    self._claiming -= 1
        monkeypatch.setattr(env.delivery, 'claim', counted)

    def _leases(self):
        table = self.env.delivery.deliveries
        with self.env.engine.connect() as connection:
            return connection.execute(sa.select(table.c.id, table.c.claim_expires_at).where(
                table.c.state == 'preparing')).all()

    async def _renewed_past(self, moment):
        """Until no claim is being made and every lease being prepared ends after ``moment``."""
        try:
            async with asyncio.timeout(STUCK_SECONDS):
                while True:
                    if not self._claiming:
                        leases = await asyncio.to_thread(self._leases)
                        if not self._claiming and all(expires is not None and expires > moment
                                                      for _, expires in leases):
                            return
                    await asyncio.sleep(0.05)
        except TimeoutError:
            raise AssertionError(f'No heartbeat renewed every lease past {moment} in {STUCK_SECONDS:.0f} s '
                                 f'(claims being made: {self._claiming}; leases: {self._leases()}).') from None

    async def outlive(self, seconds, *, lease, renewed=True):
        """Move the clock ``seconds`` on, as a long preparation takes them. ``renewed``: in steps of half a lease,
        each once the heartbeats have renewed every lease past it. Otherwise (no heartbeat renews) at once."""
        target = self.now + timedelta(seconds=seconds)
        step = timedelta(seconds=lease / 2)
        while self.now < target:
            moment = min(target, self.now + step) if renewed else target
            if renewed:
                await self._renewed_past(moment)
            self.now = moment
