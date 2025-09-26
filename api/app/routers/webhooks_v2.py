"""
Enhanced webhook router with verification, DLQ, and idempotency support.

This router provides enterprise-grade webhook handling:
- Signature verification for all providers
- Dead Letter Queue for failed processing
- Idempotency key support
- Structured audit logging
"""

import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, Request, HTTPException, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.app.services.webhook_processor import WebhookProcessor
from api.app.plugins.manager import PluginManager
from api.app.services.events import EventEmitter
from api.app.config.provider import HybridConfigProvider
from api.app.main import get_plugin_manager, get_event_emitter, get_config_provider


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/v2", tags=["Webhooks V2"])


class WebhookResponse(BaseModel):
    success: bool
    message: str
    job_id: Optional[str] = None
    status: str


def get_webhook_processor(
    plugin_manager: PluginManager = Depends(get_plugin_manager),
    event_emitter: EventEmitter = Depends(get_event_emitter),
    config_provider: HybridConfigProvider = Depends(get_config_provider)
) -> WebhookProcessor:
    """Dependency to get webhook processor."""
    return WebhookProcessor(plugin_manager, event_emitter, config_provider)


async def extract_headers(request: Request) -> Dict[str, str]:
    """Extract headers from request for webhook processing."""
    return {k.lower(): v for k, v in request.headers.items()}


@router.post("/{provider_id}", response_model=WebhookResponse)
async def process_provider_webhook(
    provider_id: str,
    request: Request,
    processor: WebhookProcessor = Depends(get_webhook_processor),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """
    Process webhook from any provider with unified handling.

    This endpoint provides enterprise-grade webhook processing:
    - Signature verification using provider-specific methods
    - Job lookup by (provider_id, external_id)
    - Dead Letter Queue for failed processing
    - Idempotency key support for duplicate prevention
    """
    try:
        # Extract request data
        headers = await extract_headers(request)
        body = await request.body()

        logger.info(f"Processing webhook from {provider_id}, size: {len(body)} bytes")

        # Process webhook
        result = await processor.process_webhook(
            provider_id=provider_id,
            headers=headers,
            body=body,
            idempotency_key=idempotency_key
        )

        # Return appropriate response
        status_code = result.get("status", 500)
        if not result.get("success", False):
            logger.warning(f"Webhook processing failed for {provider_id}: {result.get('error')}")
            raise HTTPException(status_code=status_code, detail=result.get("error", "Unknown error"))

        return WebhookResponse(
            success=True,
            message=f"Webhook processed successfully for {provider_id}",
            job_id=result.get("job_id"),
            status=result.get("new_status", "processed")
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error processing webhook from {provider_id}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post("/phaxio", response_model=WebhookResponse)
async def phaxio_webhook(
    request: Request,
    processor: WebhookProcessor = Depends(get_webhook_processor),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """Legacy Phaxio webhook endpoint - redirects to unified handler."""
    return await process_provider_webhook("phaxio", request, processor, idempotency_key)


@router.post("/sinch", response_model=WebhookResponse)
async def sinch_webhook(
    request: Request,
    processor: WebhookProcessor = Depends(get_webhook_processor),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """Legacy Sinch webhook endpoint - redirects to unified handler."""
    return await process_provider_webhook("sinch", request, processor, idempotency_key)


@router.post("/signalwire", response_model=WebhookResponse)
async def signalwire_webhook(
    request: Request,
    processor: WebhookProcessor = Depends(get_webhook_processor),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    """Legacy SignalWire webhook endpoint - redirects to unified handler."""
    return await process_provider_webhook("signalwire", request, processor, idempotency_key)


# Health check endpoint
@router.get("/health")
async def webhook_health():
    """Health check for webhook service."""
    return {"status": "healthy", "service": "webhooks_v2"}


# DLQ management endpoints (admin only)
from api.app.main import require_admin

@router.get("/admin/dlq", dependencies=[Depends(require_admin)])
async def get_dlq_entries(
    limit: int = 50,
    provider_id: Optional[str] = None,
    status: Optional[str] = None
):
    """Get DLQ entries for admin review."""
    from sqlalchemy import select
    from api.app.database import AsyncSessionLocal
    from api.app.models.webhook_dlq import WebhookDLQ

    try:
        async with AsyncSessionLocal() as session:
            stmt = select(WebhookDLQ).order_by(WebhookDLQ.received_at.desc()).limit(limit)

            if provider_id:
                stmt = stmt.where(WebhookDLQ.provider_id == provider_id)
            if status:
                stmt = stmt.where(WebhookDLQ.status == status)

            result = await session.execute(stmt)
            entries = result.scalars().all()

            return {
                "entries": [
                    {
                        "id": entry.id,
                        "provider_id": entry.provider_id,
                        "external_id": entry.external_id,
                        "received_at": entry.received_at.isoformat(),
                        "status": entry.status,
                        "error": entry.error,
                        "retry_count": entry.retry_count,
                        "headers_meta": entry.get_headers_meta()
                    }
                    for entry in entries
                ],
                "total": len(entries)
            }

    except Exception as e:
        logger.exception("Error retrieving DLQ entries")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/dlq/{dlq_id}/retry", dependencies=[Depends(require_admin)])
async def retry_dlq_entry(dlq_id: str):
    """Manually retry a specific DLQ entry."""
    from sqlalchemy import select
    from api.app.database import AsyncSessionLocal
    from api.app.models.webhook_dlq import WebhookDLQ
    from datetime import datetime

    try:
        async with AsyncSessionLocal() as session:
            stmt = select(WebhookDLQ).where(WebhookDLQ.id == dlq_id)
            result = await session.execute(stmt)
            entry = result.scalar_one_or_none()

            if not entry:
                raise HTTPException(status_code=404, detail="DLQ entry not found")

            # Reset for immediate retry
            entry.status = 'retrying'
            entry.next_retry_at = datetime.utcnow()

            await session.commit()

            return {
                "success": True,
                "message": f"DLQ entry {dlq_id} scheduled for retry",
                "entry_id": dlq_id
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error retrying DLQ entry {dlq_id}")
        raise HTTPException(status_code=500, detail=str(e))