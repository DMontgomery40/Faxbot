from __future__ import annotations

"""
Phase 2: Permission grammar and helpers (non-breaking)

Grammar: "{namespace}.{resource}:{action}", e.g., "admin.console:access".

This module maps legacy API-key scopes to canonical permissions and provides a
FastAPI dependency to guard routes by permission while preserving existing
API-key behavior. Session/user trait mapping will be added later.
"""

from typing import Iterable, List, Set, Optional, Dict
import re

_PERM_RE = re.compile(r"^[a-z0-9_.-]+\.[a-z0-9_.-]+:[a-z0-9_.-]+$")


def validate_permission_str(s: str) -> bool:
    return bool(_PERM_RE.match(s or ""))


def permissions_from_api_scopes(scopes: Iterable[str]) -> Set[str]:
    """Map legacy API-key scopes to canonical permissions.

    This is intentionally minimal and conservative. Extend as needed.
    """
    perms: Set[str] = set()
    sset = set(scopes or [])
    if "*" in sset:
        perms.add("*")
        # Grant common admin permissions under wildcard
        perms.update({
            "admin.console:access",
            "fax.jobs:send",
            "fax.jobs:read",
            "fax.inbound:read",
        })
        return perms

    if "keys:manage" in sset:
        perms.add("admin.console:access")
    if "fax:send" in sset:
        perms.add("fax.jobs:send")
    if "fax:read" in sset:
        perms.update({"fax.jobs:read", "fax.inbound:read"})
    return perms


def permissions_to_legacy_scopes(perms: Iterable[str]) -> List[str]:
    """Map canonical permissions back to legacy scopes (best-effort)."""
    out: Set[str] = set()
    pset = set(perms or [])
    if "*" in pset:
        out.add("*")
    if "admin.console:access" in pset:
        out.add("keys:manage")
    if "fax.jobs:send" in pset:
        out.add("fax:send")
    if "fax.jobs:read" in pset or "fax.inbound:read" in pset:
        out.add("fax:read")
    return sorted(out)


def require_permissions(required: List[str]):  # FastAPI dependency factory
    """Ensure the caller has the required canonical permissions.

    Current implementation maps API-key scopes → permissions.
    Session/user trait mapping can be layered in later without breaking callers.
    """
    # Local import to avoid hard-coupling on app startup
    from fastapi import Depends, HTTPException
    from ..main import require_api_key  # type: ignore

    # Validate at import-time in dev to catch mistakes early
    for p in required:
        if not validate_permission_str(p):
            raise ValueError(f"Invalid permission string: {p}")

    async def _dep(info = Depends(require_api_key)):
        # Derive permissions from legacy scopes (if any)
        scopes = (info or {}).get("scopes") or []
        perms = permissions_from_api_scopes(scopes)
        missing = [p for p in required if ("*" not in perms and p not in perms)]
        if missing:
            raise HTTPException(status_code=403, detail=f"Missing permission(s): {','.join(missing)}")
        return True

    return _dep

