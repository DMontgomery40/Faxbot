from __future__ import annotations

"""
User trait derivation helpers.

Current implementation derives a conservative trait set from legacy API-key
scopes so the Admin UI can gate features without changing server behavior.

Trait keys use a simple dotted namespace, e.g.:
  - role.admin
  - ui.terminal
  - ui.logs
  - ui.diagnostics
  - ui.plugins
  - ui.settings
  - ui.send
  - ui.jobs
  - ui.inbound
"""

from typing import Iterable, Set, Dict, Any


def traits_from_scopes(scopes: Iterable[str]) -> Set[str]:
    s = set(scopes or [])
    traits: Set[str] = set()

    if "*" in s or "keys:manage" in s:
        traits.update({
            "role.admin",
            "ui.terminal",
            "ui.logs",
            "ui.diagnostics",
            "ui.plugins",
            "ui.settings",
            "ui.send",
            "ui.jobs",
            "ui.inbound",
        })
        return traits

    # Read-only style access
    if "fax:read" in s:
        traits.update({"ui.jobs", "ui.inbound"})
    if "fax:send" in s:
        traits.add("ui.send")
    # Generic diagnostics/logs only for admins at this phase
    return traits


def pack_user_traits(user_id: str, scopes: Iterable[str]) -> Dict[str, Any]:
    traits = sorted(list(traits_from_scopes(scopes)))
    return {
        "schema_version": 1,
        "user": {"id": user_id},
        "traits": traits,
    }

