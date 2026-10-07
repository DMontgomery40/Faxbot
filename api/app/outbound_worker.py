"""Owned delivery worker; only a committed claim may authorize transmission.

The transport's preparation context owns its resources. Its submit method is
called once, after the durable marker returns successfully. A marker or receipt
commit whose acknowledgement is lost is left for durable recovery; the worker
must never guess whether that transaction committed.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from uuid import uuid4

from .config_runtime import run_lifecycle_step


@dataclass(frozen=True)
class SubmissionReceipt:
    provider_sid: str | None
    status: str
    # The adapter's own plain sentence for a failure the provider answered at once (never provider text).
    error: str | None = None

    def __post_init__(self):
        if self.status not in {'in_progress', 'success', 'failed', 'cancelled'}:
            raise ValueError('Unusable provider status.')
        if self.error is not None and (self.status != 'failed' or not isinstance(self.error, str)
                                       or not 0 < len(self.error) <= 200):
            raise ValueError('Unusable failure sentence.')
        if self.provider_sid is not None and (not isinstance(self.provider_sid, str)
                or not self.provider_sid or len(self.provider_sid) > 100
                or any(ord(char) < 32 for char in self.provider_sid)):
            raise ValueError('Unusable provider identity.')


class PreparationFailure(RuntimeError):
    def __init__(self, category='preparation_failed'):
        if category not in {'artifact_unavailable', 'provider_unavailable', 'preparation_failed'}:
            raise ValueError('Invalid preparation failure category.')
        self.category = category
        super().__init__('Fax preparation failed before submission.')


class BatchSplit(RuntimeError):
    """A shared call cannot go as planned; nothing was sent and no fax failed.

    ``separate`` names the faxes that go on their own from now on; None means
    every fax in the call. The others wait again and form a new call.
    """

    def __init__(self, separate=None):
        self.separate = frozenset(separate) if separate is not None else None
        super().__init__('The shared call was split before anything was sent.')


class CapacityWait(RuntimeError):
    """No room for this fax's call yet (its route's trunk is full); nothing was sent and nothing failed."""

    def __init__(self, seconds=5.0):
        self.seconds = seconds
        super().__init__('The fax waits for a free line.')


class OutboundWorker:
    def __init__(self, store, transport, *, interval=1.0, submission_timeout=90.0, clock=None):
        if interval <= 0 or submission_timeout <= 0:
            raise ValueError('Worker intervals must be positive.')
        self.store = store
        self.transport = transport
        self.owner = uuid4().hex
        self.interval = interval
        self.submission_timeout = submission_timeout
        self.clock = clock or datetime.utcnow
        # Faxes given back for lack of room, and when the worker may claim them again. In memory:
        # after a restart a fax is at most given back once more.
        self.paused = {}

    def _exclude(self):
        now = self.clock()
        self.paused = {job: until for job, until in self.paused.items() if until > now}
        return tuple(self.paused)

    async def step(self):
        await run_lifecycle_step(self.store.recover_expired)
        exclude = self._exclude()
        claim = await run_lifecycle_step(lambda: self.store.claim(self.owner, exclude=exclude))
        if claim is None:
            return False
        preparing = True
        try:
            async with self.transport.prepare(claim) as operation:
                # Set before awaiting: a lost commit acknowledgement must not
                # be classified as a definite failure before transmission.
                preparing = False
                authorized = await run_lifecycle_step(lambda: self.store.begin_submission(claim))
                if not authorized:
                    return True
                try:
                    async with asyncio.timeout(self.submission_timeout):
                        receipt = await operation.submit()
                    if not isinstance(receipt, SubmissionReceipt):
                        raise ValueError('Unusable provider receipt.')
                except asyncio.CancelledError:
                    await run_lifecycle_step(lambda: self.store.record_uncertain(
                        claim, category='submission_cancelled'))
                    raise
                except Exception:
                    await run_lifecycle_step(lambda: self.store.record_uncertain(claim))
                    return True
                # Deliberately outside the transport exception handler. This
                # commit may already have succeeded when its response is lost.
                await run_lifecycle_step(lambda: self.store.record_receipt(claim,
                    provider_sid=receipt.provider_sid, status=receipt.status, error=receipt.error))
        except PreparationFailure as error:
            if not preparing:
                raise
            category = error.category
            await run_lifecycle_step(lambda: self.store.fail_preparation(claim, category=category))
        except BatchSplit as split:
            if not preparing:
                raise
            separate = split.separate
            await run_lifecycle_step(lambda: self.store.split_batch(claim, separate=separate))
        except CapacityWait as wait:
            if not preparing:
                raise
            await run_lifecycle_step(lambda: self.store.defer(claim))
            until = self.clock() + timedelta(seconds=wait.seconds)
            for member in claim.everyone:
                self.paused[member.job_id] = until
            return False
        return True

    async def run(self):
        """Claim work at once after work; when idle, back off to ten intervals (10 s), and start at once on a new fax."""
        from .outbound_wake import IdleBackoff, idle_loop
        await idle_loop(self.step, backoff=IdleBackoff(self.interval, self.interval * 10),
                        warning='Delivery worker operation requires recovery.', logger=logging.getLogger(__name__))
