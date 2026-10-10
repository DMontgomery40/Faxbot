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
from .config_store import ConfigurationStoreError
from .database_busy import busy_cause, retry_when_busy


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


def _lease_lost(error):
    """The store's refusal of a preparation step whose claim ran out (``OutboundStore._preparing``)."""
    from .outbound_store import DeliveryConflict
    return isinstance(error, DeliveryConflict) and str(error) == 'Delivery preparation lease is no longer current.'


class OutboundWorker:
    def __init__(self, store, transport, *, interval=1.0, submission_timeout=90.0, clock=None, lease_seconds=30):
        if interval <= 0 or submission_timeout <= 0 or not 1 <= lease_seconds <= 300:
            raise ValueError('Worker intervals must be positive.')
        self.store = store
        self.transport = transport
        self.owner = uuid4().hex
        self.interval = interval
        self.submission_timeout = submission_timeout
        # How long a claim lasts before another worker may recover it; renewed while preparation runs.
        self.lease_seconds = lease_seconds
        self.clock = clock or datetime.utcnow
        # Faxes given back for lack of room, and when the worker may claim them again. In memory:
        # after a restart a fax is at most given back once more.
        self.paused = {}

    def _exclude(self):
        now = self.clock()
        self.paused = {job: until for job, until in self.paused.items() if until > now}
        return tuple(self.paused)

    async def _heartbeat(self, claim, stop):
        """Renew the claim's lease every third of it while the transport prepares (measuring and encoding a long
        document can take longer than one lease); stops at once when preparation ends or the lease is lost."""
        renew = getattr(self.store, 'renew_lease', None)
        if renew is None:
            return
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.lease_seconds / 3)
                return
            except asyncio.TimeoutError:
                pass
            try:
                renewed = await run_lifecycle_step(lambda: renew(claim, lease_seconds=self.lease_seconds))
            except ConfigurationStoreError as error:
                # The database was busy (or its answer was lost): the lease still runs, so preparation goes on and
                # the next beat tries again. A lease that did run out makes begin_submission refuse; nothing is sent.
                logging.getLogger(__name__).warning(
                    'Fax %s: its claim could not be renewed this time (%s); trying again at the next beat.',
                    claim.job_id, busy_cause(error) or 'the database did not answer')
                continue
            if not renewed:
                logging.getLogger(__name__).warning('Fax %s: its claim was lost while it was being prepared; '
                                                    'nothing is sent by this attempt.', claim.job_id)
                return

    async def _give_back(self, claim, seconds=5.0):
        """Return a claim nothing was sent for to the queue, as for a full line (``defer``); still busy, its lease
        runs out and lease recovery returns it (``recover_expired``)."""
        try:
            await run_lifecycle_step(lambda: retry_when_busy(lambda: self.store.defer(claim),
                                                             what=f'the return of fax {claim.job_id} to the queue'))
        except ConfigurationStoreError:
            pass  # logged by retry_when_busy; the lease runs out and lease recovery gives it back
        until = self.clock() + timedelta(seconds=seconds)
        for member in claim.everyone:
            self.paused[member.job_id] = until

    async def step(self):
        await run_lifecycle_step(self.store.recover_expired)
        exclude = self._exclude()
        if self.lease_seconds == 30:
            claim = await run_lifecycle_step(lambda: self.store.claim(self.owner, exclude=exclude))
        else:
            claim = await run_lifecycle_step(lambda: self.store.claim(self.owner, exclude=exclude,
                                                                      lease_seconds=self.lease_seconds))
        if claim is None:
            return False
        preparing = True
        stop = asyncio.Event()
        beat = asyncio.ensure_future(self._heartbeat(claim, stop))
        try:
            async with self.transport.prepare(claim) as operation:
                # Preparation is over: the durable marker below needs the lease, no longer its renewal.
                stop.set()
                await beat
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
                # commit may already have succeeded when its response is lost:
                # that one is never tried again (the fax becomes uncertain when
                # its lease ends, and the call's own result still settles it).
                # A busy database that refused it outright is tried again.
                await run_lifecycle_step(lambda: retry_when_busy(lambda: self.store.record_receipt(claim,
                    provider_sid=receipt.provider_sid, status=receipt.status, error=receipt.error),
                    what=f'the hand-over of fax {claim.job_id} to its provider'))
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
        except Exception as error:
            if not preparing:
                raise
            cause = busy_cause(error) or ('its claim ran out while it waited' if _lease_lost(error) else None)
            if cause is not None:
                # A step before the call (reading the fax, its pages or its route) found the database too busy, or
                # waited so long for it that the claim ran out: nothing was sent, so the fax goes back to the queue
                # and is claimed again in a moment. It never fails for it.
                logging.getLogger(__name__).warning('Fax %s waits a moment: the database was too busy to prepare it '
                                                    '(%s); nothing was sent.', claim.job_id, cause)
                await self._give_back(claim)
                return False
            # Any other error before the durable marker is a bug or an integration failure (such as a database
            # whose migration did not run), and nothing was sent: the fax fails as a preparation failure with its
            # cause logged, so its pre-data rules apply. Raised, it would be claimed, leased and recovered for ever.
            logging.getLogger(__name__).exception('Fax %s could not be prepared; nothing was sent.', claim.job_id)
            await run_lifecycle_step(lambda: self.store.fail_preparation(claim, category='preparation_failed'))
        finally:
            stop.set()
            if not beat.done():
                await beat
        return True

    async def run(self):
        """Claim work at once after work; when idle, back off to ten intervals (10 s), and start at once on a new fax."""
        from .outbound_wake import IdleBackoff, idle_loop
        await idle_loop(self.step, backoff=IdleBackoff(self.interval, self.interval * 10),
                        warning='Delivery worker operation requires recovery.', logger=logging.getLogger(__name__))
