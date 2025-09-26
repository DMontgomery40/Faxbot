from __future__ import annotations

"""
Typed plugin bases for Phase 1.

These classes provide a stable type surface for manager validation without
changing existing scaffolding under api/app/plugins/base/*.

- FaxbotPlugin: umbrella base (aliases existing FaxPlugin)
- TransportPlugin: transport-specific base marker

No behavior change; shims may optionally inherit these bases. The plugin
manager validates imported classes against these when available.
"""

from abc import ABC
from typing import Any, Dict

try:
    # Reuse existing interface to avoid duplication
    from .base.interface import FaxPlugin as _FaxPlugin  # type: ignore
except Exception:  # pragma: no cover
    class _FaxPlugin(ABC):  # minimal fallback
        def __init__(self) -> None:
            ...


class FaxbotPlugin(_FaxPlugin):  # type: ignore[misc]
    """Unified Faxbot plugin base (alias of existing FaxPlugin)."""

    # Optional: each plugin may declare its scope; manager defaults to 'global'
    scope: str = "global"  # global|tenant|user (Phase 3 will enforce)


class TransportPlugin(FaxbotPlugin):
    """Transport plugins must implement send/get_status semantics."""

    plugin_type: str = "transport"

    # Hints only — concrete methods are provided by FaxPlugin
    async def initialize(self, config: Dict[str, Any]) -> None:  # optional
        return None

