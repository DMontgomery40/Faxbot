"""
Admin endpoints for provider health monitoring and circuit breaker management.

Provides APIs for:
- Getting provider health status
- Manual enable/disable controls
- Circuit breaker configuration
"""

from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.app.main import require_admin
from api.app.monitoring.health import ProviderHealthMonitor


router = APIRouter(prefix="/admin/providers", tags=["Provider Health"], dependencies=[Depends(require_admin)])


class ProviderStatusResponse(BaseModel):
    provider_statuses: Dict[str, Dict[str, Any]]
    total_providers: int
    healthy_count: int
    degraded_count: int
    circuit_open_count: int
    disabled_count: int


class ProviderActionRequest(BaseModel):
    provider_id: str


class ProviderActionResponse(BaseModel):
    success: bool
    provider_id: str
    new_status: str
    message: Optional[str] = None


@router.get("/health", response_model=ProviderStatusResponse)
async def get_provider_health_status(request: Request):
    """Get health status of all providers."""
    health_monitor: ProviderHealthMonitor = getattr(request.app.state, "health_monitor", None)

    if not health_monitor:
        raise HTTPException(status_code=503, detail="Health monitor not available")

    provider_statuses = await health_monitor.get_provider_statuses()

    # Calculate summary stats
    status_counts = {
        'healthy': 0,
        'degraded': 0,
        'circuit_open': 0,
        'disabled': 0
    }

    for status_info in provider_statuses.values():
        status = status_info.get('status', 'healthy')
        if status in status_counts:
            status_counts[status] += 1

    return ProviderStatusResponse(
        provider_statuses=provider_statuses,
        total_providers=len(provider_statuses),
        healthy_count=status_counts['healthy'],
        degraded_count=status_counts['degraded'],
        circuit_open_count=status_counts['circuit_open'],
        disabled_count=status_counts['disabled']
    )


@router.post("/enable", response_model=ProviderActionResponse)
async def enable_provider(request: Request, action_request: ProviderActionRequest):
    """Manually enable a provider (reset circuit breaker)."""
    health_monitor: ProviderHealthMonitor = getattr(request.app.state, "health_monitor", None)

    if not health_monitor:
        raise HTTPException(status_code=503, detail="Health monitor not available")

    try:
        await health_monitor.manual_enable_provider(action_request.provider_id)

        return ProviderActionResponse(
            success=True,
            provider_id=action_request.provider_id,
            new_status="healthy",
            message=f"Provider {action_request.provider_id} has been enabled and circuit breaker reset"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to enable provider: {str(e)}")


@router.post("/disable", response_model=ProviderActionResponse)
async def disable_provider(request: Request, action_request: ProviderActionRequest):
    """Manually disable a provider."""
    health_monitor: ProviderHealthMonitor = getattr(request.app.state, "health_monitor", None)

    if not health_monitor:
        raise HTTPException(status_code=503, detail="Health monitor not available")

    try:
        await health_monitor.manual_disable_provider(action_request.provider_id)

        return ProviderActionResponse(
            success=True,
            provider_id=action_request.provider_id,
            new_status="disabled",
            message=f"Provider {action_request.provider_id} has been manually disabled"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to disable provider: {str(e)}")


@router.get("/circuit-breaker/{provider_id}/should-allow")
async def should_allow_requests(request: Request, provider_id: str):
    """Check if circuit breaker allows requests for a provider."""
    health_monitor: ProviderHealthMonitor = getattr(request.app.state, "health_monitor", None)

    if not health_monitor:
        return {"allowed": True, "reason": "Health monitor not available"}

    allowed = health_monitor.should_allow_request(provider_id)

    return {
        "provider_id": provider_id,
        "allowed": allowed,
        "reason": "Circuit breaker state" if not allowed else "Provider healthy"
    }


@router.post("/circuit-breaker/{provider_id}/record-result")
async def record_request_result(
    request: Request,
    provider_id: str,
    success: bool,
    error: Optional[str] = None
):
    """Record the result of a provider request for circuit breaker tracking."""
    health_monitor: ProviderHealthMonitor = getattr(request.app.state, "health_monitor", None)

    if not health_monitor:
        return {"recorded": False, "reason": "Health monitor not available"}

    health_monitor.record_request_result(provider_id, success, error)

    return {
        "provider_id": provider_id,
        "recorded": True,
        "success": success,
        "error": error
    }