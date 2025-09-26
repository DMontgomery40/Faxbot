"""
Webhook processing service with verification, routing, and DLQ support.

This service provides enterprise-grade webhook handling:
- Signature verification using plugin-specific methods
- Job lookup by (provider_id, external_id)
- Retry logic with exponential backoff
- Dead Letter Queue for failed processing
- Idempotency key support
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.app.database import AsyncSessionLocal
from api.app.models.webhook_dlq import WebhookDLQ
from api.app.models.jobs import Job
from api.app.plugins.manager import PluginManager
from api.app.services.events import EventEmitter, EventType
from api.app.config.provider import HybridConfigProvider


logger = logging.getLogger(__name__)


class WebhookProcessor:
    """
    Processes incoming webhooks with verification, routing, and DLQ support.
    """

    def __init__(self, plugin_manager: PluginManager, event_emitter: EventEmitter,
                 config_provider: HybridConfigProvider):
        self.plugin_manager = plugin_manager
        self.event_emitter = event_emitter
        self.config_provider = config_provider
        self._idempotency_cache: Dict[str, Any] = {}

    async def process_webhook(self, provider_id: str, headers: Dict[str, str],
                            body: bytes, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """
        Process an incoming webhook with full verification and error handling.

        Args:
            provider_id: Provider that sent the webhook
            headers: HTTP headers from webhook request
            body: Raw webhook body
            idempotency_key: Optional idempotency key for duplicate prevention

        Returns:
            Dict with processing result and status
        """
        try:
            # Check idempotency
            if idempotency_key and idempotency_key in self._idempotency_cache:
                return self._idempotency_cache[idempotency_key]

            # Get provider plugin
            plugin = self.plugin_manager.get_provider_plugin(provider_id)
            if not plugin:
                error = f"No plugin found for provider: {provider_id}"
                logger.error(error)
                return {"success": False, "error": error, "status": 404}

            # Verify webhook signature
            try:
                if hasattr(plugin, 'verify_webhook'):
                    is_valid = await plugin.verify_webhook(headers, body)
                    if not is_valid:
                        error = f"Webhook signature verification failed for provider: {provider_id}"
                        logger.warning(error)
                        return {"success": False, "error": error, "status": 401}
            except Exception as e:
                logger.error(f"Webhook verification error for {provider_id}: {str(e)}")
                return {"success": False, "error": f"Verification failed: {str(e)}", "status": 500}

            # Parse webhook payload
            try:
                payload_str = body.decode('utf-8')
                payload = json.loads(payload_str)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                error = f"Invalid webhook payload from {provider_id}: {str(e)}"
                logger.error(error)
                await self._add_to_dlq(provider_id, None, error, headers)
                return {"success": False, "error": error, "status": 400}

            # Extract external_id from payload
            external_id = await self._extract_external_id(plugin, payload, provider_id)
            if not external_id:
                error = f"Could not extract external_id from {provider_id} webhook"
                logger.error(error)
                await self._add_to_dlq(provider_id, None, error, headers)
                return {"success": False, "error": error, "status": 400}

            # Find job by provider_id and external_id
            job = await self._find_job(provider_id, external_id)
            if not job:
                error = f"No job found for {provider_id} external_id: {external_id}"
                logger.warning(error)
                await self._add_to_dlq(provider_id, external_id, error, headers)
                return {"success": False, "error": error, "status": 404}

            # Process status callback
            result = await self._process_status_callback(plugin, job, payload)

            # Cache result for idempotency
            if idempotency_key:
                self._idempotency_cache[idempotency_key] = result

            return result

        except Exception as e:
            error = f"Unexpected error processing webhook from {provider_id}: {str(e)}"
            logger.exception(error)
            await self._add_to_dlq(provider_id, None, error, headers)
            return {"success": False, "error": error, "status": 500}

    async def _extract_external_id(self, plugin: Any, payload: Dict[str, Any],
                                 provider_id: str) -> Optional[str]:
        """Extract external_id from webhook payload using provider-specific logic."""
        try:
            if hasattr(plugin, 'extract_external_id'):
                return await plugin.extract_external_id(payload)

            # Fallback logic for common providers
            if provider_id == 'phaxio':
                return payload.get('fax', {}).get('id')
            elif provider_id == 'sinch':
                return payload.get('id')
            elif provider_id.startswith('sip_'):
                return payload.get('job_id') or payload.get('external_id')

            # Generic fallback
            return payload.get('id') or payload.get('external_id') or payload.get('job_id')

        except Exception as e:
            logger.error(f"Error extracting external_id from {provider_id}: {str(e)}")
            return None

    async def _find_job(self, provider_id: str, external_id: str) -> Optional[Job]:
        """Find job by provider_id and external_id."""
        try:
            async with AsyncSessionLocal() as session:
                stmt = select(Job).where(
                    Job.provider_id == provider_id,
                    Job.external_id == external_id
                )
                result = await session.execute(stmt)
                return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error finding job for {provider_id}/{external_id}: {str(e)}")
            return None

    async def _process_status_callback(self, plugin: Any, job: Job,
                                     payload: Dict[str, Any]) -> Dict[str, Any]:
        """Process status callback and emit events."""
        try:
            old_status = job.status

            # Let plugin handle the status update
            if hasattr(plugin, 'handle_status_callback'):
                await plugin.handle_status_callback(job, payload)
            else:
                # Default status mapping
                await self._update_job_status_default(job, payload)

            # Emit status change event
            if job.status != old_status:
                await self.event_emitter.emit_event(
                    EventType.FAX_STATUS_CHANGED,
                    job_id=job.id,
                    provider_id=job.provider_id,
                    payload_meta={
                        'old_status': old_status,
                        'new_status': job.status,
                        'external_id': job.external_id
                    }
                )

            return {
                "success": True,
                "job_id": job.id,
                "old_status": old_status,
                "new_status": job.status,
                "status": 200
            }

        except Exception as e:
            error = f"Error processing status callback: {str(e)}"
            logger.exception(error)
            return {"success": False, "error": error, "status": 500}

    async def _update_job_status_default(self, job: Job, payload: Dict[str, Any]):
        """Default job status update logic."""
        # This is a fallback - plugins should implement handle_status_callback
        status_field = payload.get('status') or payload.get('state')
        if status_field:
            # Map provider-specific statuses to canonical ones
            status_map = {
                'success': 'SUCCESS',
                'completed': 'SUCCESS',
                'delivered': 'SUCCESS',
                'failed': 'FAILED',
                'error': 'FAILED',
                'pending': 'QUEUED',
                'queued': 'QUEUED',
                'processing': 'SENDING'
            }
            canonical_status = status_map.get(status_field.lower(), status_field.upper())
            job.status = canonical_status

        async with AsyncSessionLocal() as session:
            session.add(job)
            await session.commit()

    async def _add_to_dlq(self, provider_id: str, external_id: Optional[str],
                         error: str, headers: Dict[str, str]):
        """Add failed webhook to Dead Letter Queue."""
        try:
            safe_headers = WebhookDLQ.create_safe_headers_meta(headers)
            dlq_entry = WebhookDLQ(
                provider_id=provider_id,
                external_id=external_id,
                error=error,
                headers_meta=safe_headers
            )

            async with AsyncSessionLocal() as session:
                session.add(dlq_entry)
                await session.commit()

            logger.info(f"Added webhook to DLQ: provider={provider_id}, external_id={external_id}")

        except Exception as e:
            logger.error(f"Failed to add webhook to DLQ: {str(e)}")

    async def retry_dlq_entries(self, max_retries: int = 3):
        """
        Process DLQ entries that are ready for retry.

        This should be called periodically by a background task.
        """
        try:
            async with AsyncSessionLocal() as session:
                # Find DLQ entries ready for retry
                now = datetime.utcnow()
                stmt = select(WebhookDLQ).where(
                    WebhookDLQ.status == 'retrying',
                    WebhookDLQ.next_retry_at <= now,
                    WebhookDLQ.retry_count.cast(int) < max_retries
                )
                result = await session.execute(stmt)
                dlq_entries = result.scalars().all()

                for entry in dlq_entries:
                    await self._retry_dlq_entry(session, entry, max_retries)

                await session.commit()

        except Exception as e:
            logger.error(f"Error processing DLQ retries: {str(e)}")

    async def _retry_dlq_entry(self, session: AsyncSession, entry: WebhookDLQ, max_retries: int):
        """Retry a single DLQ entry."""
        try:
            retry_count = int(entry.retry_count)
            if retry_count >= max_retries:
                entry.mark_failed(f"Max retries ({max_retries}) exceeded")
                return

            # Calculate exponential backoff
            backoff_seconds = min(300, 30 * (2 ** retry_count))  # Max 5 minutes
            next_retry = datetime.utcnow() + timedelta(seconds=backoff_seconds)

            entry.increment_retry(next_retry)

            logger.info(f"Scheduled DLQ entry for retry: {entry.id}, attempt {retry_count + 1}")

        except Exception as e:
            logger.error(f"Error retrying DLQ entry {entry.id}: {str(e)}")
            entry.mark_failed(f"Retry error: {str(e)}")

    def clear_idempotency_cache(self, older_than_minutes: int = 60):
        """Clear old idempotency cache entries to prevent memory leaks."""
        # In production, this should use Redis with TTL
        # For now, we'll keep it simple
        if len(self._idempotency_cache) > 1000:
            self._idempotency_cache.clear()
            logger.info("Cleared idempotency cache due to size limit")