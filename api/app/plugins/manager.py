"""Plugin Manager (safe skeleton, feature-gated).

This module provides a minimal, non-invasive manager that can resolve an
outbound provider and expose a thin adapter over existing services. It is
NOT imported by the runtime unless you wire it in behind FEATURE_V3_PLUGINS.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, Any

from ..config import settings
from ..phaxio_service import get_phaxio_service
from ..sinch_service import get_sinch_service

try:
    # Optional config store
    from .config_store import read_config as _read_cfg  # type: ignore
except Exception:  # pragma: no cover
    _read_cfg = None  # type: ignore


@dataclass
class OutboundAdapter:
    """Thin adapter interface over existing services."""
    id: str

    async def send(self, to_number: str, file_path: str, *, job_id: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError

    async def get_status(self, job_id: str, *, provider_sid: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError


class PhaxioAdapter(OutboundAdapter):
    def __init__(self) -> None:
        super().__init__(id="phaxio")

    async def send(self, to_number: str, file_url: str, *, job_id: Optional[str] = None) -> Dict[str, Any]:
        svc = get_phaxio_service()
        if not svc:
            raise RuntimeError("Phaxio not configured")
        if not job_id:
            raise ValueError("job_id required for Phaxio send (callback correlation)")
        return await svc.send_fax(to_number, file_url, job_id)

    async def get_status(self, job_id: str, *, provider_sid: Optional[str] = None) -> Dict[str, Any]:
        svc = get_phaxio_service()
        if not svc:
            raise RuntimeError("Phaxio not configured")
        if not provider_sid:
            raise ValueError("provider_sid required for Phaxio status")
        return await svc.get_fax_status(provider_sid)


class SinchAdapter(OutboundAdapter):
    def __init__(self) -> None:
        super().__init__(id="sinch")

    async def send(self, to_number: str, file_path: str, *, job_id: Optional[str] = None) -> Dict[str, Any]:
        svc = get_sinch_service()
        if not svc:
            raise RuntimeError("Sinch not configured")
        # Prefer direct file send (multipart)
        return await svc.send_fax_file(to_number, file_path)

    async def get_status(self, job_id: str, *, provider_sid: Optional[str] = None) -> Dict[str, Any]:
        svc = get_sinch_service()
        if not svc:
            raise RuntimeError("Sinch not configured")
        if not provider_sid:
            raise ValueError("provider_sid required for Sinch status")
        return await svc.get_fax_status(str(provider_sid))


class PluginManager:
    """Safe, minimal manager that resolves outbound provider.

    Resolution precedence:
    - When FEATURE_V3_PLUGINS is enabled and a valid config file exists,
      use providers.outbound.plugin if enabled.
    - Otherwise, fallback to env-based settings.fax_backend.
    """

    def __init__(self) -> None:
        self._outbound: Optional[str] = None

    def resolve_outbound(self) -> str:
        # Try config file only when feature is enabled
        if settings.feature_v3_plugins and _read_cfg is not None:
            res = _read_cfg(settings.faxbot_config_path)
            if getattr(res, "ok", False) and getattr(res, "data", None):
                data = res.data or {}
                ob = ((data.get("providers") or {}).get("outbound") or {})
                if ob.get("enabled") and ob.get("plugin") in {"phaxio", "sinch", "sip"}:
                    self._outbound = str(ob.get("plugin"))
        # Fallback to env
        if not self._outbound:
            self._outbound = settings.fax_backend
        return self._outbound

    def get_outbound_adapter(self) -> OutboundAdapter:
        pid = (self._outbound or self.resolve_outbound()).lower()
        if pid == "phaxio":
            return PhaxioAdapter()
        if pid == "sinch":
            return SinchAdapter()
        # SIP adapter intentionally not implemented in the safe skeleton
        raise NotImplementedError("SIP/Asterisk adapter not available in skeleton manager")


_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager

