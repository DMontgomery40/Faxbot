from __future__ import annotations

import os
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException
from api.app.config import settings
from api.app.auth import verify_db_key


def require_admin(x_api_key: Optional[str] = Header(default=None)):
    """Minimal admin auth dependency for marketplace endpoints.
    - Accepts env bootstrap API key
    - Or a DB-backed key with scope keys:manage
    """
    if settings.api_key and x_api_key == settings.api_key:
        return {"admin": True, "key_id": "env"}
    info = verify_db_key(x_api_key)
    if not info or ("keys:manage" not in (info.get("scopes") or [])):
        raise HTTPException(401, detail="Admin authentication failed")
    return info


router = APIRouter(prefix="/admin/marketplace", tags=["AdminMarketplace"], dependencies=[Depends(require_admin)])


@router.get("/plugins")
def list_plugins():
    """List available plugins in the marketplace (disabled by default).

    Gate with ADMIN_MARKETPLACE_ENABLED=false by default to avoid exposing in prod
    until features are complete.
    """
    enabled = os.getenv("ADMIN_MARKETPLACE_ENABLED", "false").lower() in {"1", "true", "yes"}
    if not enabled:
        # Hide endpoint when disabled to avoid confusing operators
        raise HTTPException(404, detail="Not found")
    # Placeholder: will be populated via trait‑aware registry in later PRs
    return {"plugins": []}


@router.post("/install")
def install_plugin(payload: Dict[str, Any] | None = None):
    """Remote install a plugin (disabled by default).

    Gate with ADMIN_MARKETPLACE_REMOTE_INSTALL_ENABLED=false by default.
    """
    remote_enabled = os.getenv("ADMIN_MARKETPLACE_REMOTE_INSTALL_ENABLED", "false").lower() in {"1", "true", "yes"}
    if not remote_enabled:
      raise HTTPException(503, detail="Remote install disabled")
    # Placeholder only; actual implementation will be added later
    return {"ok": False, "message": "not_implemented"}
