"""Plugin dependency object (feature-gated scaffolding).

Not imported by runtime unless FEATURE_V3_PLUGINS is enabled.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Any, Optional
import logging
from pathlib import Path


@dataclass
class PluginDeps:
    """Dependencies injected into plugins (no PHI in logs)."""

    # Core
    logger: logging.Logger
    db: Callable[..., Any]  # factory returning DB session/context manager
    http: Any  # e.g., httpx.AsyncClient

    # Storage/config
    storage: Any
    config_dir: Path

    # Observability
    audit: Callable[[str, dict], None]
    events: Optional[Any] = None
    metrics: Optional[Any] = None
    cache: Optional[Any] = None

    # Security helpers
    encrypt: Optional[Callable[[bytes], bytes]] = None
    decrypt: Optional[Callable[[bytes], bytes]] = None

    def audit_event(self, event_type: str, **kwargs):
        safe_fields = ['plugin_id', 'job_id', 'status', 'backend', 'error_code']
        metadata = {k: v for k, v in kwargs.items() if k in safe_fields}
        try:
            self.audit(event_type, metadata)
        except Exception:
            pass

    def get_config_path(self, filename: str) -> Path:
        return self.config_dir / filename

    def log_info(self, message: str, **kwargs):
        self._log_safe(self.logger.info, message, **kwargs)

    def log_error(self, message: str, **kwargs):
        self._log_safe(self.logger.error, message, **kwargs)

    def _log_safe(self, fn, message: str, **kwargs):
        def looks_phi_key(k: str) -> bool:
            k = k.lower()
            return any(s in k for s in ['ssn', 'dob', 'name', 'address', 'to_number', 'from_number', 'content'])
        safe_kwargs = {k: v for k, v in kwargs.items() if not looks_phi_key(k)}
        try:
            fn(message, extra=safe_kwargs)
        except Exception:
            # Avoid raising from logs
            pass

