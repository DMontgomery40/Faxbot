"""Owned delivery worker; only a committed claim may authorize transmission.

The transport's preparation context owns its resources. Its submit method is
called once, after the durable marker returns successfully. A marker or receipt
commit whose acknowledgement is lost is left for durable recovery; the worker
must never guess whether that transaction committed.
"""
import asyncio
from dataclasses import dataclass
import logging
from uuid import uuid4

from .config_runtime import run_lifecycle_step


@dataclass(frozen=True)
class SubmissionReceipt:
    provider_sid: str | None
    status: str

    def __post_init__(self):
        if self.status not in {'in_progress', 'success', 'failed', 'cancelled'}:
            raise ValueError('Unusable provider status.')
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


class OutboundWorker:
    def __init__(self, store, transport, *, interval=1.0, submission_timeout=90.0):
        if interval <= 0 or submission_timeout <= 0:
            raise ValueError('Worker intervals must be positive.')
        self.store = store
        self.transport = transport
        self.owner = uuid4().hex
        self.interval = interval
        self.submission_timeout = submission_timeout

    async def step(self):
        await run_lifecycle_step(self.store.recover_expired)
        claim = await run_lifecycle_step(lambda: self.store.claim(self.owner))
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
                    provider_sid=receipt.provider_sid, status=receipt.status))
        except PreparationFailure as error:
            if not preparing:
                raise
            category = error.category
            await run_lifecycle_step(lambda: self.store.fail_preparation(claim, category=category))
        return True

    async def run(self):
        while True:
            try:
                worked = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                # No provider payload, recipient, URL, credentials or traceback.
                logging.getLogger(__name__).warning('Delivery worker operation requires recovery.')
                worked = False
            if not worked:
                await asyncio.sleep(self.interval)
