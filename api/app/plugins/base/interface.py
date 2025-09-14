"""Base plugin interfaces (feature-gated scaffolding)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

from .deps import PluginDeps
from .types import PluginManifest, SendResult, StatusResult


class FaxPlugin(ABC):
    """Base plugin interface for outbound fax providers."""

    def __init__(self) -> None:
        self.deps: Optional[PluginDeps] = None
        self.config: Dict[str, Any] = {}
        self.started: bool = False

    @abstractmethod
    def manifest(self) -> PluginManifest:  # no PHI
        ...

    @abstractmethod
    def validate_config(self, config: Dict[str, Any]) -> None:
        ...

    def start(self, config: Dict[str, Any], deps: PluginDeps) -> None:
        self.validate_config(config)
        self.config = config
        self.deps = deps
        self.started = True
        try:
            m = self.manifest()
            deps.audit_event("plugin_started", plugin_id=m.id, version=m.version)
        except Exception:
            pass

    def stop(self) -> None:
        if self.started and self.deps:
            try:
                m = self.manifest()
                self.deps.audit_event("plugin_stopped", plugin_id=m.id)
            except Exception:
                pass
        self.started = False
        self.deps = None
        self.config = {}

    @abstractmethod
    async def send(self, to_number: str, file_path: str, options: Dict[str, Any] = {}) -> SendResult:
        ...

    @abstractmethod
    async def get_status(self, job_id: str) -> StatusResult:
        ...

    async def handle_webhook(self, headers: Dict[str, str], body: bytes) -> Dict[str, Any]:
        raise NotImplementedError

    def get_capabilities(self) -> Dict[str, bool]:
        m = self.manifest()
        caps = set(m.capabilities or [])
        return {
            'can_send': 'send' in caps,
            'can_get_status': 'get_status' in caps,
            'supports_webhooks': 'webhook' in caps,
            'supports_inbound': 'receive' in caps,
        }

