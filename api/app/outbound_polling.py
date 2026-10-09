"""Bounded status reads through each fax's immutable provider account."""
import asyncio
import hashlib
import logging

from .config_runtime import run_lifecycle_step
from .outbound_store import NO_FALLBACK_CATEGORIES
from .outbound_transport import _receipt
from .provider_execution import service_from_profile, UnsupportedProviderExecutionError


class OutboundPoller:
    def __init__(self, store, *, interval=1.0, timeout=20.0):
        if interval <= 0 or timeout <= 0:
            raise ValueError('Polling intervals must be positive.')
        self.store, self.interval, self.timeout = store, interval, timeout

    async def refresh(self, job_id, *, automatic=False):
        target = await run_lifecycle_step(lambda: self.store.poll_target(job_id, automatic=automatic))
        return await self.refresh_target(job_id, target)

    async def refresh_target(self, job_id, target):
        """Consume a captured lookup; completion is background delivery evidence."""
        if target is None:
            return False
        profile, attempt_id, sid = target
        configuration = profile.configuration
        service = service_from_profile(profile)
        manifest = configuration.manifest is not None
        if manifest and 'get_status' not in service.m.actions:
            raise UnsupportedProviderExecutionError('This provider does not support status refresh.')
        async with asyncio.timeout(self.timeout):
            result = (await service.get_status(job_id=job_id, provider_sid=sid) if manifest
                      else await service.get_fax_status(sid))
        receipt = _receipt(result, manifest=manifest,
                           sinch=not manifest and configuration.provider_id == 'sinch')
        identity = '\0'.join((attempt_id, receipt.provider_sid, receipt.status))
        key = 'poll:' + hashlib.sha256(identity.encode()).hexdigest()
        # A provider adapter's own plain sentence for a failure (never the provider's text).
        failure = result.get('failure') if isinstance(result, dict) and receipt.status == 'failed' else None
        # The adapter's own judgment that pages may have reached the fax machine: never another route.
        category = result.get('failure_category') if isinstance(result, dict) and receipt.status == 'failed' else None
        if category == 'pages_unconfirmed':
            # No page was confirmed, yet part of one may have arrived: uncertain, waiting for a person,
            # exactly as for the fax engine's own calls.
            return await run_lifecycle_step(lambda: self.store.record_unconfirmed(job_id,
                attempt_id=attempt_id, profile_id=profile.id, event_key=key))
        # The adapter's classification of a failed call: it ended before any fax data, or not, or it cannot say.
        ended = result.get('before_fax_data') if isinstance(result, dict) and receipt.status == 'failed' else None
        observed = await run_lifecycle_step(lambda: self.store.observe(job_id,
            attempt_id=attempt_id, profile_id=profile.id, provider_sid=receipt.provider_sid,
            status=receipt.status, event_key=key, error=failure if isinstance(failure, str) else None,
            error_category=category if category in NO_FALLBACK_CATEGORIES else None,
            before_data=ended if isinstance(ended, bool) else None))
        if receipt.status == 'failed' and not manifest:
            # The service's own count of pages sent, kept once (evidence only) for sending only the rest of a
            # broken fax (routing/continuation.py); it never changes the delivery.
            from .routing.continuation import record_provider_report
            await run_lifecycle_step(lambda: record_provider_report(
                self.store.configuration.engine, job_id=job_id, attempt_id=attempt_id,
                provider=configuration.provider_id, result=result))
        return observed

    async def step(self):
        job_id = await run_lifecycle_step(self.store.reserve_poll)
        if job_id is None:
            return False
        try:
            await self.refresh(job_id, automatic=True)
        except UnsupportedProviderExecutionError:
            pass  # Native transports report through their owned event listeners.
        return True

    async def run(self):
        """Read statuses at once while some are due; when idle, back off to ten intervals (10 s), and wake on new work."""
        from .outbound_wake import IdleBackoff, idle_loop
        await idle_loop(self.step, backoff=IdleBackoff(self.interval, self.interval * 10),
                        warning='Provider status refresh is temporarily unavailable.',
                        logger=logging.getLogger(__name__))
