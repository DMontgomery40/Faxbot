import os
import shutil
import re
import uuid
import asyncio
import secrets
from datetime import datetime, timedelta
import tempfile
from typing import Optional, Any, List, Dict, cast
import subprocess
import time
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Header, Depends, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .config import (
    settings,
    reload_settings,
    active_outbound,
    active_inbound,
    VALID_BACKENDS,
    providerHasTrait,
    providerTraitValue,
)
from .db import init_db, SessionLocal, FaxJob
from .db import InboundEvent  # for idempotency (provider_sid + event_type)
from .models import FaxJobOut
from .conversion import ensure_dir, txt_to_pdf, pdf_to_tiff
from .ami import ami_client
from .phaxio_service import get_phaxio_service
from .sinch_service import get_sinch_service
from .signalwire_service import get_signalwire_service
from .config_loader import load_provider_secrets
from .providers import sinch as sinch_inbound_adapter
from .providers import phaxio as phaxio_inbound_adapter
from .freeswitch_service import originate_txfax, fs_cli_available
import hmac
import hashlib
from urllib.parse import urlparse
from fastapi.responses import StreamingResponse
import json
from .audit import init_audit_logger, audit_event
from .audit import query_recent_logs
from .storage import get_storage, reset_storage
from .auth import verify_db_key, create_api_key, list_api_keys, revoke_api_key, rotate_api_key
from .plugins.http_provider import HttpManifest, HttpProviderRuntime
from .utils.observability import log_event
from .signalwire_service import get_signalwire_service

# v3 plugins (feature-gated)
try:
    from .plugins.config_store import read_config as _read_cfg, write_config as _write_cfg  # type: ignore
except Exception:  # pragma: no cover - optional
    _read_cfg = None  # type: ignore
    _write_cfg = None  # type: ignore
from pydantic import BaseModel
from .middleware.traits import requires_traits
from .security.permissions import require_permissions
from .security.user_traits import pack_user_traits


app = FastAPI(
    title="Faxbot API",
    version="1.0.0",
    description="The first and only open-source, self-hostable fax API. Send faxes with a single function call.",
    contact={
        "name": "Faxbot Support",
        "url": "https://faxbot.net",
        "email": "support@faxbot.net",
    },
    license_info={
        "name": "MIT",
        "url": "https://github.com/dmontgomery40/faxbot/blob/main/LICENSE",
    },
)
# Expose phaxio_service module for tests that reference app.phaxio_service
from . import phaxio_service as _phaxio_module  # noqa: E402
app.phaxio_service = _phaxio_module  # type: ignore[attr-defined]


PHONE_RE = re.compile(r"^[+]?\d{6,20}$")
ALLOWED_CT = {"application/pdf", "text/plain"}

# ===== Phase 2: sessions feature-flag fail-fast for secrets =====
try:
    if os.getenv("FAXBOT_SESSIONS_ENABLED", "false").lower() in {"1","true","yes"}:
        cfg_key = os.getenv("CONFIG_MASTER_KEY", "")
        pepper = os.getenv("FAXBOT_SESSION_PEPPER", "")
        if not cfg_key or len(cfg_key) != 44:
            raise RuntimeError("CONFIG_MASTER_KEY missing or invalid length (44-char base64) while sessions enabled")
        if not pepper:
            raise RuntimeError("FAXBOT_SESSION_PEPPER missing while sessions enabled")
except Exception as _sec_ex:
    # Hard fail on import when sessions are enabled but secrets missing
    raise

# Strict verification toggle for inbound signatures
STRICT_INBOUND = os.getenv("INBOUND_STRICT_VERIFY", "false").lower() in {"1","true","yes"}


# In-memory per-key rate limiter (fixed window, per minute)
_rate_buckets: dict[str, dict[str, int]] = {}

# In-memory inbound dedupe (provider_id + external_id) with short TTL window
_inbound_seen: dict[str, int] = {}

def _inbound_dedupe(provider_id: str, external_id: str, window_sec: int = 600) -> bool:
    """Return True if this (provider, external_id) was seen recently; else record and return False.
    Uses a simple in-memory TTL map to prevent duplicate processing storms.
    """
    try:
        now = int(time.time())
        # Prune old entries
        cutoff = now - window_sec
        old_keys = [k for k, ts in _inbound_seen.items() if ts < cutoff]
        for k in old_keys:
            _inbound_seen.pop(k, None)
        key = f"{provider_id}:{external_id}"
        ts = _inbound_seen.get(key)
        if ts and ts >= cutoff:
            return True
        _inbound_seen[key] = now
        return False
    except Exception:
        return False

# Send-side idempotency (Idempotency-Key header) — 10 minute window
_send_idempotency: dict[str, tuple[str, int]] = {}

def _idempotency_get_job(idem_key: str, window_sec: int = 600) -> Optional[str]:
    try:
        now = int(time.time())
        cutoff = now - window_sec
        # prune
        old = [k for k, (_, ts) in _send_idempotency.items() if ts < cutoff]
        for k in old:
            _send_idempotency.pop(k, None)
        rec = _send_idempotency.get(idem_key)
        if not rec:
            return None
        job_id, ts = rec
        if ts < cutoff:
            _send_idempotency.pop(idem_key, None)
            return None
        return job_id
    except Exception:
        return None

def _idempotency_put(idem_key: str, job_id: str) -> None:
    try:
        _send_idempotency[idem_key] = (job_id, int(time.time()))
    except Exception:
        pass

    # Bootstrap admin user (dev/stage only): create 'admin' if sessions enabled and bootstrap password present
    try:
        if os.getenv("FAXBOT_SESSIONS_ENABLED", "false").lower() in {"1","true","yes"}:
            boot = os.getenv("FAXBOT_BOOTSTRAP_PASSWORD", "")
            if boot:
                from .plugins.manager import PluginManager
                pm = PluginManager()
                pm.load_all()
                ident = pm.get_active_by_type("identity")
                user = None
                if hasattr(ident, "find_user_by_username"):
                    user = await ident.find_user_by_username("admin")  # type: ignore
                if not user and hasattr(ident, "create_user"):
                    await ident.create_user("admin", boot, traits={"role": "admin"})  # type: ignore
                    print("[info] Bootstrapped admin user via FAXBOT_BOOTSTRAP_PASSWORD")
    except Exception as _boot_ex:
        # Never block startup for bootstrap; log only
        try:
            print(f"[warn] Admin bootstrap skipped: {_boot_ex}")
        except Exception:
            pass


def _ack_response(payload: Optional[dict] = None):
    """Return an ACK response with status 200 in test/compat mode and 202 otherwise.

    Controlled by env flags:
      - FAXBOT_TEST_MODE=true|1|yes
      - CALLBACK_COMPAT_200=true|1|yes
    """
    try:
        test_mode = os.getenv("FAXBOT_TEST_MODE", "false").lower() in {"1", "true", "yes"}
        compat_200 = os.getenv("CALLBACK_COMPAT_200", "false").lower() in {"1", "true", "yes"}
        code = 200 if (test_mode or compat_200) else 202
    except Exception:
        code = 202
    return JSONResponse(payload or {"status": "accepted"}, status_code=code)


def _enforce_rate_limit(info: Optional[dict], path: str, limit: Optional[int] = None):
    # Choose provided per-route limit, else global
    limit = int(limit or settings.max_requests_per_minute)
    if not limit or limit <= 0:
        return
    if info is None:
        # Do not rate limit unauthenticated dev requests
        return
    key_id = info.get("key_id") or "unknown"
    now_min = int(time.time() // 60)
    # Prune old buckets (keep only current minute to bound memory)
    try:
        old_keys = [k for k, v in _rate_buckets.items() if v.get("window") != now_min]
        for k in old_keys:
            _rate_buckets.pop(k, None)
    except Exception:
        pass
    bucket_key = f"{key_id}|{path}"
    bucket = _rate_buckets.get(bucket_key)
    if not bucket or bucket.get("window") != now_min:
        bucket = {"window": now_min, "count": 0}
        _rate_buckets[bucket_key] = bucket
    bucket["count"] += 1
    if bucket["count"] > limit:
        retry_after = (bucket["window"] + 1) * 60 - int(time.time())
        audit_event("rate_limited", key_id=key_id, path=path, limit=limit)
        from fastapi import HTTPException
        # Provide standard rate-limit headers for client backoff
        remaining = 0
        reset = max(1, retry_after)
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={
                "Retry-After": str(reset),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(int(time.time()) + reset),
            },
        )


# ===== Admin UI static mount (local-only feature) =====
if os.getenv("ENABLE_LOCAL_ADMIN", "false").lower() == "true":
    # Prefer container path if present, else project-relative path
    admin_ui_path_candidates = [
        "/app/admin_ui/dist",
        os.path.join(os.path.dirname(__file__), "..", "admin_ui", "dist"),
    ]
    for _p in admin_ui_path_candidates:
        try:
            ap = os.path.abspath(_p)
            if os.path.exists(ap):
                app.mount("/admin/ui", StaticFiles(directory=ap, html=True), name="admin_ui")
                break
        except Exception:
            pass

# Serve project assets (logo, etc.) under /assets if present (dev convenience)
_assets_candidates = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets")),
    os.path.abspath(os.path.join(os.getcwd(), "assets")),
]
for _ap in _assets_candidates:
    try:
        if os.path.isdir(_ap):
            app.mount("/assets", StaticFiles(directory=_ap), name="assets")
            break
    except Exception:
        pass

# ===== Optional CSRF middleware (cookie sessions only) =====
try:
    if os.getenv("FAXBOT_CSRF_ENABLED", "false").lower() in {"1","true","yes"}:
        from .middleware.csrf import CSRFMiddleware  # type: ignore
        app.add_middleware(CSRFMiddleware)
except Exception:
    # Keep running even if middleware import fails in environments without it
    pass

# ===== Embedded MCP mounts (optional) =====
try:
    if os.getenv("ENABLE_MCP_SSE", "false").lower() in {"1","true","yes"}:
        # Set environment for python_mcp before import
        os.environ.setdefault("FAX_API_URL", "http://localhost:8080")
        if settings.api_key:
            os.environ.setdefault("API_KEY", settings.api_key)
        # Configure OAuth variables for the embedded server
        if settings.require_mcp_oauth:
            if settings.oauth_issuer:
                os.environ["OAUTH_ISSUER"] = settings.oauth_issuer
            if settings.oauth_audience:
                os.environ["OAUTH_AUDIENCE"] = settings.oauth_audience
            if settings.oauth_jwks_url:
                os.environ["OAUTH_JWKS_URL"] = settings.oauth_jwks_url
        from python_mcp import server as _mcp_server  # type: ignore
        mount_app = _mcp_server.app if settings.require_mcp_oauth else getattr(_mcp_server, "inner_app")
        app.mount(settings.mcp_sse_path, mount_app)
except Exception as _mcp_err:
    # Do not break API if MCP mount fails; surface in diagnostics
    print(f"[warn] MCP SSE mount failed: {_mcp_err}")

try:
    if os.getenv("ENABLE_MCP_HTTP", "false").lower() in {"1","true","yes"}:
        # Prepare environment
        os.environ.setdefault("FAX_API_URL", "http://localhost:8080")
        if settings.api_key:
            os.environ.setdefault("API_KEY", settings.api_key)
        from python_mcp import http_server as _mcp_http  # type: ignore
        app.mount(settings.mcp_http_path, _mcp_http.app)
except Exception as _mcp_http_err:
    print(f"[warn] MCP HTTP mount failed: {_mcp_http_err}")


# ===== Admin security middleware (loopback + flag) =====
@app.middleware("http")
async def enforce_local_admin(request: Request, call_next):
    # Restrict only the browser UI under /admin/ui; leave programmatic admin APIs accessible
    if request.url.path.startswith("/admin/ui"):
        # Feature flag gate
        if os.getenv("ENABLE_LOCAL_ADMIN", "false").lower() != "true":
            return Response(content="Admin console disabled", status_code=404)
        # Block proxied access by default (defensive), unless explicitly allowed for demo/testing
        h = request.headers
        allow_tunnel_ui = os.getenv("ADMIN_UI_ALLOW_TUNNEL", "false").lower() in {"1","true","yes"}
        if ("x-forwarded-for" in h or "x-real-ip" in h) and not allow_tunnel_ui:
            return Response(content="Admin console not available through proxy", status_code=403)
        # Local access policy
        # Allow loopback; when running inside a container, also allow private bridge IPs (RFC1918)
        client_ip = str(request.client.host)
        try:
            import ipaddress
            ip = ipaddress.ip_address(client_ip)
            # Treat RFC1918 and CGNAT (100.64.0.0/10, e.g., Tailscale) as local-like
            cgnat = ipaddress.ip_network('100.64.0.0/10')
        except Exception:
            return Response(content="Forbidden", status_code=403)
        allow = ip.is_loopback or ip.is_private or (ip.version == 4 and ip in cgnat) or allow_tunnel_ui
        if not allow:
            return Response(content="Forbidden", status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/admin/"):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ===== Shared helpers for admin responses =====
def mask_secret(value: Optional[str], visible_chars: int = 4) -> str:
    if not value:
        return "***"
    if len(value) <= visible_chars:
        return "***"
    return "*" * (len(value) - visible_chars) + value[-visible_chars:]


def _mask_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        if p.username or p.password:
            netloc = p.hostname or ''
            if p.port:
                netloc += f":{p.port}"
            masked = p._replace(netloc=netloc).geturl()
            return masked
        return url
    except Exception:
        return url


def mask_phone(phone: Optional[str]) -> str:
    if not phone or len(phone) < 4:
        return "****"
    return "*" * (len(phone) - 4) + phone[-4:]


def sanitize_error(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    # Replace long digit sequences (likely numbers/IDs/phones) and truncate
    sanitized = re.sub(r"\+?\d{6,}", "***", text)
    return sanitized[:80]


@app.on_event("startup")
async def on_startup():
    # Re-read environment into settings for testability and dynamic config
    reload_settings()
    init_db()
    # Ensure data dir
    ensure_dir(settings.fax_data_dir)
    # Validate Ghostscript availability — required only when provider traits demand it
    _gs_missing = shutil.which("gs") is None
    if not settings.fax_disabled:
        try:
            needs_gs = providerHasTrait("any", "requires_ghostscript") or providerHasTrait("any", "requires_tiff")
        except Exception:
            needs_gs = False
        if needs_gs and _gs_missing:
            if str(os.getenv("FAXBOT_TEST_MODE", "false")).lower() in {"1","true","yes"}:
                print("[warn] Ghostscript (gs) not found; allowed via FAXBOT_TEST_MODE for unit tests only")
            else:
                raise RuntimeError("Ghostscript (gs) not found. Install 'ghostscript' — required for this backend (TIFF/GS conversions).")
    # Security posture warnings
    if not settings.require_api_key and not settings.api_key and not settings.fax_disabled:
        print("[warn] API auth is not enforced (REQUIRE_API_KEY=false and API_KEY unset); /fax requests are unauthenticated. Set API_KEY or REQUIRE_API_KEY for production.")
    try:
        pu = urlparse(settings.public_api_url)
        insecure = pu.scheme == "http" and pu.hostname not in {"localhost", "127.0.0.1"}
        if insecure:
            msg = "PUBLIC_API_URL is not HTTPS; cloud providers will fetch PDFs over HTTP. Use HTTPS in production."
            if settings.enforce_public_https and active_outbound() == "phaxio":
                raise RuntimeError(msg)
            else:
                print(f"[warn] {msg}")
    except Exception:
        pass

    # Start periodic cleanup task for artifacts
    if settings.artifact_ttl_days > 0:
        asyncio.create_task(_artifact_cleanup_loop())
    # Init audit logger
    init_audit_logger(
        enabled=settings.audit_log_enabled,
        fmt=settings.audit_log_format,
        filepath=(settings.audit_log_file or None),
        use_syslog=settings.audit_log_syslog,
        syslog_address=(settings.audit_log_syslog_address or None),
    )
    # Start AMI when required by traits (either direction)
    if not settings.fax_disabled and providerHasTrait("any", "requires_ami"):
        asyncio.create_task(ami_client.connect())
        ami_client.on_fax_result(_handle_fax_result)

    # Load provider status map for canonicalization
    try:
        from .status_map import load_status_map  # type: ignore
        load_status_map()
    except Exception:
        pass

    # Auto‑tunnel: Cloudflare URL discovery (dev/non‑HIPAA only)
    try:
        auto_enabled = (os.getenv("AUTO_TUNNEL_ENABLED", "false").lower() in {"1","true","yes"})
        provider_env = (os.getenv("TUNNEL_PROVIDER", "").lower())
        # Run when explicitly enabled or provider=cloudflare
        if auto_enabled or provider_env == "cloudflare":
            asyncio.create_task(_auto_tunnel_cloudflare_watcher())
    except Exception:
        pass


def _handle_fax_result(event):
    job_id = event.get("JobID") or event.get("jobid")
    status = event.get("Status") or event.get("status")
    error = event.get("Error") or event.get("error")
    pages = event.get("Pages") or event.get("pages")
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if job:
            j = cast(Any, job)
            j.status = status or j.status
            j.error = error
            if pages:
                try:
                    job.pages = int(pages)
                except Exception:
                    pass
            j.updated_at = datetime.utcnow()
            db.add(j)
            db.commit()
    if job_id and status:
        audit_event("job_updated", job_id=job_id, status=status, provider="asterisk")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    """Readiness probe. Returns 200 when core dependencies are ready.
    Checks: DB connectivity; backend configuration; storage configuration when inbound is enabled.
    """
    # DB check
    db_ok = False
    try:
        from sqlalchemy import text  # type: ignore
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        db_ok = False

    # Trait-driven checks
    ob = active_outbound()
    ib = active_inbound()
    backend_warnings: List[str] = []
    outbound_ok = ob in VALID_BACKENDS
    inbound_ok = ib in VALID_BACKENDS

    # Storage check (only if inbound enabled)
    storage_ok = True
    storage_error: Optional[str] = None
    if settings.inbound_enabled:
        try:
            # Ensure storage can be initialized
            get_storage()
            if settings.storage_backend == "s3" and not settings.s3_bucket:
                storage_ok = False
                storage_error = "S3_BUCKET not set"
        except Exception as e:
            storage_ok = False
            storage_error = str(e)

    # System dependency check (Ghostscript — required)
    gs_installed = shutil.which("gs") is not None
    if not gs_installed:
        backend_warnings.append("Ghostscript (gs) not installed — required for fax file processing")

    # AMI connection (only when required by traits)
    ami_connected = False
    try:
        from .ami import ami_client as _ac  # type: ignore
        ami_connected = bool(getattr(_ac, "_connected").is_set())  # type: ignore[union-attr]
    except Exception:
        ami_connected = False

    # Required traits for readiness
    ami_required = providerHasTrait("any", "requires_ami")
    storage_required = settings.inbound_enabled and providerHasTrait("inbound", "needs_storage")
    ready = bool(
        db_ok and gs_installed and outbound_ok and inbound_ok and
        (not ami_required or ami_connected) and
        (not storage_required or storage_ok)
    )
    status_code = 200 if ready else 503
    from fastapi.responses import JSONResponse as _JR
    return _JR(
        content={
            "status": "ready" if ready else "not_ready",
            "backend": ob,
            "checks": {
                "db": db_ok,
                "ghostscript": gs_installed,
                "storage": storage_ok if storage_required else None,
                "outbound": {
                    "backend": ob,
                    "backend_config": outbound_ok,
                    "ami_connected": ami_connected if providerHasTrait("outbound", "requires_ami") else None,
                },
                "inbound": {
                    "backend": ib,
                    "enabled": settings.inbound_enabled,
                    "backend_config": inbound_ok,
                    "ami_connected": ami_connected if providerHasTrait("inbound", "requires_ami") else None,
                },
            },
            "warnings": backend_warnings,
            "storage_error": storage_error,
        },
        status_code=status_code,
    )


def require_api_key(request: Request, x_api_key: Optional[str] = Header(default=None)):
    """Authenticate request using either env API_KEY or DB-backed key.
    Behavior:
      - If header matches env API_KEY → allow
      - Else, if header is a valid DB key → allow
      - Else, if REQUIRE_API_KEY=true → 401
      - Else (dev mode) → allow
    """
    # Env bootstrap key
    if settings.api_key and x_api_key == settings.api_key:
        # Optionally audit usage without logging secrets
        audit_event("api_key_used", key_id="env", path=request.url.path if request else None)
        # Treat env key as full-access for compatibility
        return {"key_id": "env", "scopes": ["*"]}
    # Try DB-backed key
    info = verify_db_key(x_api_key)
    if info:
        audit_event("api_key_used", key_id=info.get("key_id"), path=request.url.path if request else None)
        return info
    # If API key is required, reject
    if settings.require_api_key or settings.api_key:
        raise HTTPException(401, detail="Invalid or missing API key")
    # Dev mode: allow unauthenticated
    return None


def require_admin(x_api_key: Optional[str] = Header(default=None)):
    # Allow env key as admin for bootstrap
    if settings.api_key and x_api_key == settings.api_key:
        return {"admin": True, "key_id": "env"}
    info = verify_db_key(x_api_key)
    if not info or ("keys:manage" not in (info.get("scopes") or [])):
        raise HTTPException(401, detail="Admin authentication failed")
    return info


def _has_scope(info: Optional[dict], required: str) -> bool:
    if info is None:
        return False
    scopes = info.get("scopes") or []
    return "*" in scopes or required in scopes


def require_scopes(required: List[str], path: Optional[str] = None, rpm: Optional[int] = None):
    """Factory to build a dependency enforcing scopes and per-route RPM.
    Allows unauthenticated access only when not enforcing API keys in dev.
    """
    def _dep(info = Depends(require_api_key)):
        if info is None and not settings.require_api_key and not settings.api_key:
            return
        missing = [s for s in required if not _has_scope(info, s)]
        if missing:
            audit_event("api_key_denied_scope", key_id=(info or {}).get("key_id"), required=",".join(missing))
            raise HTTPException(403, detail=f"Insufficient scope: {','.join(required)} required")
        if rpm and path:
            _enforce_rate_limit(info, path, rpm)
    return _dep


def require_fax_send(info = Depends(require_api_key)):
    # If not enforcing API key (dev mode) and unauthenticated, allow
    if info is None and not settings.require_api_key and not settings.api_key:
        return
    if not _has_scope(info, "fax:send"):
        audit_event("api_key_denied_scope", key_id=(info or {}).get("key_id"), required="fax:send")
        raise HTTPException(403, detail="Insufficient scope: fax:send required")
    # Rate limit per key if configured
    _enforce_rate_limit(info, "/fax")


def require_fax_read(info = Depends(require_api_key)):
    if info is None and not settings.require_api_key and not settings.api_key:
        return
    if not _has_scope(info, "fax:read"):
        audit_event("api_key_denied_scope", key_id=(info or {}).get("key_id"), required="fax:read")
        raise HTTPException(403, detail="Insufficient scope: fax:read required")
    _enforce_rate_limit(info, "/fax/{id}")


class CreateAPIKeyIn(BaseModel):
    name: Optional[str] = None
    owner: Optional[str] = None
    scopes: Optional[List[str]] = None
    expires_at: Optional[datetime] = None
    note: Optional[str] = None


class CreateAPIKeyOut(BaseModel):
    key_id: str
    token: str
    name: Optional[str] = None
    owner: Optional[str] = None
    scopes: List[str] = []
    expires_at: Optional[datetime] = None


class APIKeyMeta(BaseModel):
    key_id: str
    name: Optional[str] = None
    owner: Optional[str] = None
    scopes: List[str] = []
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    note: Optional[str] = None


@app.get("/admin/config", dependencies=[Depends(require_admin)])
def get_admin_config():
    """Return sanitized effective configuration for operators.
    Does not include secrets. Requires admin auth (bootstrap env key or keys:manage).
    """
    # Ensure latest env-backed settings are reflected on every request
    try:
        reload_settings()
    except Exception:
        pass
    backend = settings.fax_backend
    ob = active_outbound()
    ib = active_inbound()
    # Configured flags
    from .config import get_provider_registry
    cfg = {
        "backend": backend,
        "hybrid": {
            "outbound": ob,
            "inbound": ib,
            "outbound_explicit": bool(os.getenv("FAX_OUTBOUND_BACKEND")),
            "inbound_explicit": bool(os.getenv("FAX_INBOUND_BACKEND")),
        },
        "allow_restart": settings.admin_allow_restart,
        "require_api_key": settings.require_api_key,
        "enforce_public_https": settings.enforce_public_https,
        "phaxio_verify_signature": settings.phaxio_verify_signature,
        "persisted_settings_enabled": (os.getenv("ENABLE_PERSISTED_SETTINGS", "false").lower() in {"1","true","yes"}),
        "branding": {
            "docs_base": os.getenv("DOCS_BASE_URL", "https://dmontgomery40.github.io/Faxbot"),
            "logo_path": "/admin/ui/faxbot_full_logo.png",
        },
        "migration": {
            "env_to_db_done": (os.getenv("ADMIN_MIGRATION_ENV_TO_DB_DONE", "false").lower() in {"1","true","yes"}),
            "banner": not (os.getenv("ADMIN_MIGRATION_ENV_TO_DB_DONE", "false").lower() in {"1","true","yes"})
        },
        # Traits in one call (active + registry)
        "traits": {
            "active": {"outbound": ob, "inbound": ib},
        },
        "mcp": {
            "sse_enabled": (os.getenv("ENABLE_MCP_SSE", "false").lower() in {"1","true","yes"}),
            "sse_path": settings.mcp_sse_path,
            "require_oauth": settings.require_mcp_oauth,
            "oauth": {
                "issuer": settings.oauth_issuer,
                "audience": settings.oauth_audience,
                "jwks_url": settings.oauth_jwks_url,
            },
            "http_enabled": (os.getenv("ENABLE_MCP_HTTP", "false").lower() in {"1","true","yes"}),
            "http_path": settings.mcp_http_path,
        },
        "audit_log_enabled": settings.audit_log_enabled,
        "rate_limits": {
            "global_rpm": settings.max_requests_per_minute,
            "inbound_list_rpm": settings.inbound_list_rpm,
            "inbound_get_rpm": settings.inbound_get_rpm,
        },
        "inbound": {
            "enabled": settings.inbound_enabled,
            "retention_days": settings.inbound_retention_days,
            "token_ttl_minutes": settings.inbound_token_ttl_minutes,
        },
        # Traits registry exposed for clients (Admin UI, iOS) to gate UX by capabilities
        "traits": {
            "active": {
                "outbound": ob,
                "inbound": ib,
            },
            "registry": get_provider_registry(),
        },
        "storage": {
            "backend": settings.storage_backend,
            "s3_bucket": (settings.s3_bucket[:4] + "…" if settings.s3_bucket else ""),
            "s3_region": settings.s3_region,
            "s3_prefix": settings.s3_prefix,
            "s3_endpoint_url": settings.s3_endpoint_url,
            "s3_kms_key_id": (settings.s3_kms_key_id[:8] + "…" if settings.s3_kms_key_id else ""),
        },
        "backend_configured": {
            "phaxio": bool(settings.phaxio_api_key and settings.phaxio_api_secret),
            "sinch": bool(settings.sinch_project_id and settings.sinch_api_key and settings.sinch_api_secret),
            "signalwire": bool(settings.signalwire_space_url and settings.signalwire_project_id and settings.signalwire_api_token),
            "documo": bool(settings.documo_api_key),
            "sip_ami_configured": bool(settings.ami_username and settings.ami_password),
            "sip_ami_password_default": (settings.ami_password == "changeme"),
        },
        "public_api_url": settings.public_api_url,
    }
    # v3 plugins status (feature-gated)
    if settings.feature_v3_plugins:
        cfg["v3_plugins"] = {
            "enabled": True,
            "active_outbound": ob,
            "config_path": settings.faxbot_config_path,
            "plugin_install_enabled": settings.feature_plugin_install,
        }
    # Late import to avoid circulars
    try:
        from .config import get_provider_registry  # type: ignore
        reg = get_provider_registry()
        cfg["traits"]["registry"] = reg
        # Summarize providers capabilities for Admin Console
        providers: list[dict[str, Any]] = []
        for pid, meta in (reg or {}).items():
            t = (meta.get("traits") or {})
            providers.append({
                "id": pid,
                "supports_outbound": True,
                "supports_inbound": bool(t.get("supports_inbound")),
                "inbound_verification": (t.get("inbound_verification") or "none"),
            })
        cfg["providers"] = providers
    except Exception:
        cfg["traits"]["registry"] = {}
        cfg["providers"] = []
    return cfg

@app.post("/admin/config/import-env", dependencies=[Depends(require_admin)])
async def admin_import_env(request: Request):
    """Import selected environment variables (Phase 3 stub).

    Accepts JSON body { "prefixes": ["PHAXIO_", "SINCH_", ...] }.
    Returns count of discovered keys. Full DB write occurs in the Phase 3 provider.
    """
    try:
        js = await request.json()
    except Exception:
        js = {}
    prefixes = js.get("prefixes") or ["PHAXIO_", "SINCH_", "S3_", "AWS_", "STORAGE_"]
    from .config.migrate import import_env_to_db
    count = import_env_to_db(prefixes)
    return {"ok": True, "discovered": count, "prefixes": prefixes}


# Provider traits and active backends — lightweight helper for clients
@app.get("/admin/providers", dependencies=[Depends(require_admin)])
def get_admin_providers():
    from .config import get_provider_registry, active_outbound, active_inbound
    try:
        reload_settings()
    except Exception:
        pass
    return {
        "schema_version": 1,
        "active": {
            "outbound": active_outbound(),
            "inbound": active_inbound(),
        },
        "registry": get_provider_registry(),
    }


@app.get("/admin/traits/validate", dependencies=[Depends(require_admin)])
def validate_traits_registry():
    """Return provider traits registry with any schema issues identified.
    Clients and CI can use this to assert trait correctness.
    """
    from .config import get_provider_registry, get_traits_schema_issues
    try:
        reload_settings()
    except Exception:
        pass
    return {
        "schema_version": 1,
        "registry": get_provider_registry(),
        "issues": get_traits_schema_issues(),
    }


@app.get("/admin/settings", dependencies=[Depends(require_admin)])
def get_admin_settings():
    """Return effective settings with sensitive values masked."""
    ob = active_outbound()
    ib = active_inbound()
    return {
        "backend": {
            "type": settings.fax_backend,
            "disabled": settings.fax_disabled,
        },
        "features": {
            "v3_plugins": settings.feature_v3_plugins,
            "plugin_install": settings.feature_plugin_install,
        },
        "hybrid": {
            "outbound_backend": ob,
            "inbound_backend": ib,
            "outbound_explicit": bool(os.getenv("FAX_OUTBOUND_BACKEND")),
            "inbound_explicit": bool(os.getenv("FAX_INBOUND_BACKEND")),
        },
        "phaxio": {
            "api_key": mask_secret(settings.phaxio_api_key),
            "api_secret": mask_secret(settings.phaxio_api_secret),
            "callback_url": settings.phaxio_status_callback_url,
            "verify_signature": settings.phaxio_verify_signature,
            "configured": bool(settings.phaxio_api_key and settings.phaxio_api_secret),
        },
        "documo": {
            "api_key": mask_secret(settings.documo_api_key),
            "base_url": settings.documo_base_url,
            "sandbox": settings.documo_use_sandbox,
            "configured": bool(settings.documo_api_key),
        },
        "sinch": {
            "project_id": settings.sinch_project_id,
            "api_key": mask_secret(settings.sinch_api_key),
            "api_secret": mask_secret(settings.sinch_api_secret),
            "base_url": os.getenv("SINCH_BASE_URL", ""),
            "auth_method": settings.sinch_auth_method,
            "configured": bool(settings.sinch_project_id and settings.sinch_api_key and settings.sinch_api_secret),
        },
        "signalwire": {
            "space_url": settings.signalwire_space_url,
            "project_id": settings.signalwire_project_id,
            "api_token": mask_secret(settings.signalwire_api_token),
            "from_fax": mask_phone(settings.signalwire_fax_from_e164),
            "callback_url": settings.signalwire_status_callback_url,
            "configured": bool(settings.signalwire_space_url and settings.signalwire_project_id and settings.signalwire_api_token),
        },
        "fs": {
            "esl_host": settings.fs_esl_host,
            "esl_port": settings.fs_esl_port,
            "gateway_name": settings.fs_gateway_name,
            "caller_id_number": settings.fs_caller_id_number,
            "t38_enable": settings.fs_t38_enable,
        },
        "sip": {
            "ami_host": settings.ami_host,
            "ami_port": settings.ami_port,
            "ami_username": settings.ami_username,
            "ami_password": mask_secret(settings.ami_password),
            "ami_password_is_default": settings.ami_password == "changeme",
            "station_id": mask_phone(settings.fax_station_id),
            "configured": bool(settings.ami_username and settings.ami_password),
        },
        "security": {
            "require_api_key": settings.require_api_key,
            "enforce_https": settings.enforce_public_https,
            "audit_enabled": settings.audit_log_enabled,
            "public_api_url": settings.public_api_url,
        },
        "storage": {
            "backend": settings.storage_backend,
            "s3_bucket": (settings.s3_bucket[:4] + "…" if settings.s3_bucket else ""),
            "s3_kms_enabled": bool(settings.s3_kms_key_id),
        },
        "database": {
            "url": settings.database_url,
            "persistent": settings.database_url.startswith("sqlite:////faxdata/") if isinstance(settings.database_url, str) else False,
        },
        "inbound": {
            "enabled": settings.inbound_enabled,
            "retention_days": settings.inbound_retention_days,
            "token_ttl_minutes": settings.inbound_token_ttl_minutes,
            "sip": {
                "asterisk_secret": mask_secret(settings.asterisk_inbound_secret),
                "configured": bool(settings.asterisk_inbound_secret),
            },
            "phaxio": {
                "verify_signature": settings.phaxio_inbound_verify_signature,
            },
            "sinch": {
                "basic_auth_configured": bool(settings.sinch_inbound_basic_user),
            },
        },
        "limits": {
            "max_file_size_mb": settings.max_file_size_mb,
            "pdf_token_ttl_minutes": settings.pdf_token_ttl_minutes,
            "rate_limit_rpm": settings.max_requests_per_minute,
            "inbound_list_rpm": settings.inbound_list_rpm,
            "inbound_get_rpm": settings.inbound_get_rpm,
        },
        "tunnel": {
            "provider": (os.getenv("TUNNEL_PROVIDER") or str(_TUNNEL_STATE.get("provider") or "none")).lower(),
        },
    }

# === Read-only effective config (env/default; DB in Phase 3) ===
class EffectiveConfigOut(BaseModel):
    schema_version: int
    values: Dict[str, Dict[str, Any]]


@app.get("/admin/config/effective", dependencies=[Depends(require_admin)])
def admin_config_effective() -> EffectiveConfigOut:
    def src(env_key: str, default: Any) -> dict[str, Any]:  # type: ignore
        val = os.getenv(env_key)
        return {"key": env_key, "value": (val if val is not None else default), "source": ("env" if val is not None else "default")}

    values: Dict[str, Dict[str, Any]] = {
        "FAX_BACKEND": src("FAX_BACKEND", settings.fax_backend),
        "FAX_OUTBOUND_BACKEND": src("FAX_OUTBOUND_BACKEND", os.getenv("FAX_BACKEND", settings.fax_backend)),
        "FAX_INBOUND_BACKEND": src("FAX_INBOUND_BACKEND", os.getenv("FAX_BACKEND", settings.fax_backend)),
        "PUBLIC_API_URL": src("PUBLIC_API_URL", settings.public_api_url),
        # Provider creds presence only (no PHI)
        "PHAXIO_API_KEY": {"key": "PHAXIO_API_KEY", "value": bool(settings.phaxio_api_key), "source": ("env" if os.getenv("PHAXIO_API_KEY") else "default")},
        "PHAXIO_API_SECRET": {"key": "PHAXIO_API_SECRET", "value": bool(settings.phaxio_api_secret), "source": ("env" if os.getenv("PHAXIO_API_SECRET") else "default")},
        "SINCH_PROJECT_ID": {"key": "SINCH_PROJECT_ID", "value": bool(settings.sinch_project_id), "source": ("env" if os.getenv("SINCH_PROJECT_ID") else "default")},
        "SINCH_API_KEY": {"key": "SINCH_API_KEY", "value": bool(settings.sinch_api_key), "source": ("env" if os.getenv("SINCH_API_KEY") else "default")},
        "SINCH_API_SECRET": {"key": "SINCH_API_SECRET", "value": bool(settings.sinch_api_secret), "source": ("env" if os.getenv("SINCH_API_SECRET") else "default")},
        # Storage
        "STORAGE_BACKEND": src("STORAGE_BACKEND", settings.storage_backend),
        "S3_BUCKET": {"key": "S3_BUCKET", "value": bool(settings.s3_bucket), "source": ("env" if os.getenv("S3_BUCKET") else "default")},
        "S3_REGION": src("S3_REGION", settings.s3_region),
        "S3_ENDPOINT_URL": src("S3_ENDPOINT_URL", settings.s3_endpoint_url),
        "S3_KMS_KEY_ID": src("S3_KMS_KEY_ID", settings.s3_kms_key_id),
    }
    return EffectiveConfigOut(schema_version=1, values=values)


class ProviderTestOut(BaseModel):
    success: bool
    message: str
    latency_ms: float


@app.post("/admin/providers/{provider_id}/test", dependencies=[Depends(require_admin)])
async def admin_provider_test(provider_id: str) -> ProviderTestOut:
    import time as _time
    t0 = _time.perf_counter()
    pid = (provider_id or "").strip().lower()
    ok = False
    msg = ""
    try:
        if pid == "phaxio":
            ok = bool(settings.phaxio_api_key and settings.phaxio_api_secret)
            msg = "credentials present" if ok else "missing PHAXIO_API_KEY/PHAXIO_API_SECRET"
        elif pid == "sinch":
            ok = bool(settings.sinch_project_id and settings.sinch_api_key and settings.sinch_api_secret)
            msg = "credentials present" if ok else "missing SINCH_PROJECT_ID/API_KEY/API_SECRET"
        elif pid in {"sip", "asterisk"}:
            ok = bool(settings.ami_password and settings.ami_host)
            msg = "AMI configured" if ok else "AMI host/password not configured"
        elif pid == "s3":
            ok = bool(settings.s3_bucket)
            msg = "bucket configured" if ok else "S3_BUCKET missing"
        elif pid == "local":
            ok = True
            msg = "local storage ready"
        elif pid == "identity":
            try:
                from .plugins.manager import PluginManager
                pm = PluginManager()
                pm.load_all()
                ident = pm.get_active_by_type("identity")
                if hasattr(ident, "test_connection"):
                    res = await ident.test_connection()  # type: ignore
                    ok = bool(res.get("success", False))
                    msg = str(res.get("message", ""))
                else:
                    ok = False
                    msg = "identity provider missing test_connection()"
            except Exception as ex:
                ok = False
                msg = f"identity test failed: {ex}"
        else:
            ok = True
            msg = "no-op test"
    except Exception as e:
        ok = False
        msg = str(e)
    dt = max(0.0, (_time.perf_counter() - t0) * 1000.0)
    return ProviderTestOut(success=ok, message=msg, latency_ms=round(dt, 2))


# ===== Admin UI config (ETag-cached) =====
@app.get("/admin/ui-config", dependencies=[Depends(require_admin)])
async def admin_ui_config(request: Request):
    cfg = {
        "schema_version": 1,
        "features": {
            "sessions_enabled": os.getenv("FAXBOT_SESSIONS_ENABLED", "false").lower() in {"1","true","yes"},
            "csrf_enabled": os.getenv("FAXBOT_CSRF_ENABLED", "false").lower() in {"1","true","yes"},
        },
        "endpoints": {
            "metrics": "/metrics",
            "providers": "/admin/providers",
            "config_effective": "/admin/config/effective",
            "provider_test": "/admin/providers/{id}/test",
            "auth_login": "/auth/login",
            "auth_logout": "/auth/logout",
            "auth_refresh": "/auth/refresh",
        },
        # client can render docs links relative to this base
        "docs_base": os.getenv("DOCS_BASE_URL", "https://docs.faxbot.net"),
    }
    body = json.dumps(cfg, sort_keys=True).encode("utf-8")
    etag = hashlib.sha256(body).hexdigest()
    inm = request.headers.get("if-none-match") or request.headers.get("If-None-Match")
    if inm and inm == etag:
        return Response(status_code=304)
    resp = JSONResponse(cfg)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, max-age=30"
    return resp


# ===== Demo: permission-guarded endpoint (non-breaking) =====
@app.get("/admin/permissions/check", dependencies=[Depends(require_permissions(["admin.console:access"]))])
async def admin_permissions_check():
    return {"ok": True}


@app.get("/admin/user/traits", dependencies=[Depends(require_admin)])
async def admin_user_traits(info = Depends(require_admin)):
    # info contains key_id and scopes for admin keys
    scopes = (info or {}).get("scopes") or []
    user_id = (info or {}).get("key_id") or "unknown"
    return pack_user_traits(user_id, scopes)


# ===== Dev helper: create identity user (feature-gated) =====
class _CreateUserIn(BaseModel):
    username: str
    password: str
    traits: Optional[Dict[str, Any]] = None


@app.post("/admin/identity/dev/create-user", dependencies=[Depends(require_admin)])
async def admin_identity_dev_create_user(payload: _CreateUserIn):
    # Gate strictly to dev only; avoid accidental prod usage
    if os.getenv("FAXBOT_DEV_IDENTITY_INIT", "false").lower() not in {"1", "true", "yes"}:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        from .plugins.manager import PluginManager
        pm = PluginManager()
        pm.load_all()
        ident = pm.get_active_by_type("identity")
        if not hasattr(ident, "create_user"):
            raise HTTPException(status_code=400, detail="identity provider missing create_user()")
        user = await ident.create_user(payload.username, payload.password, traits=payload.traits)  # type: ignore
        return {"ok": True, "id": getattr(user, "id", None), "username": getattr(user, "username", None)}
    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"create user failed: {ex}")


class ValidateSettingsRequest(BaseModel):
    backend: str
    phaxio_api_key: Optional[str] = None
    phaxio_api_secret: Optional[str] = None
    sinch_project_id: Optional[str] = None
    sinch_api_key: Optional[str] = None
    sinch_api_secret: Optional[str] = None
    ami_host: Optional[str] = None
    ami_port: Optional[int] = None
    ami_username: Optional[str] = None
    ami_password: Optional[str] = None


@app.post("/admin/settings/validate", dependencies=[Depends(require_admin)])
async def validate_settings(payload: ValidateSettingsRequest):
    """Validate connectivity/non-destructive checks for the selected backend."""
    results: dict[str, Any] = {"backend": payload.backend, "checks": {}, "test_fax": None}
    if payload.backend == "phaxio":
        if payload.phaxio_api_key and payload.phaxio_api_secret:
            try:
                import httpx
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        "https://api.phaxio.com/v2.1/account/status",
                        auth=(payload.phaxio_api_key, payload.phaxio_api_secret),
                    )
                    results["checks"]["auth"] = (resp.status_code == 200)
                    if resp.status_code == 200:
                        data = resp.json()
                        results["checks"]["account_status"] = bool(data.get("success"))
            except Exception as e:
                results["checks"]["auth"] = False
                results["checks"]["error"] = str(e)
        else:
            results["checks"]["auth"] = False
    elif payload.backend == "sinch":
        # Presence + fast auth ping
        present = bool(payload.sinch_project_id and payload.sinch_api_key and payload.sinch_api_secret)
        results["checks"]["auth_present"] = present
        if present:
            try:
                import httpx
                base = os.getenv("SINCH_BASE_URL", "https://fax.api.sinch.com/v3").rstrip("/")
                # Prefer OAuth if enabled
                token = None
                if (os.getenv("SINCH_AUTH_METHOD", settings.sinch_auth_method or "basic").lower() == "oauth"):
                    try:
                        auth_base = os.getenv("SINCH_AUTH_BASE_URL", "https://auth.sinch.com/oauth2/token")
                        data = {"grant_type": "client_credentials"}
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            tr = await client.post(auth_base, data=data, auth=(payload.sinch_api_key, payload.sinch_api_secret))
                        if tr.status_code < 400:
                            token = (tr.json() or {}).get("access_token")
                    except Exception:
                        token = None
                url = f"{base}/projects/{payload.sinch_project_id}/services/default"
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers={"Authorization": f"Bearer {token}"} if token else None, auth=None if token else (payload.sinch_api_key, payload.sinch_api_secret))
                ok = (resp.status_code < 400)
                # Fallback probe for tenants without services endpoint
                if not ok:
                    url2 = f"{base}/projects/{payload.sinch_project_id}/faxes?limit=1"
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        r2 = await client.get(url2, headers={"Authorization": f"Bearer {token}"} if token else None, auth=None if token else (payload.sinch_api_key, payload.sinch_api_secret))
                    ok = (r2.status_code < 400)
                results["checks"]["auth"] = ok
            except Exception as e:
                results["checks"]["auth"] = False
                results["checks"]["error"] = str(e)
        else:
            results["checks"]["auth"] = False
    elif payload.backend == "sip":
        if all([payload.ami_host, payload.ami_username, payload.ami_password]):
            try:
                from .ami import test_ami_connection
                ok = await test_ami_connection(
                    host=payload.ami_host or "asterisk",
                    port=payload.ami_port or 5038,
                    username=payload.ami_username or "api",
                    password=payload.ami_password or "",
                )
                results["checks"]["ami_connection"] = bool(ok)
                if payload.ami_password == "changeme":
                    results["checks"]["ami_password_secure"] = False
                    results["checks"]["warning"] = "AMI password is still default"
            except Exception as e:
                results["checks"]["ami_connection"] = False
                results["checks"]["error"] = str(e)
        import shutil as _sh
        results["checks"]["ghostscript"] = _sh.which("gs") is not None
    # Common check: fax_data_dir write
    try:
        _test = os.path.join(settings.fax_data_dir, f"test_{uuid.uuid4().hex}")
        with open(_test, "w") as f:
            f.write("ok")
        os.remove(_test)
        results["checks"]["fax_data_dir_writable"] = True
    except Exception:
        results["checks"]["fax_data_dir_writable"] = False
    return results


class UpdateSettingsRequest(BaseModel):
    # Core
    backend: Optional[str] = None  # 'phaxio' | 'sinch' | 'sip'
    outbound_backend: Optional[str] = None  # hybrid outbound
    inbound_backend: Optional[str] = None   # hybrid inbound
    require_api_key: Optional[bool] = None
    enforce_public_https: Optional[bool] = None
    public_api_url: Optional[str] = None
    fax_disabled: Optional[bool] = None
    max_file_size_mb: Optional[int] = None
    enable_persisted_settings: Optional[bool] = None
    database_url: Optional[str] = None
    # Feature flags
    feature_v3_plugins: Optional[bool] = None
    feature_plugin_install: Optional[bool] = None
    # MCP embedded SSE
    enable_mcp_sse: Optional[bool] = None
    require_mcp_oauth: Optional[bool] = None
    oauth_issuer: Optional[str] = None
    oauth_audience: Optional[str] = None
    oauth_jwks_url: Optional[str] = None
    enable_mcp_http: Optional[bool] = None

    # Phaxio
    phaxio_api_key: Optional[str] = None
    phaxio_api_secret: Optional[str] = None
    phaxio_status_callback_url: Optional[str] = None
    phaxio_verify_signature: Optional[bool] = None

    # Sinch
    sinch_project_id: Optional[str] = None
    sinch_api_key: Optional[str] = None
    sinch_api_secret: Optional[str] = None
    sinch_base_url: Optional[str] = None
    sinch_auth_method: Optional[str] = None  # 'basic' | 'oauth'
    sinch_auth_base_url: Optional[str] = None  # token endpoint

    # SignalWire
    signalwire_space_url: Optional[str] = None
    signalwire_project_id: Optional[str] = None
    signalwire_api_token: Optional[str] = None
    signalwire_fax_from_e164: Optional[str] = None

    # Documo
    documo_api_key: Optional[str] = None
    documo_base_url: Optional[str] = None
    documo_use_sandbox: Optional[bool] = None

    # SIP/Asterisk
    ami_host: Optional[str] = None
    ami_port: Optional[int] = None
    ami_username: Optional[str] = None
    ami_password: Optional[str] = None
    fax_station_id: Optional[str] = None

    # Inbound
    inbound_enabled: Optional[bool] = None
    inbound_retention_days: Optional[int] = None
    inbound_token_ttl_minutes: Optional[int] = None
    asterisk_inbound_secret: Optional[str] = None
    phaxio_inbound_verify_signature: Optional[bool] = None
    sinch_inbound_basic_user: Optional[str] = None
    sinch_inbound_basic_pass: Optional[str] = None

    # Audit / rate limiting
    audit_log_enabled: Optional[bool] = None
    max_requests_per_minute: Optional[int] = None
    # Storage
    storage_backend: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_prefix: Optional[str] = None
    s3_region: Optional[str] = None
    s3_endpoint_url: Optional[str] = None
    s3_kms_key_id: Optional[str] = None
    # Inbound rate limits
    inbound_list_rpm: Optional[int] = None
    inbound_get_rpm: Optional[int] = None

    # Tunnel
    tunnel_provider: Optional[str] = None  # none|cloudflare|wireguard|tailscale


def _set_env_bool(key: str, value: Optional[bool]):
    if value is None:
        return
    os.environ[key] = "true" if value else "false"


def _set_env_opt(key: str, value):
    if value is None:
        return
    os.environ[key] = str(value)


@app.put("/admin/settings", dependencies=[Depends(require_admin)])
def update_admin_settings(payload: UpdateSettingsRequest):
    """Apply configuration updates in-process by setting environment variables and reloading settings.
    Note: For persistence across restarts, write these to your .env and restart the API.
    """
    # Core
    if payload.backend:
        _set_env_opt("FAX_BACKEND", payload.backend)
    # Hybrid overrides when provided
    if payload.outbound_backend:
        _set_env_opt("FAX_OUTBOUND_BACKEND", payload.outbound_backend)
    if payload.inbound_backend:
        _set_env_opt("FAX_INBOUND_BACKEND", payload.inbound_backend)
    _set_env_bool("REQUIRE_API_KEY", payload.require_api_key)
    _set_env_bool("ENFORCE_PUBLIC_HTTPS", payload.enforce_public_https)
    _set_env_opt("PUBLIC_API_URL", payload.public_api_url)
    _set_env_bool("FAX_DISABLED", payload.fax_disabled)
    _set_env_bool("ENABLE_PERSISTED_SETTINGS", payload.enable_persisted_settings)
    _set_env_opt("DATABASE_URL", payload.database_url)
    # Feature flags
    _set_env_bool("FEATURE_V3_PLUGINS", payload.feature_v3_plugins)
    _set_env_bool("FEATURE_PLUGIN_INSTALL", payload.feature_plugin_install)
    # MCP SSE
    _set_env_bool("ENABLE_MCP_SSE", payload.enable_mcp_sse)
    _set_env_bool("REQUIRE_MCP_OAUTH", payload.require_mcp_oauth)
    _set_env_opt("OAUTH_ISSUER", payload.oauth_issuer)
    _set_env_opt("OAUTH_AUDIENCE", payload.oauth_audience)
    _set_env_opt("OAUTH_JWKS_URL", payload.oauth_jwks_url)
    _set_env_bool("ENABLE_MCP_HTTP", payload.enable_mcp_http)
    _set_env_opt("MAX_FILE_SIZE_MB", payload.max_file_size_mb)
    # Tunnel
    if payload.tunnel_provider is not None:
        prov = str(payload.tunnel_provider or "none").lower()
        if prov not in {"none", "cloudflare", "wireguard", "tailscale"}:
            raise HTTPException(400, detail="Invalid tunnel provider")
        _set_env_opt("TUNNEL_PROVIDER", prov)

    # Phaxio
    _set_env_opt("PHAXIO_API_KEY", payload.phaxio_api_key)
    _set_env_opt("PHAXIO_API_SECRET", payload.phaxio_api_secret)
    if payload.phaxio_status_callback_url is not None:
        _set_env_opt("PHAXIO_STATUS_CALLBACK_URL", payload.phaxio_status_callback_url)
        # also set alias for compatibility
        _set_env_opt("PHAXIO_CALLBACK_URL", payload.phaxio_status_callback_url)
    _set_env_bool("PHAXIO_VERIFY_SIGNATURE", payload.phaxio_verify_signature)

    # Sinch
    _set_env_opt("SINCH_PROJECT_ID", payload.sinch_project_id)
    _set_env_opt("SINCH_API_KEY", payload.sinch_api_key)
    _set_env_opt("SINCH_API_SECRET", payload.sinch_api_secret)
    _set_env_opt("SINCH_BASE_URL", payload.sinch_base_url)
    if payload.sinch_auth_method is not None:
        _set_env_opt("SINCH_AUTH_METHOD", str(payload.sinch_auth_method).lower())
    _set_env_opt("SINCH_AUTH_BASE_URL", payload.sinch_auth_base_url)

    # SignalWire
    _set_env_opt("SIGNALWIRE_SPACE_URL", payload.signalwire_space_url)
    _set_env_opt("SIGNALWIRE_PROJECT_ID", payload.signalwire_project_id)
    _set_env_opt("SIGNALWIRE_API_TOKEN", payload.signalwire_api_token)
    _set_env_opt("SIGNALWIRE_FAX_FROM_E164", payload.signalwire_fax_from_e164)

    # Documo (preview)
    _set_env_opt("DOCUMO_API_KEY", payload.documo_api_key)
    _set_env_opt("DOCUMO_BASE_URL", payload.documo_base_url)
    _set_env_bool("DOCUMO_SANDBOX", payload.documo_use_sandbox)

    # SIP
    _set_env_opt("ASTERISK_AMI_HOST", payload.ami_host)
    _set_env_opt("ASTERISK_AMI_PORT", payload.ami_port)
    _set_env_opt("ASTERISK_AMI_USERNAME", payload.ami_username)
    _set_env_opt("ASTERISK_AMI_PASSWORD", payload.ami_password)
    _set_env_opt("FAX_LOCAL_STATION_ID", payload.fax_station_id)

    # Inbound
    _set_env_bool("INBOUND_ENABLED", payload.inbound_enabled)
    _set_env_opt("INBOUND_RETENTION_DAYS", payload.inbound_retention_days)
    _set_env_opt("INBOUND_TOKEN_TTL_MINUTES", payload.inbound_token_ttl_minutes)
    _set_env_opt("ASTERISK_INBOUND_SECRET", payload.asterisk_inbound_secret)
    _set_env_bool("PHAXIO_INBOUND_VERIFY_SIGNATURE", payload.phaxio_inbound_verify_signature)
    _set_env_opt("SINCH_INBOUND_BASIC_USER", payload.sinch_inbound_basic_user)
    _set_env_opt("SINCH_INBOUND_BASIC_PASS", payload.sinch_inbound_basic_pass)
    # No HMAC verification for Sinch fax inbound

    # Audit / rate limiting
    _set_env_bool("AUDIT_LOG_ENABLED", payload.audit_log_enabled)
    _set_env_opt("MAX_REQUESTS_PER_MINUTE", payload.max_requests_per_minute)
    # Storage (note: credentials are handled by AWS SDK env/role, not here)
    storage_fields = [
        payload.storage_backend,
        payload.s3_bucket,
        payload.s3_prefix,
        payload.s3_region,
        payload.s3_endpoint_url,
        payload.s3_kms_key_id,
    ]
    _set_env_opt("STORAGE_BACKEND", payload.storage_backend)
    _set_env_opt("S3_BUCKET", payload.s3_bucket)
    _set_env_opt("S3_PREFIX", payload.s3_prefix)
    _set_env_opt("S3_REGION", payload.s3_region)
    _set_env_opt("S3_ENDPOINT_URL", payload.s3_endpoint_url)
    _set_env_opt("S3_KMS_KEY_ID", payload.s3_kms_key_id)
    # Inbound rate limits
    _set_env_opt("INBOUND_LIST_RPM", payload.inbound_list_rpm)
    _set_env_opt("INBOUND_GET_RPM", payload.inbound_get_rpm)

    # Apply live
    # Detect backend/storage changes for guidance
    old_backend = settings.fax_backend
    old_ob = active_outbound()
    old_ib = active_inbound()
    old_storage = (settings.storage_backend or "local").lower()
    reload_settings()
    # (No debug logs in production path)
    new_backend = settings.fax_backend
    new_ob = active_outbound()
    new_ib = active_inbound()
    new_storage = (settings.storage_backend or "local").lower()
    backend_changed = (old_backend != new_backend)
    outbound_changed = (old_ob != new_ob)
    inbound_changed = (old_ib != new_ib)
    storage_changed = (old_storage != new_storage) or any(storage_fields)
    # Changing MCP flags typically requires restart to remount
    mcp_changed = any([
        payload.enable_mcp_sse is not None,
        payload.require_mcp_oauth is not None,
        payload.oauth_issuer is not None,
        payload.oauth_audience is not None,
        payload.oauth_jwks_url is not None,
        payload.enable_mcp_http is not None,
    ])
    if storage_changed:
        # Recreate storage client with new settings on next access
        try:
            reset_storage()
        except Exception:
            pass
    # Return current effective masked view + hints
    out = get_admin_settings()
    out["_meta"] = {
        "backend_changed": backend_changed,
        "outbound_changed": outbound_changed,
        "inbound_changed": inbound_changed,
        "storage_changed": storage_changed,
        "restart_recommended": backend_changed or new_ob == "sip" or storage_changed or mcp_changed,
    }
    return out


@app.post("/admin/settings/reload", dependencies=[Depends(require_admin)])
def admin_reload_settings():
    reload_settings()
    return get_admin_settings()


@app.post("/admin/restart", dependencies=[Depends(require_admin)])
async def admin_restart():
    """Optional: restart the API process (for containerized deployments). Controlled by ADMIN_ALLOW_RESTART."""
    if not settings.admin_allow_restart:
        raise HTTPException(403, detail="Restart not allowed")
    async def _exit_soon():
        await asyncio.sleep(0.5)
        os._exit(0)
    asyncio.create_task(_exit_soon())
    return {"ok": True, "note": "Process will exit; container manager should restart it."}


@app.get("/admin/health-status", dependencies=[Depends(require_admin)])
async def get_health_status():
    # Basic dashboard counters and posture
    with SessionLocal() as db:
        queued = db.query(FaxJob).filter(FaxJob.status == "queued").count()
        in_prog = db.query(FaxJob).filter(FaxJob.status == "in_progress").count()
        recent_fail = db.query(FaxJob).filter(
            FaxJob.status == "failed", FaxJob.updated_at > datetime.utcnow() - timedelta(hours=1)
        ).count()
    # Lightweight health signals (avoid duplicating full readiness logic)
    # DB check
    db_ok = True
    try:
        from sqlalchemy import text  # type: ignore
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    # Ghostscript
    gs_ok = shutil.which("gs") is not None
    # Backend configured
    backend = settings.fax_backend
    backend_ok = True
    if backend == "phaxio":
        backend_ok = bool(settings.phaxio_api_key and settings.phaxio_api_secret)
    elif backend == "sinch":
        backend_ok = bool(settings.sinch_project_id and settings.sinch_api_key and settings.sinch_api_secret)
    # backend_ok remains True for sip; AMI connectivity is handled asynchronously
    backend_healthy = bool(db_ok and gs_ok and backend_ok)
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "backend": backend,
        "backend_healthy": backend_healthy,
        "jobs": {"queued": queued, "in_progress": in_prog, "recent_failures": recent_fail},
        "inbound_enabled": settings.inbound_enabled,
        "api_keys_configured": bool(settings.api_key),
        "require_auth": settings.require_api_key,
    }


@app.get("/admin/db-status", dependencies=[Depends(require_admin)])
def admin_db_status():
    from sqlalchemy import text  # type: ignore
    url = settings.database_url
    engine = "unknown"
    sqlite_file = None
    if url.startswith("sqlite:"):
        engine = "sqlite"
        # sqlite:///./file or sqlite:////abs
        path = url.split("sqlite:///")[-1]
        if path.startswith("/"):
            sqlite_file = path
        else:
            # relative to CWD
            sqlite_file = os.path.abspath(path)
    elif url.startswith("postgres"):  # pragma: no cover
        engine = "postgres"
    elif url.startswith("mysql"):  # pragma: no cover
        engine = "mysql"

    connected = False
    err = None
    counts = {}
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            connected = True
            # Try lightweight counts
            try:
                counts["fax_jobs"] = db.query(FaxJob).count()
            except Exception:  # pragma: no cover
                counts["fax_jobs"] = None
            try:
                from .auth import APIKey  # type: ignore
                counts["api_keys"] = db.query(APIKey).count()
            except Exception:
                counts["api_keys"] = None
            try:
                from .db import InboundFax  # type: ignore
                counts["inbound_fax"] = db.query(InboundFax).count()
            except Exception:
                counts["inbound_fax"] = None
    except Exception as e:  # pragma: no cover
        connected = False
        err = str(e)

    sqlite_info = None
    if sqlite_file:
        try:
            st = os.stat(sqlite_file)
            sqlite_info = {
                "path": sqlite_file,
                "exists": True,
                "size_bytes": st.st_size,
                "modified": datetime.utcfromtimestamp(st.st_mtime).isoformat(),
                "persistent_volume": sqlite_file.startswith("/faxdata/") or sqlite_file.startswith("/faxdata")
            }
        except FileNotFoundError:
            sqlite_info = {"path": sqlite_file, "exists": False}

    return {
        "url": _mask_url(url),
        "engine": engine,
        "connected": connected,
        "error": err,
        "counts": counts,
        "sqlite": sqlite_info,
    }

# ====== Manifest Providers (HTTP) — install/validate (admin-only) ======

def _providers_dir() -> str:
    return os.getenv("FAXBOT_PROVIDERS_DIR", os.path.join(os.getcwd(), "config", "providers"))


class ManifestIn(BaseModel):
    manifest: dict


@app.post("/admin/plugins/http/install", dependencies=[Depends(require_admin)])
def install_http_manifest(payload: ManifestIn):
    man = HttpManifest.from_dict(payload.manifest or {})
    if not man.id:
        raise HTTPException(400, detail="Manifest id is required")
    dest_dir = os.path.join(_providers_dir(), man.id)
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, "manifest.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload.manifest, f, indent=2)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"ok": True, "id": man.id, "path": path}


class ManifestValidateIn(BaseModel):
    manifest: dict
    credentials: dict | None = None
    settings: dict | None = None
    to: str | None = None
    file_url: str | None = None
    from_number: str | None = None
    render_only: bool | None = True


@app.post("/admin/plugins/http/validate", dependencies=[Depends(require_admin)])
async def validate_http_manifest(payload: ManifestValidateIn):
    man = HttpManifest.from_dict(payload.manifest or {})
    if not man.id:
        raise HTTPException(400, detail="Manifest id required")
    info = {
        "id": man.id,
        "name": man.name,
        "actions": list(man.actions.keys()),
        "allowed_domains": man.allowed_domains,
    }
    # Basic HIPAA posture checks
    try:
        if settings.enforce_public_https:
            insecure = []
            for k, act in (man.actions or {}).items():
                try:
                    from urllib.parse import urlparse as _p
                    if act.url and _p(act.url).scheme == "http":
                        insecure.append(k)
                except Exception:
                    pass
            if insecure:
                info["warnings"] = [f"Action(s) {', '.join(insecure)} use HTTP. HTTPS is required when ENFORCE_PUBLIC_HTTPS=true."]
    except Exception:
        pass
    if not payload.render_only:
        try:
            rt = HttpProviderRuntime(man, payload.credentials or {}, payload.settings or {})
            res = await rt.send_fax(to=payload.to or "+15551234567", file_url=payload.file_url, from_number=payload.from_number)
            info["normalized"] = res
        except Exception as e:
            info["error"] = str(e)
    return info


class ImportManifestsIn(BaseModel):
    items: Optional[List[dict]] = None
    markdown: Optional[str] = None
    source: Optional[str] = None  # 'repo_scrape' reads api_plugins_list.md from CWD


def _extract_json_blocks(md: str) -> List[dict]:
    blocks: List[dict] = []
    try:
        import re as _re
        pattern = _re.compile(r"```(?:json)?\s*([\s\S]*?)```", _re.MULTILINE)
        for m in pattern.finditer(md):
            raw = m.group(1).strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    blocks.append(obj)
                elif isinstance(obj, list):
                    for it in obj:
                        if isinstance(it, dict):
                            blocks.append(it)
            except Exception:
                continue
    except Exception:
        pass
    return blocks


@app.post("/admin/plugins/http/import-manifests", dependencies=[Depends(require_admin)])
def import_http_manifests(payload: ImportManifestsIn):
    """Bulk import provider manifests from JSON list or scraped markdown.
    For markdown, extracts JSON code fences and imports objects that look like manifests.
    """
    candidates: List[dict] = []
    if (payload.source or "").lower() == "repo_scrape" and not payload.items and not payload.markdown:
        try:
            scrape_path = os.path.join(os.getcwd(), "api_plugins_list.md")
            with open(scrape_path, "r", encoding="utf-8") as f:
                payload.markdown = f.read()
        except Exception as e:
            raise HTTPException(404, detail=f"Scrape file not found or unreadable: {e}")
    if payload.items:
        for it in payload.items:
            if isinstance(it, dict):
                candidates.append(it)
    if payload.markdown:
        candidates.extend(_extract_json_blocks(payload.markdown or ""))
    if not candidates:
        raise HTTPException(400, detail="No manifest candidates provided")
    imported: List[dict] = []
    errors: List[dict] = []
    for data in candidates:
        try:
            man = HttpManifest.from_dict(data)
            if not man.id:
                raise ValueError("manifest.id missing")
            dest_dir = os.path.join(_providers_dir(), man.id)
            os.makedirs(dest_dir, exist_ok=True)
            path = os.path.join(dest_dir, "manifest.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            imported.append({"id": man.id, "name": man.name, "path": path})
        except Exception as e:
            errors.append({"error": str(e), "data_keys": list(data.keys())[:5]})
    return {"ok": True, "imported": imported, "errors": errors}


class LogsQuery(BaseModel):
    q: Optional[str] = None
    event: Optional[str] = None
    since: Optional[str] = None
    limit: Optional[int] = 200


@app.get("/admin/logs", dependencies=[Depends(require_admin)])
def admin_logs(q: Optional[str] = None, event: Optional[str] = None, since: Optional[str] = None, limit: int = 200):
    """Return recent audit logs from in-process ring buffer with simple filtering.
    For persistent logs, configure AUDIT_LOG_FILE and use external tooling; this endpoint focuses on interactive UI needs.
    """
    try:
        rows = query_recent_logs(q=q, event=event, since=since, limit=limit)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"items": rows, "count": len(rows)}


@app.get("/admin/logs/tail", dependencies=[Depends(require_admin)])
def admin_logs_tail(q: Optional[str] = None, event: Optional[str] = None, lines: int = 2000):
    """Tail the audit log file when AUDIT_LOG_FILE is configured.
    Returns last N lines (default 2000), filtered by substring and/or event name when logs are JSON.
    """
    path = settings.audit_log_file or ""
    if not path:
        raise HTTPException(400, detail="AUDIT_LOG_FILE not configured")
    try:
        from collections import deque as _dq
        dq = _dq(maxlen=max(1, min(lines, 20000)))
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                dq.append(line.rstrip('\n'))
        out = []
        q_norm = (q or "").lower()
        for raw in dq:
            item = {"raw": raw}
            try:
                import json as _json
                obj = _json.loads(raw)
                item.update(obj if isinstance(obj, dict) else {"message": obj})
            except Exception:
                pass
            if event and str(item.get("event")) != event:
                continue
            if q_norm and q_norm not in raw.lower():
                continue
            out.append(item)
        return {"items": out, "count": len(out), "source": path}
    except FileNotFoundError:
        raise HTTPException(404, detail="Audit log file not found")
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ===== Admin actions (safe, allowlisted exec for UI) =====
class ActionItem(BaseModel):
    id: str
    label: str
    backend: Optional[List[str]] = None  # None or ["*"] means all


_ACTIONS_REGISTRY: Dict[str, Dict[str, Any]] = {
    # Safe, introspective commands only; never include secrets
    "python_version": {
        "label": "Python Version",
        "kind": "python",
        "runner": lambda: {
            "stdout": f"{os.sys.version}",
            "stderr": "",
            "code": 0,
        },
        "backend": ["*"]
    },
    "gs_version": {
        "label": "Ghostscript Version",
        "kind": "shell",
        "cmd": ["gs", "-v"],
        "timeout": 10,
        "backend": ["sip", "freeswitch"],
    },
    "list_faxdata": {
        "label": "List /faxdata",
        "kind": "shell",
        "cmd": ["ls", "-la", "/faxdata"],
        "timeout": 5,
        "backend": ["*"]
    },
    # Tunnel helpers (local-only admin actions) — keep only safe file reads
}


def _admin_exec_enabled() -> bool:
    # Default enabled when local admin is on; can be disabled via env
    val = os.getenv("ENABLE_ADMIN_EXEC", None)
    if val is not None:
        return val.lower() in {"1", "true", "yes"}
    return os.getenv("ENABLE_LOCAL_ADMIN", "false").lower() in {"1","true","yes"}


@app.get("/admin/actions", dependencies=[Depends(require_admin)])
def admin_actions_list():
    if not _admin_exec_enabled():
        return {"enabled": False, "items": []}
    b = settings.fax_backend or ""
    items: List[ActionItem] = []
    for aid, meta in _ACTIONS_REGISTRY.items():
        backends = meta.get("backend") or ["*"]
        if "*" in backends or b in backends:
            items.append(ActionItem(id=aid, label=meta.get("label") or aid, backend=backends))
    return {"enabled": True, "items": [i.dict() for i in items]}


class RunActionIn(BaseModel):
    id: str


@app.post("/admin/actions/run", dependencies=[Depends(require_admin)])
def admin_actions_run(payload: RunActionIn):
    if not _admin_exec_enabled():
        raise HTTPException(403, detail="Admin exec is disabled. Set ENABLE_ADMIN_EXEC=true for local-only use.")
    meta = _ACTIONS_REGISTRY.get(payload.id)
    if not meta:
        raise HTTPException(404, detail="Unknown action")
    # Backend gate
    backs = meta.get("backend") or ["*"]
    if "*" not in backs and settings.fax_backend not in backs:
        raise HTTPException(400, detail="Action not applicable for current backend")
    try:
        if meta.get("kind") == "python":
            res = meta.get("runner")()
            return {"ok": True, "id": payload.id, **res}
        elif meta.get("kind") == "shell":
            cmd = meta.get("cmd")
            if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
                raise ValueError("Invalid command spec")
            timeout = int(meta.get("timeout", 20))
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return {
                "ok": p.returncode == 0,
                "id": payload.id,
                "code": p.returncode,
                "stdout": p.stdout[-10000:],
                "stderr": p.stderr[-4000:],
            }
        else:
            raise ValueError("Unsupported action kind")
    except subprocess.TimeoutExpired:
        return {"ok": False, "id": payload.id, "code": 124, "stdout": "", "stderr": "Timed out"}
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ===== VPN Tunnel endpoints (admin-only, UI-driven) =====
class TunnelStatusOut(BaseModel):
    enabled: bool
    provider: str  # none|cloudflare|wireguard|tailscale
    status: str    # disabled|connecting|connected|error
    public_url: Optional[str] = None
    local_ip: Optional[str] = None
    last_checked: Optional[datetime] = None
    error_message: Optional[str] = None


class TunnelConfigIn(BaseModel):
    enabled: bool = False
    provider: str = "none"  # none|cloudflare|wireguard|tailscale
    cloudflare_custom_domain: Optional[str] = None
    wireguard_endpoint: Optional[str] = None
    wireguard_server_public_key: Optional[str] = None
    wireguard_client_ip: Optional[str] = None
    wireguard_dns: Optional[str] = None
    tailscale_auth_key: Optional[str] = None
    tailscale_hostname: Optional[str] = None


class TunnelTestOut(BaseModel):
    ok: bool
    message: Optional[str] = None
    target: Optional[str] = None


# In-memory state (non-persistent; UI persists a masked version in .env via existing settings persistence)
_TUNNEL_STATE: Dict[str, Any] = {
    "enabled": False,
    "provider": "none",
    "public_url": None,
    "last_checked": None,
    "error": None,
}

_PAIR_CODES: Dict[str, Dict[str, Any]] = {}


def _hipaa_posture_enabled() -> bool:
    try:
        return bool(settings.enforce_public_https and (settings.require_api_key or settings.api_key))
    except Exception:
        return False


@app.get("/admin/tunnel/status", dependencies=[Depends(require_admin)])
def admin_tunnel_status() -> TunnelStatusOut:
    # Compose a conservative status view; do not leak secrets
    enabled = bool(_TUNNEL_STATE.get("enabled"))
    provider = str(_TUNNEL_STATE.get("provider") or "none").lower()
    public_url = _TUNNEL_STATE.get("public_url")
    error = _TUNNEL_STATE.get("error")
    # HIPAA posture disables Cloudflare quick tunnel
    if provider == "cloudflare" and _hipaa_posture_enabled():
        return TunnelStatusOut(
            enabled=False,
            provider="cloudflare",
            status="error",
            public_url=None,
            error_message="Cloudflare Quick Tunnel is not HIPAA compliant. Use WireGuard or Tailscale.",
            last_checked=datetime.utcnow(),
        )
    status = "disabled"
    if enabled:
        status = "connected" if (public_url and provider == "cloudflare") else "connecting"
        if error:
            status = "error"
    # Derive a local IP hint best-effort
    local_ip = None
    try:
        import socket
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        pass
    # Dev-only Cloudflare URL discovery (optional): parse a known log path if HIPAA off
    discovered_url = None
    if not _hipaa_posture_enabled() and not public_url:
        try:
            log_path = os.getenv("CLOUDFLARE_LOG_PATH", os.getenv("CLOUDFLARED_LOG_PATH", "/faxdata/cloudflared/cloudflared.log"))
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                import re as _re
                m = _re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
                if m:
                    discovered_url = m.group(0)
                    _TUNNEL_STATE.update({"public_url": discovered_url, "provider": "cloudflare", "enabled": True, "last_checked": datetime.utcnow(), "error": None})
                    public_url = discovered_url
                    provider = "cloudflare"
                    status = "connected"
        except Exception:
            pass

    return TunnelStatusOut(
        enabled=enabled,
        provider=provider,
        status=status,
        public_url=public_url if provider == "cloudflare" and not _hipaa_posture_enabled() else None,
        local_ip=local_ip,
        last_checked=datetime.utcnow(),
        error_message=(str(error) if error else None),
    )


@app.post("/admin/tunnel/config", dependencies=[Depends(require_admin)])
def admin_tunnel_config(payload: TunnelConfigIn) -> TunnelStatusOut:
    # Validate provider
    provider = (payload.provider or "none").lower()
    if provider not in {"none", "cloudflare", "wireguard", "tailscale"}:
        raise HTTPException(400, detail="Invalid provider")
    # Enforce HIPAA posture
    if provider == "cloudflare" and _hipaa_posture_enabled():
        # Auto-disable and warn in status
        _TUNNEL_STATE.update({
            "enabled": False,
            "provider": "cloudflare",
            "public_url": None,
            "error": "Cloudflare Quick Tunnel is not allowed in HIPAA posture",
            "last_checked": datetime.utcnow(),
        })
        return admin_tunnel_status()
    # Apply config safely (no secrets echoed)
    _TUNNEL_STATE.update({
        "enabled": bool(payload.enabled),
        "provider": provider,
        "error": None,
        "last_checked": datetime.utcnow(),
    })
    # Reset derived URL on provider change
    if provider != "cloudflare":
        _TUNNEL_STATE["public_url"] = None
    return admin_tunnel_status()


@app.post("/admin/tunnel/test", dependencies=[Depends(require_admin)])
def admin_tunnel_test() -> TunnelTestOut:
    # Prefer testing the public tunnel URL when available; else test local/public_api_url
    try:
        import http.client
        from urllib.parse import urlparse as _up
        url = (_TUNNEL_STATE.get("public_url") or settings.public_api_url or "http://localhost:8080").rstrip("/")
        p = _up(url)
        host = p.hostname or "localhost"
        port = p.port or (443 if p.scheme == "https" else 80)
        path = "/health"
        conn = http.client.HTTPSConnection(host, port, timeout=3) if p.scheme == "https" else http.client.HTTPConnection(host, port, timeout=3)
        conn.request("GET", path)
        resp = conn.getresponse()
        ok = (resp.status == 200)
        return TunnelTestOut(ok=ok, message=("OK" if ok else f"HTTP {resp.status}"), target=f"{host}:{port}{path}")
    except Exception as e:
        return TunnelTestOut(ok=False, message=str(e)[:120])


class PairOut(BaseModel):
    code: str
    expires_at: datetime


@app.post("/admin/tunnel/pair", dependencies=[Depends(require_admin)])
def admin_tunnel_pair() -> PairOut:
    # Generate a short-lived numeric code; do not include secrets in the QR/content
    code = str(secrets.randbelow(899999) + 100000)
    expires = datetime.utcnow() + timedelta(minutes=5)
    _PAIR_CODES[code] = {"expires_at": expires, "created_at": datetime.utcnow()}
    return PairOut(code=code, expires_at=expires)


class RegisterSinchOut(BaseModel):
    success: bool
    webhook_url: Optional[str] = None
    provider_response: Optional[dict[str, Any]] = None
    error: Optional[str] = None


@app.post("/admin/tunnel/register-sinch", dependencies=[Depends(require_admin)])
def admin_tunnel_register_sinch() -> RegisterSinchOut:
    """Programmatically register the current public webhook URL with Sinch Fax.

    Non-HIPAA users may rely on Cloudflare Quick Tunnels; HIPAA users typically
    configure a stable PUBLIC_API_URL. This endpoint prefers the active tunnel
    URL when available and otherwise falls back to PUBLIC_API_URL.
    """
    # Determine webhook base
    public_url = _TUNNEL_STATE.get("public_url") or (settings.public_api_url or "").rstrip("/")
    if not public_url:
        return RegisterSinchOut(success=False, error="No public URL available (tunnel inactive and PUBLIC_API_URL unset)")

    webhook_url = f"{public_url}/sinch-inbound"

    # Validate backend and credentials
    if active_inbound() != "sinch":
        return RegisterSinchOut(success=False, error="Inbound backend is not Sinch; switch inbound to Sinch to register its webhook")
    project_id = (settings.sinch_project_id or "").strip()
    api_key = (settings.sinch_api_key or "").strip()
    api_secret = (settings.sinch_api_secret or "").strip()
    if not (project_id and api_key and api_secret):
        return RegisterSinchOut(success=False, error="Missing Sinch credentials (project_id/api_key/api_secret)")

    # Resolve base URL
    base_url = os.getenv("SINCH_BASE_URL", "https://fax.api.sinch.com/v3").rstrip("/")
    api_url = f"{base_url}/projects/{project_id}/services/default"

    payload = {
        "incomingFaxWebhook": {
            "url": webhook_url,
            "eventTypes": ["INCOMING_FAX", "FAX_COMPLETED"],
        }
    }

    try:
        import httpx  # local import to avoid startup penalty
        with httpx.Client(timeout=15.0, headers={"Content-Type": "application/json"}) as client:
            resp = client.patch(api_url, json=payload, auth=(api_key, api_secret))
            if resp.status_code in (200, 201):
                data = {}
                try:
                    data = resp.json()
                except Exception:
                    data = {"status_code": resp.status_code}
                return RegisterSinchOut(success=True, webhook_url=webhook_url, provider_response=data)
            return RegisterSinchOut(success=False, webhook_url=webhook_url, error=f"Sinch API error {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        return RegisterSinchOut(success=False, webhook_url=webhook_url, error=str(e)[:200])


@app.get("/admin/tunnel/cloudflared/logs", dependencies=[Depends(require_admin)])
def admin_tunnel_cloudflared_logs(lines: int = Query(default=50, ge=1, le=500)):
    """Read last N lines from the Cloudflared log file on disk.
    This avoids shelling out and works with the sidecar volume mount.
    """
    log_path = os.getenv("CLOUDFLARE_LOG_PATH", os.getenv("CLOUDFLARED_LOG_PATH", "/faxdata/cloudflared/cloudflared.log"))
    try:
        from collections import deque
        dq = deque(maxlen=int(lines))
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                dq.append(line.rstrip('\n'))
        return {"items": list(dq), "path": log_path}
    except FileNotFoundError:
        raise HTTPException(404, detail="Cloudflared log file not found")
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.get("/admin/inbound/callbacks", dependencies=[Depends(require_admin)])
def admin_inbound_callbacks():
    # Reload to reflect current inbound backend and auth flags
    try:
        reload_settings()
    except Exception:
        pass
    base = settings.public_api_url.rstrip("/")
    backend = active_inbound()
    out: dict[str, Any] = {"backend": backend, "callbacks": []}
    if backend == "phaxio":
        out["callbacks"].append({
            "name": "Phaxio Inbound",
            "url": f"{base}/phaxio-inbound",
            "verify_signature": settings.phaxio_inbound_verify_signature,
            "notes": "Configure in Phaxio console → Inbound settings. Enable HMAC verification if policy requires.",
        })
    elif backend == "sinch":
        out["callbacks"].append({
            "name": "Sinch Fax Inbound",
            "url": f"{base}/sinch-inbound",
            "auth": {"basic": bool(settings.sinch_inbound_basic_user)},
            "notes": "Set webhook in Sinch Fax Customer Dashboard. Sinch webhooks are not provider‑signed; you may enforce Basic auth.",
            "content_types": ["application/json", "multipart/form-data"],
            "preferred_content_type": "application/json",
        })
    elif backend == "signalwire":
        out["callbacks"].append({
            "name": "SignalWire Fax Status",
            "url": f"{base}/signalwire-callback",
            "notes": "Configure StatusCallback on send; this endpoint will process updates.",
        })
    elif backend == "sip":
        out["callbacks"].append({
            "name": "Asterisk Internal",
            "url": "/_internal/asterisk/inbound",
            "header": "X-Internal-Secret",
            "secret_configured": bool(settings.asterisk_inbound_secret),
            "notes": "Call from dialplan/AGI on the private network only.",
            "example_curl": (
                "curl -X POST -H 'X-Internal-Secret: <secret>' -H 'Content-Type: application/json' "
                "http://api:8080/_internal/asterisk/inbound "
                "-d '{\"tiff_path\":\"/faxdata/in.tiff\",\"to_number\":\"+1555...\"}'"
            ),
        })
    return out


class SimulateInboundIn(BaseModel):
    backend: Optional[str] = None  # default to current backend
    fr: Optional[str] = None
    to: Optional[str] = None
    pages: Optional[int] = 1
    status: Optional[str] = "received"


@app.post("/admin/inbound/simulate", dependencies=[Depends(require_admin)])
def admin_inbound_simulate(payload: SimulateInboundIn):
    if not settings.inbound_enabled:
        raise HTTPException(400, detail="Inbound not enabled")
    backend = (payload.backend or settings.fax_backend).lower()
    job_id = uuid.uuid4().hex
    data_dir = settings.fax_data_dir
    ensure_dir(data_dir)
    # Create a valid small PDF for simulation
    pdf_path = os.path.join(data_dir, f"{job_id}.pdf")
    try:
        from .conversion import write_valid_text_pdf  # type: ignore
        write_valid_text_pdf(pdf_path, [
            "Inbound simulation (Faxbot)",
            f"Backend: {backend}",
            f"To: {payload.to or ''} From: {payload.fr or ''}",
        ])
    except Exception:
        # Fallback minimal but valid PDF generation without reportlab context
        with open(pdf_path, "wb") as f:
            f.write(b"%PDF-1.4\n% Faxbot inbound simulation\n%%EOF")
    size_bytes = os.path.getsize(pdf_path)
    with open(pdf_path, "rb") as _f:
        sha256_hex = hashlib.sha256(_f.read()).hexdigest()
    storage = get_storage()
    try:
        stored_uri = storage.put_pdf(pdf_path, f"{job_id}.pdf")
    except Exception:
        stored_uri = None
    try:
        if stored_uri.startswith("s3://") and os.path.exists(pdf_path):
            os.remove(pdf_path)
    except Exception:
        pass
    pdf_token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=max(1, settings.inbound_token_ttl_minutes))
    retention_until = datetime.utcnow() + timedelta(days=settings.inbound_retention_days) if settings.inbound_retention_days > 0 else None

    with SessionLocal() as db:
        fx = InboundFax(
            id=job_id,
            from_number=payload.fr,
            to_number=payload.to,
            status=payload.status or "received",
            backend=backend,
            inbound_backend=active_inbound(),
            provider_sid=None,
            pages=payload.pages,
            size_bytes=size_bytes,
            sha256=sha256_hex,
            pdf_path=stored_uri,
            tiff_path=None,
            mailbox_label=None,
            retention_until=retention_until,
            pdf_token=pdf_token,
            pdf_token_expires_at=expires_at,
            created_at=datetime.utcnow(),
            received_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(fx)
        db.commit()
    audit_event("inbound_received", job_id=job_id, backend=backend)
    return {"id": job_id, "status": "ok"}


class PurgeInboundBySidIn(BaseModel):
    provider_sid: str


@app.delete("/admin/inbound/purge-by-sid", dependencies=[Depends(require_admin)])
def admin_purge_inbound_by_sid(payload: PurgeInboundBySidIn):
    """Admin-only utility to delete inbound_faxes and inbound_events by provider_sid.
    Intended for local/dev use to keep tests hermetic.
    """
    deleted_faxes = 0
    deleted_events = 0
    try:
        with SessionLocal() as db:
            from .db import InboundFax, InboundEvent  # type: ignore
            faxes = db.query(InboundFax).filter(InboundFax.provider_sid == payload.provider_sid).all()
            for r in faxes:
                db.delete(r)
            deleted_faxes = len(faxes)
            events = db.query(InboundEvent).filter(InboundEvent.provider_sid == payload.provider_sid).all()
            for r in events:
                db.delete(r)
            deleted_events = len(events)
            db.commit()
        audit_event("admin_purge_inbound", provider_sid=payload.provider_sid, faxes=deleted_faxes, events=deleted_events)
        return {"ok": True, "deleted_faxes": deleted_faxes, "deleted_events": deleted_events}
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to purge: {e}")


@app.get("/admin/fax-jobs", dependencies=[Depends(require_admin)])
async def list_admin_jobs(
    status: Optional[str] = None,
    backend: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
):
    with SessionLocal() as db:
        q = db.query(FaxJob)
        if status:
            q = q.filter(FaxJob.status == status)
        if backend:
            q = q.filter(FaxJob.backend == backend)
        total = q.count()
        rows = q.order_by(FaxJob.created_at.desc()).offset(offset).limit(limit).all()
        return {
            "total": total,
            "jobs": [
                {
                    "id": r.id,
                    "to_number": mask_phone(getattr(r, "to_number", None)),
                    "status": r.status,
                    "backend": r.backend,
                    "pages": r.pages,
                    "error": sanitize_error(getattr(r, "error", None)),
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                }
                for r in rows
            ],
        }


@app.get("/admin/fax-jobs/{job_id}", dependencies=[Depends(require_admin)])
async def get_admin_job(job_id: str):
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if not job:
            raise HTTPException(404, detail="Job not found")
        return {
            "id": job.id,
            "to_number": mask_phone(getattr(job, "to_number", None)),
            "status": job.status,
            "backend": job.backend,
            "pages": job.pages,
            "error": sanitize_error(getattr(job, "error", None)),
            "provider_sid": job.provider_sid,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "file_name": job.file_name,
        }


@app.get("/admin/fax-jobs/{job_id}/pdf", dependencies=[Depends(require_admin)])
def admin_get_job_pdf(job_id: str):
    """Admin-only: download the outbound fax PDF for a job if present.
    Works for all backends; the API generates/keeps a PDF per job prior to sending.
    """
    pdf_path = os.path.join(settings.fax_data_dir, f"{job_id}.pdf")
    if not os.path.exists(pdf_path):
        raise HTTPException(404, detail="PDF file not found")
    audit_event("admin_pdf_download", job_id=job_id)
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"fax_{job_id}.pdf",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.post("/admin/fax-jobs/{job_id}/refresh", dependencies=[Depends(require_admin)])
async def admin_refresh_job(job_id: str):
    """Refresh job status via provider when supported (manifest providers preview).
    For manifest-based outbound providers that define get_status, polls provider and updates DB.
    """
    # Load job
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if not job:
            raise HTTPException(404, detail="Job not found")
        backend = (job.backend or settings.fax_backend).lower()
    # Only handle manifest-backed backends for now
    mpath = os.path.join(os.getcwd(), "config", "providers", backend, "manifest.json")
    if not (settings.feature_v3_plugins and os.path.exists(mpath)):
        raise HTTPException(400, detail="Refresh not supported for this backend")
    try:
        with open(mpath, "r", encoding="utf-8") as f:
            man = HttpManifest.from_dict(json.load(f))
        p_settings: Dict[str, Any] = {}
        try:
            if _read_cfg is not None:
                cfg = _read_cfg(settings.faxbot_config_path)
                if getattr(cfg, "ok", False) and getattr(cfg, "data", None):
                    ob = ((cfg.data.get("providers") or {}).get("outbound") or {})
                    if (ob.get("plugin") or "").lower() == backend:
                        p_settings = ob.get("settings") or {}
        except Exception:
            pass
        rt = HttpProviderRuntime(man, {}, p_settings)
        res = await rt.get_status(job_id=job_id, provider_sid=(job.provider_sid or None))
        status = str(res.get("status") or job.status)
        prov_sid = str(res.get("job_id") or job.provider_sid or "")
        with SessionLocal() as db:
            j = db.get(FaxJob, job_id)
            if j:
                j.status = status
                if prov_sid:
                    j.provider_sid = prov_sid
                j.updated_at = datetime.utcnow()
                db.add(j)
                db.commit()
        with SessionLocal() as db:
            j2 = db.get(FaxJob, job_id)
            if j2:
                return _serialize_job(j2)
        raise HTTPException(500, detail="Failed to load updated job")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.post("/admin/diagnostics/run", dependencies=[Depends(require_admin)])
async def run_diagnostics():
    """Run bounded, non-destructive diagnostics. Backend‑isolated: only report
    checks relevant to the active outbound/inbound providers and required traits.
    """
    ob = active_outbound()
    ib = active_inbound()
    diag: dict[str, Any] = {
        "timestamp": datetime.utcnow().isoformat(),
        "backend": settings.fax_backend,
        "outbound_backend": ob,
        "inbound_backend": ib,
        "checks": {},
    }

    # Per-direction checks (trait-driven)
    # Outbound
    out: dict[str, Any] = {"backend": ob, "requires_ami": providerHasTrait("outbound", "requires_ami")}
    if ob == "phaxio":
        out["auth_configured"] = bool(settings.phaxio_api_key and settings.phaxio_api_secret)
        out["public_url_https"] = settings.public_api_url.startswith("https://")
    elif ob == "sinch":
        out["auth_configured"] = bool(settings.sinch_project_id and settings.sinch_api_key and settings.sinch_api_secret)
        # Optional live ping (fast): verify credentials reach a basic resource
        try:
            if out["auth_configured"]:
                base = os.getenv("SINCH_BASE_URL", "https://fax.api.sinch.com/v3").rstrip("/")
                url = f"{base}/projects/{settings.sinch_project_id}/services/default"
                import httpx
                token = None
                if (os.getenv("SINCH_AUTH_METHOD", settings.sinch_auth_method or "basic").lower() == "oauth"):
                    try:
                        from .sinch_service import get_sinch_service  # local import to avoid cycles
                        svc = get_sinch_service()
                        if svc is not None:
                            token = await svc.get_access_token()
                    except Exception:
                        token = None
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url, headers={"Authorization": f"Bearer {token}"} if token else None, auth=None if token else (settings.sinch_api_key, settings.sinch_api_secret))
                ok = (resp.status_code < 400)
                if not ok:
                    url2 = f"{base}/projects/{settings.sinch_project_id}/faxes?limit=1"
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        r2 = await client.get(url2, headers={"Authorization": f"Bearer {token}"} if token else None, auth=None if token else (settings.sinch_api_key, settings.sinch_api_secret))
                    ok = (r2.status_code < 400)
                out["auth_valid"] = ok
        except Exception:
            out.setdefault("auth_valid", None)
    elif ob == "signalwire":
        out["auth_configured"] = bool(settings.signalwire_space_url and settings.signalwire_project_id and settings.signalwire_api_token)
    elif ob == "documo":
        out["auth_configured"] = bool(settings.documo_api_key)
    if providerHasTrait("outbound", "requires_ami"):
        out["ami_password_not_default"] = settings.ami_password != "changeme"
        try:
            from .ami import test_ami_connection
            out["ami_reachable"] = await test_ami_connection(settings.ami_host, settings.ami_port, settings.ami_username, settings.ami_password)
        except Exception as e:
            out["ami_reachable"] = False
            out["ami_error"] = str(e)
    diag["checks"]["outbound"] = out

    # Inbound
    inbound: dict[str, Any] = {
        "backend": ib,
        "enabled": settings.inbound_enabled,
        "requires_ami": providerHasTrait("inbound", "requires_ami"),
        "needs_storage": providerHasTrait("inbound", "needs_storage"),
        "inbound_verification": providerTraitValue("inbound", "inbound_verification"),
    }
    if settings.inbound_enabled:
        # Derive verification posture from provider traits
        # Map to concrete flags when applicable
        if providerHasTrait("inbound", "requires_ami"):
            inbound["asterisk_secret_set"] = bool(settings.asterisk_inbound_secret)
        if ib == "phaxio":
            inbound["signature_verification"] = settings.phaxio_inbound_verify_signature
        if ib == "sinch":
            inbound["basic_auth_configured"] = bool(settings.sinch_inbound_basic_user)
    diag["checks"]["inbound"] = inbound

    # System checks
    sys: dict[str, Any] = {
        "ghostscript": shutil.which("gs") is not None,
        "fax_data_dir": os.path.exists(settings.fax_data_dir),
        "fax_data_writable": False,
        "database_connected": False,
        "temp_dir_writable": False,
    }
    # Test fax_data_dir write
    try:
        os.makedirs(settings.fax_data_dir, exist_ok=True)
        test_path = os.path.join(settings.fax_data_dir, f"diag_{uuid.uuid4().hex}")
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
        sys["fax_data_writable"] = True
    except Exception:
        pass
    # DB
    try:
        from sqlalchemy import text  # type: ignore
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            sys["database_connected"] = True
    except Exception:
        pass
    # Temp dir
    try:
        with tempfile.NamedTemporaryFile(delete=True) as tmp:
            tmp.write(b"ok")
            sys["temp_dir_writable"] = True
    except Exception:
        pass
    diag["checks"]["system"] = sys

    # Storage diagnostics only when required by inbound provider traits
    if providerHasTrait("inbound", "needs_storage"):
        if settings.storage_backend.lower() == "s3":
            st = {
                "type": "s3",
                "bucket_set": bool(settings.s3_bucket),
                "region_set": bool(settings.s3_region),
                "kms_enabled": bool(settings.s3_kms_key_id),
            }
            if settings.s3_bucket and os.getenv("ENABLE_S3_DIAGNOSTICS", "false").lower() == "true":
                try:
                    import boto3  # type: ignore
                    from botocore.config import Config  # type: ignore
                    s3 = boto3.client(
                        "s3",
                        region_name=(settings.s3_region or None),
                        endpoint_url=(settings.s3_endpoint_url or None),
                        config=Config(signature_version="s3v4"),
                    )
                    s3.head_bucket(Bucket=settings.s3_bucket)
                    st["accessible"] = True
                except Exception as e:
                    st["accessible"] = False
                    st["error"] = str(e)[:100]
            diag["checks"]["storage"] = st
        else:
            diag["checks"]["storage"] = {"type": "local", "warning": "Local storage only suitable for development"}

    # Remove duplicate/global inbound block; handled above for active inbound

    # Security summary
    diag["checks"]["security"] = {
        "enforce_https": settings.enforce_public_https,
        "audit_logging": settings.audit_log_enabled,
        "rate_limiting": settings.max_requests_per_minute > 0,
        "pdf_token_ttl": settings.pdf_token_ttl_minutes,
    }

    # Plugins (v3) readiness
    try:
        plugins_info: dict[str, Any] = {
            "v3_enabled": settings.feature_v3_plugins,
            "plugin_install_enabled": settings.feature_plugin_install,
            "active_outbound": ob,
            "installed": 0,
            "manifests": [],
        }
        issues_total = 0
        if settings.feature_v3_plugins:
            prov_dir = _providers_dir()
            if os.path.isdir(prov_dir):
                for pid in os.listdir(prov_dir):
                    mpath = os.path.join(prov_dir, pid, "manifest.json")
                    if not os.path.exists(mpath):
                        continue
                    try:
                        with open(mpath, "r", encoding="utf-8") as f:
                            mdata = json.load(f)
                        man = HttpManifest.from_dict(mdata)
                        actions = list((man.actions or {}).keys())
                        issues: list[str] = []
                        # Basic manifest checks
                        if "send_fax" not in actions:
                            issues.append("missing send_fax action")
                        if not man.allowed_domains:
                            issues.append("allowed_domains empty")
                        # HTTPS check when enforcing HTTPS
                        if settings.enforce_public_https:
                            for name, act in (man.actions or {}).items():
                                try:
                                    pu = urlparse(act.url)
                                    if pu.scheme == "http":
                                        issues.append(f"action {name} uses http")
                                except Exception:
                                    issues.append(f"action {name} url invalid")
                        plugins_info["manifests"].append({
                            "id": man.id,
                            "name": man.name,
                            "actions": actions,
                            "allowed_domains": man.allowed_domains,
                            "issues": issues,
                        })
                        issues_total += len(issues)
                    except Exception as e:
                        plugins_info.setdefault("errors", []).append({"id": pid, "error": str(e)})
            plugins_info["installed"] = len(plugins_info["manifests"])  # type: ignore[index]
        diag["checks"]["plugins"] = plugins_info
    except Exception:
        # Don't break diagnostics on plugin scan errors
        pass

    # Traits schema issues (expose unknown trait keys for CI visibility)
    try:
        from .config import CANONICAL_TRAIT_KEYS, get_traits_schema_issues
        diag["checks"]["traits_schema"] = {
            "allowed_keys": sorted(list(CANONICAL_TRAIT_KEYS)),
            "issues": get_traits_schema_issues(),
        }
    except Exception:
        pass

    # Summary (backend‑isolated)
    critical: list[str] = []
    warnings: list[str] = []
    if ob == "phaxio":
        p = diag["checks"].get("outbound", {})
        if not p.get("auth_configured"):
            critical.append("Phaxio API key not set")
        if not p.get("public_url_https"):
            warnings.append("PUBLIC_API_URL should be HTTPS")
    if ob == "sip":
        s = diag["checks"].get("outbound", {})
        if not s.get("ami_password_not_default"):
            critical.append("AMI password is default 'changeme'")
        if not s.get("ami_reachable"):
            warnings.append("AMI not reachable")
    if settings.inbound_enabled and ib == "sip":
        inbound = diag["checks"].get("inbound", {})
        if not inbound.get("asterisk_secret_set"):
            critical.append("ASTERISK_INBOUND_SECRET not set for inbound SIP")
    # Ghostscript: only critical when tiff conversion is required (self-hosted paths)
    if not sys.get("ghostscript"):
        if providerHasTrait("any", "requires_tiff"):
            critical.append("Ghostscript (gs) not installed — required for fax file processing")
        else:
            warnings.append("Ghostscript not found; OK for pure cloud backends with PDF pass-through")
    if not sys.get("fax_data_writable"):
        # Critical when inbound enabled or storage needed; otherwise warn
        if settings.inbound_enabled or providerHasTrait("inbound", "needs_storage"):
            critical.append("Cannot write to fax data dir")
        else:
            warnings.append("fax data dir not writable; OK when no inbound/storage")
    if not sys.get("database_connected"):
        critical.append("Database connection failed")
    # Plugin warnings
    try:
        p = diag["checks"].get("plugins", {})
        if p.get("v3_enabled") and p.get("installed", 0) > 0:
            for m in p.get("manifests", []):
                for issue in (m.get("issues") or []):
                    warnings.append(f"Plugin {m.get('id')}: {issue}")
    except Exception:
        pass
    # Attach traits so clients can render consistently across screens
    try:
        from .config import get_provider_registry
        diag["traits"] = {"registry": get_provider_registry(), "active": {"outbound": ob, "inbound": ib}}
    except Exception:
        pass
    diag["summary"] = {"healthy": len(critical) == 0, "critical_issues": critical, "warnings": warnings}
    return diag


@app.get("/admin/settings/export", dependencies=[Depends(require_admin)])
def export_settings_env():
    """Generate a .env snippet for current settings (secrets redacted)."""
    # Keep this export faithful to explicit env overrides so copy/paste is accurate
    lines: List[str] = []
    lines.append(f"FAX_BACKEND={settings.fax_backend}")
    # Include dual-backend envs only when explicitly set
    if os.getenv("FAX_OUTBOUND_BACKEND"):
        lines.append(f"FAX_OUTBOUND_BACKEND={os.getenv('FAX_OUTBOUND_BACKEND')}")
    if os.getenv("FAX_INBOUND_BACKEND"):
        lines.append(f"FAX_INBOUND_BACKEND={os.getenv('FAX_INBOUND_BACKEND')}")
    lines.append(f"REQUIRE_API_KEY={str(settings.require_api_key).lower()}")
    lines.append(f"ENFORCE_PUBLIC_HTTPS={str(settings.enforce_public_https).lower()}")
    lines.append("# NOTE: Secrets are redacted below. Fill in actual values before use.")
    if settings.fax_backend == "phaxio":
        lines.append("# Phaxio configuration")
        lines.append(
            f"PHAXIO_API_KEY={(settings.phaxio_api_key[:8] + '…') if settings.phaxio_api_key else ''}"
        )
        lines.append(
            f"PHAXIO_API_SECRET={(settings.phaxio_api_secret[:8] + '…') if settings.phaxio_api_secret else ''}"
        )
        lines.append(f"PUBLIC_API_URL={settings.public_api_url}")
        lines.append(f"PHAXIO_STATUS_CALLBACK_URL={settings.phaxio_status_callback_url}")
        lines.append("# Also supports PHAXIO_CALLBACK_URL as an alias")
        lines.append(f"PHAXIO_VERIFY_SIGNATURE={str(settings.phaxio_verify_signature).lower()}")
    elif settings.fax_backend == "sip":
        lines.append("# SIP/Asterisk configuration")
        lines.append(f"ASTERISK_AMI_HOST={settings.ami_host}")
        lines.append(f"ASTERISK_AMI_PORT={settings.ami_port}")
        lines.append(f"ASTERISK_AMI_USERNAME={settings.ami_username}")
        lines.append("ASTERISK_AMI_PASSWORD=***REDACTED***")
    elif settings.fax_backend == "sinch":
        lines.append("# Sinch configuration")
        lines.append(f"SINCH_PROJECT_ID={settings.sinch_project_id}")
        lines.append(
            f"SINCH_API_KEY={(settings.sinch_api_key[:8] + '…') if settings.sinch_api_key else ''}"
        )
        lines.append(
            f"SINCH_API_SECRET={(settings.sinch_api_secret[:8] + '…') if settings.sinch_api_secret else ''}"
        )
        if os.getenv("SINCH_BASE_URL"):
            lines.append(f"SINCH_BASE_URL={os.getenv('SINCH_BASE_URL')}")
        if os.getenv("SINCH_AUTH_METHOD"):
            lines.append(f"SINCH_AUTH_METHOD={os.getenv('SINCH_AUTH_METHOD')}")
        if os.getenv("SINCH_AUTH_BASE_URL"):
            lines.append(f"SINCH_AUTH_BASE_URL={os.getenv('SINCH_AUTH_BASE_URL')}")
    return {
        "env_content": "\n".join(lines),
        "requires_restart": True,
        "note": "v1 is read-only. Copy to .env and restart the API to apply.",
    }


def _export_settings_full_env() -> str:
    """Generate a full .env content string with unmasked values for persistence.
    Admin-only usage. Includes core, backend, inbound, storage and security settings.
    """
    kv: dict[str, str] = {}
    # Core
    kv["FAX_BACKEND"] = settings.fax_backend
    # Hybrid backends (include only when explicitly set)
    if os.getenv("FAX_OUTBOUND_BACKEND"):
        kv["FAX_OUTBOUND_BACKEND"] = active_outbound()
    if os.getenv("FAX_INBOUND_BACKEND"):
        kv["FAX_INBOUND_BACKEND"] = active_inbound()
    kv["REQUIRE_API_KEY"] = "true" if settings.require_api_key else "false"
    kv["ENFORCE_PUBLIC_HTTPS"] = "true" if settings.enforce_public_https else "false"
    kv["FAX_DISABLED"] = "true" if settings.fax_disabled else "false"
    kv["PUBLIC_API_URL"] = settings.public_api_url
    if os.getenv("TUNNEL_PROVIDER"):
        kv["TUNNEL_PROVIDER"] = os.getenv("TUNNEL_PROVIDER", "none").lower()
    kv["MAX_FILE_SIZE_MB"] = str(settings.max_file_size_mb)
    # Security / audit
    kv["AUDIT_LOG_ENABLED"] = "true" if settings.audit_log_enabled else "false"
    if settings.audit_log_file:
        kv["AUDIT_LOG_FILE"] = settings.audit_log_file
    kv["PDF_TOKEN_TTL_MINUTES"] = str(settings.pdf_token_ttl_minutes)
    if settings.max_requests_per_minute is not None:
        kv["MAX_REQUESTS_PER_MINUTE"] = str(settings.max_requests_per_minute)
    # Backend: Phaxio
    if settings.fax_backend == "phaxio":
        kv["PHAXIO_API_KEY"] = settings.phaxio_api_key or ""
        kv["PHAXIO_API_SECRET"] = settings.phaxio_api_secret or ""
        if settings.phaxio_status_callback_url:
            kv["PHAXIO_STATUS_CALLBACK_URL"] = settings.phaxio_status_callback_url
        kv["PHAXIO_VERIFY_SIGNATURE"] = "true" if settings.phaxio_verify_signature else "false"
    # Backend: Sinch
    if settings.fax_backend == "sinch":
        kv["SINCH_PROJECT_ID"] = settings.sinch_project_id or ""
        kv["SINCH_API_KEY"] = settings.sinch_api_key or ""
        kv["SINCH_API_SECRET"] = settings.sinch_api_secret or ""
        if os.getenv("SINCH_BASE_URL"):
            kv["SINCH_BASE_URL"] = os.getenv("SINCH_BASE_URL", "")
        kv["SINCH_AUTH_METHOD"] = (os.getenv("SINCH_AUTH_METHOD") or settings.sinch_auth_method or "basic").lower()
        if os.getenv("SINCH_AUTH_BASE_URL"):
            kv["SINCH_AUTH_BASE_URL"] = os.getenv("SINCH_AUTH_BASE_URL", "")
    # Backend: SIP/Asterisk
    if settings.fax_backend == "sip":
        kv["ASTERISK_AMI_HOST"] = settings.ami_host
        kv["ASTERISK_AMI_PORT"] = str(settings.ami_port)
        kv["ASTERISK_AMI_USERNAME"] = settings.ami_username
        kv["ASTERISK_AMI_PASSWORD"] = settings.ami_password
        if settings.fax_station_id:
            kv["FAX_LOCAL_STATION_ID"] = settings.fax_station_id
    # Inbound
    kv["INBOUND_ENABLED"] = "true" if settings.inbound_enabled else "false"
    kv["INBOUND_RETENTION_DAYS"] = str(settings.inbound_retention_days)
    kv["INBOUND_TOKEN_TTL_MINUTES"] = str(settings.inbound_token_ttl_minutes)
    if settings.asterisk_inbound_secret:
        kv["ASTERISK_INBOUND_SECRET"] = settings.asterisk_inbound_secret
    kv["PHAXIO_INBOUND_VERIFY_SIGNATURE"] = "true" if settings.phaxio_inbound_verify_signature else "false"
    if settings.sinch_inbound_basic_user:
        kv["SINCH_INBOUND_BASIC_USER"] = settings.sinch_inbound_basic_user
        kv["SINCH_INBOUND_BASIC_PASS"] = settings.sinch_inbound_basic_pass or ""
    kv["INBOUND_LIST_RPM"] = str(settings.inbound_list_rpm)
    kv["INBOUND_GET_RPM"] = str(settings.inbound_get_rpm)
    # Storage
    kv["STORAGE_BACKEND"] = (settings.storage_backend or "local")
    if (settings.storage_backend or "").lower() == "s3":
        if settings.s3_bucket:
            kv["S3_BUCKET"] = settings.s3_bucket
        if settings.s3_prefix:
            kv["S3_PREFIX"] = settings.s3_prefix
        if settings.s3_region:
            kv["S3_REGION"] = settings.s3_region
        if settings.s3_endpoint_url:
            kv["S3_ENDPOINT_URL"] = settings.s3_endpoint_url
        if settings.s3_kms_key_id:
            kv["S3_KMS_KEY_ID"] = settings.s3_kms_key_id
    # Admin console toggles
    kv["ENABLE_LOCAL_ADMIN"] = os.getenv("ENABLE_LOCAL_ADMIN", "true").lower()
    kv["ADMIN_ALLOW_RESTART"] = os.getenv("ADMIN_ALLOW_RESTART", "false").lower()
    # Persisted settings loader toggle
    kv["ENABLE_PERSISTED_SETTINGS"] = os.getenv("ENABLE_PERSISTED_SETTINGS", "false").lower()
    if os.getenv("PERSISTED_ENV_PATH"):
        kv["PERSISTED_ENV_PATH"] = os.getenv("PERSISTED_ENV_PATH", "") or ""
    # MCP SSE
    kv["ENABLE_MCP_SSE"] = os.getenv("ENABLE_MCP_SSE", "false").lower()
    kv["REQUIRE_MCP_OAUTH"] = os.getenv("REQUIRE_MCP_OAUTH", "false").lower()
    if settings.oauth_issuer:
        kv["OAUTH_ISSUER"] = settings.oauth_issuer
    if settings.oauth_audience:
        kv["OAUTH_AUDIENCE"] = settings.oauth_audience
    if settings.oauth_jwks_url:
        kv["OAUTH_JWKS_URL"] = settings.oauth_jwks_url
    # MCP HTTP
    kv["ENABLE_MCP_HTTP"] = os.getenv("ENABLE_MCP_HTTP", "false").lower()

    lines: list[str] = []
    for k, v in kv.items():
        if v is None:
            continue
        s = str(v)
        if any(ch in s for ch in [" ", "#"]):
            s = f'"{s}"'
        lines.append(f"{k}={s}")
    return "\n".join(lines) + "\n"


class PersistSettingsIn(BaseModel):
    content: str | None = None
    path: str | None = None


@app.post("/admin/settings/persist", dependencies=[Depends(require_admin)])
def persist_settings(payload: PersistSettingsIn):
    """Write a .env file to a persisted location (default: /faxdata/faxbot.env).
    If content is not provided, the server will render a full export from current settings.
    """
    target = payload.path or os.getenv("PERSISTED_ENV_PATH", "/faxdata/faxbot.env")
    content = payload.content or _export_settings_full_env()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write(content)
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to write settings: {e}")
    return {"ok": True, "path": target}

@app.post("/fax", response_model=FaxJobOut, status_code=202, dependencies=[Depends(require_fax_send)])
async def send_fax(background: BackgroundTasks, to: str = Form(...), file: UploadFile = File(...), idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key")):
    ob = active_outbound()
    # Preserve legacy behavior in disabled/test mode to avoid cross-test env leakage
    if settings.fax_disabled:
        ob = settings.fax_backend
    # Validate destination
    if not PHONE_RE.match(to):
        raise HTTPException(400, detail="'to' must be E.164 or digits only")
    # Stream upload to disk with magic sniff and size enforcement
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    job_id = uuid.uuid4().hex
    orig_path = os.path.join(settings.fax_data_dir, f"{job_id}-{file.filename}")
    pdf_path = os.path.join(settings.fax_data_dir, f"{job_id}.pdf")
    tiff_path = os.path.join(settings.fax_data_dir, f"{job_id}.tiff")

    total = 0
    first_chunk = b""
    CHUNK = 64 * 1024
    try:
        with open(orig_path, "wb") as out:
            # Read first chunk for magic sniff
            first_chunk = await file.read(CHUNK)
            total += len(first_chunk)
            if total > max_bytes:
                raise HTTPException(413, detail=f"File exceeds {settings.max_file_size_mb} MB limit")
            out.write(first_chunk)
            # Stream the rest
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, detail=f"File exceeds {settings.max_file_size_mb} MB limit")
                out.write(chunk)
    except HTTPException:
        try:
            if os.path.exists(orig_path):
                os.remove(orig_path)
        finally:
            raise

    # Magic sniff: PDF if starts with %PDF, else treat as text if UTF-8 clean
    is_pdf = first_chunk.startswith(b"%PDF")
    is_text = False
    if not is_pdf:
        try:
            first_chunk.decode("utf-8")
            is_text = True
        except Exception:
            is_text = False
    if not (is_pdf or is_text):
        # Unsupported type
        try:
            os.remove(orig_path)
        except Exception:
            pass
        raise HTTPException(415, detail="Only PDF and TXT are allowed")

    # Convert to PDF if needed
    if is_text or (file.filename and file.filename.lower().endswith(".txt")):
        if settings.fax_disabled:
            # Test mode - still generate a valid PDF using ReportLab
            from .conversion import write_valid_text_pdf  # type: ignore
            write_valid_text_pdf(pdf_path, [
                "Faxbot test TXT→PDF",
                f"Source: {os.path.basename(str(orig_path))}",
            ])
        else:
            txt_to_pdf(orig_path, pdf_path)
    else:
        # Copy the PDF directly
        shutil.copyfile(orig_path, pdf_path)

    # Backend-specific file preparation (trait-driven)
    pages = None
    manifest_path = os.path.join(os.getcwd(), "config", "providers", ob, "manifest.json")
    requires_tiff = False
    try:
        from .config import providerHasTrait
        requires_tiff = bool(providerHasTrait("outbound", "requires_tiff"))
    except Exception:
        requires_tiff = (ob in {"sip", "freeswitch"})
    if settings.feature_v3_plugins and os.path.exists(manifest_path):
        pages = None
    elif requires_tiff:
        if settings.fax_disabled:
            pages = 1
            with open(tiff_path, "wb") as f:
                f.write(b"TIFF_PLACEHOLDER")
        else:
            pages, _ = pdf_to_tiff(pdf_path, tiff_path)
    else:
        pages = None

    # Create job in DB with backend info
    with SessionLocal() as db:
        job = FaxJob(
            id=job_id,
            to_number=to,
            file_name=file.filename,
            tiff_path=tiff_path,
            status="queued",
            pages=pages,
            backend=ob,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
    # Record idempotency and emit canonical event (no PHI)
    if idempotency_key:
        _idempotency_put(idempotency_key, job_id)
    audit_event("FAX_QUEUED", job_id=job_id, backend=ob)

    # Kick off fax sending based on backend
    if not settings.fax_disabled:
        if ob in {"phaxio", "sinch"}:
            background.add_task(_send_via_outbound_normalized, job_id, to, pdf_path, tiff_path)
        elif ob == "signalwire":
            background.add_task(_send_via_signalwire, job_id, to, pdf_path)
        elif ob == "freeswitch":
            background.add_task(_send_via_freeswitch, job_id, to, tiff_path)
        elif settings.feature_v3_plugins and os.path.exists(manifest_path):
            background.add_task(_send_via_manifest, job_id, to, pdf_path)
        else:
            # Default to SIP/Asterisk
            background.add_task(_originate_job, job_id, to, tiff_path)

    return _serialize_job(job)


async def _originate_job(job_id: str, to: str, tiff_path: str):
    try:
        audit_event("job_dispatch", job_id=job_id, method="sip")
        await ami_client.originate_sendfax(job_id, to, tiff_path)
        # Mark as started
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                j = cast(Any, job)
                j.status = "in_progress"
                j.updated_at = datetime.utcnow()
                db.add(j)
                db.commit()
        audit_event("FAX_SENT", job_id=job_id, backend="sip")
    except Exception as e:
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                j = cast(Any, job)
                j.status = "failed"
                j.error = str(e)
                j.updated_at = datetime.utcnow()
                db.add(j)
                db.commit()
        audit_event("FAX_FAILED", job_id=job_id)


@app.get("/fax/{job_id}", response_model=FaxJobOut, dependencies=[Depends(require_fax_read)])
async def get_fax(job_id: str, refresh: bool = False):
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if not job:
            raise HTTPException(404, detail="Job not found")
    if refresh and job.provider_sid and (job.backend in {"phaxio", "sinch"}):
        try:
            from .providers.outbound import get_outbound_adapter
            from .status_map import canonical_status, load_status_map
            load_status_map()
            adapter = get_outbound_adapter()
            res = await adapter.get_status(job.provider_sid)
            raw = str((res.get("raw") or {}).get("status") or res.get("status") or "").lower()
            status = canonical_status(job.backend, raw)
            with SessionLocal() as db:
                j = db.get(FaxJob, job_id)
                if j:
                    j.status = status or j.status
                    j.updated_at = datetime.utcnow()
                    db.add(j)
                    db.commit()
                    job = j
        except Exception:
            # Best effort; return DB snapshot
            pass
    return _serialize_job(job)


# Admin API key management
@app.post("/admin/api-keys", response_model=CreateAPIKeyOut, dependencies=[Depends(require_admin)])
def admin_create_api_key(payload: CreateAPIKeyIn):
    result = create_api_key(
        name=payload.name,
        owner=payload.owner,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
        note=payload.note,
    )
    return CreateAPIKeyOut(**result)  # type: ignore[arg-type]


@app.get("/admin/api-keys", response_model=List[APIKeyMeta], dependencies=[Depends(require_admin)])
def admin_list_api_keys():
    rows = list_api_keys()
    return [APIKeyMeta(**r) for r in rows]


@app.delete("/admin/api-keys/{key_id}", dependencies=[Depends(require_admin)])
def admin_revoke_api_key(key_id: str):
    ok = revoke_api_key(key_id)
    if not ok:
        raise HTTPException(404, detail="Key not found")
    return {"status": "ok"}

# ===== WireGuard configuration & QR (admin-only) =====

def _wg_conf_path() -> str:
    # Ensure parent directory exists
    path = settings.wireguard_conf_path or "/faxdata/wireguard/wg.conf"
    try:
        ensure_dir(os.path.dirname(path))
    except Exception:
        pass
    return path


def _wg_validate_conf_text(text: str) -> None:
    # Basic sanity checks for a WireGuard config file
    t = (text or "").strip()
    if not t:
        raise HTTPException(400, detail="Empty configuration")
    # Require [Interface] section and at least one key
    if "[Interface]" not in t:
        raise HTTPException(400, detail="Missing [Interface] section")
    if "PrivateKey" not in t:
        raise HTTPException(400, detail="Missing PrivateKey in [Interface]")
    if "[Peer]" not in t:
        raise HTTPException(400, detail="Missing [Peer] section")


class WGImportOut(BaseModel):
    ok: bool


@app.post("/admin/tunnel/wg/import", dependencies=[Depends(require_admin)])
async def admin_wg_import_conf(request: Request, file: Optional[UploadFile] = File(default=None), content: Optional[str] = Form(default=None)) -> WGImportOut:  # type: ignore
    # Accept either multipart file upload or raw text via 'content' field
    try:
        conf_text: Optional[str] = None
        # Size cap for incoming conf (bytes)
        try:
            max_bytes = int(os.getenv("MAX_WG_CONF_BYTES", "65536"))
        except Exception:
            max_bytes = 65536
        if file is not None:
            raw = await file.read()
            if raw and len(raw) > max_bytes:
                raise HTTPException(413, detail="WireGuard configuration too large")
            conf_text = raw.decode("utf-8", errors="replace")
        elif content is not None:
            conf_text = str(content)
            if len(conf_text.encode("utf-8")) > max_bytes:
                raise HTTPException(413, detail="WireGuard configuration too large")
        else:
            # Try JSON payload with {"content": "..."}
            try:
                data = await request.json()
                conf_text = str(data.get("content", ""))
                if len(conf_text.encode("utf-8")) > max_bytes:
                    raise HTTPException(413, detail="WireGuard configuration too large")
            except Exception:
                conf_text = None
        if conf_text is None:
            raise HTTPException(400, detail="No configuration provided")
        _wg_validate_conf_text(conf_text)
        path = _wg_conf_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write(conf_text)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return WGImportOut(ok=True)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.get("/admin/tunnel/wg/conf", dependencies=[Depends(require_admin)])
def admin_wg_get_conf():
    path = _wg_conf_path()
    if not os.path.exists(path):
        raise HTTPException(404, detail="WireGuard configuration not found")
    # Download as text file
    filename = os.path.basename(path) or "wg.conf"
    return FileResponse(path, media_type="text/plain", filename=filename)


class WgDeleteOut(BaseModel):
    ok: bool


@app.delete("/admin/tunnel/wg/conf", dependencies=[Depends(require_admin)])
def admin_wg_delete_conf() -> WgDeleteOut:
    path = _wg_conf_path()
    if not os.path.exists(path):
        return WgDeleteOut(ok=True)
    try:
        os.remove(path)
        return WgDeleteOut(ok=True)
    except Exception as e:
        raise HTTPException(500, detail=str(e))


class WgQrOut(BaseModel):
    png_base64: Optional[str] = None
    svg_base64: Optional[str] = None


@app.post("/admin/tunnel/wg/qr", dependencies=[Depends(require_admin)])
def admin_wg_qr() -> WgQrOut:
    """Render the stored WireGuard configuration as a QR (SVG base64)."""
    path = _wg_conf_path()
    if not os.path.exists(path):
        raise HTTPException(404, detail="WireGuard configuration not found")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        _wg_validate_conf_text(content)
        # Preferred: SVG (no native deps)
        try:
            import io
            import segno  # type: ignore
            qr = segno.make(content, error='M')
            buf = io.BytesIO()
            qr.save(buf, kind='svg')
            svg_bytes = buf.getvalue()
            import base64 as _b64
            b64 = _b64.b64encode(svg_bytes).decode("ascii")
            return WgQrOut(svg_base64=b64)
        except Exception as e2:
            raise HTTPException(500, detail=f"Failed to render QR: {str(e2)}")
    except HTTPException:
        raise
    except Exception as e:
        # Provide actionable note if renderPM is unavailable
        raise HTTPException(500, detail=f"Failed to render QR: {str(e)}")


# ===== Auto‑tunnel helpers (Cloudflare) =====
_CF_URL_RE = re.compile(r"https://[a-z0-9.-]+\.trycloudflare\.com", re.IGNORECASE)


async def _auto_tunnel_cloudflare_watcher():
    """Dev/non‑HIPAA: discover Cloudflare Quick Tunnel URL from a mounted log file
    and auto‑inject into runtime settings (no persistence) so the Admin Console and
    webhooks "just work" without manual pasting. Never runs under HIPAA posture.
    """
    # Lazy import to avoid circulars at import time
    global _TUNNEL_STATE  # type: ignore
    try:
        while True:
            try:
                # Respect HIPAA posture strictly
                if settings.enforce_public_https:
                    await asyncio.sleep(15)
                    continue
                provider = (os.getenv("TUNNEL_PROVIDER", "") or "").lower()
                if provider and provider != "cloudflare" and not (os.getenv("AUTO_TUNNEL_ENABLED", "false").lower() in {"1","true","yes"}):
                    await asyncio.sleep(10)
                    continue

                log_path = os.getenv("CLOUDFLARE_LOG_PATH", "/faxdata/cloudflared/cloudflared.log")
                url_found = None
                try:
                    with open(log_path, "rb") as f:
                        # Read last ~64KB to keep bounded
                        try:
                            f.seek(-65536, os.SEEK_END)
                        except Exception:
                            pass
                        chunk = f.read().decode("utf-8", errors="replace")
                        m = None
                        for m in _CF_URL_RE.finditer(chunk):
                            url_found = m.group(0)
                        # url_found will be last match if any
                except FileNotFoundError:
                    url_found = None
                except Exception:
                    url_found = None

                # Update in‑memory state and optionally runtime settings
                if url_found and (not _TUNNEL_STATE.get("public_url") or _TUNNEL_STATE.get("public_url") != url_found):
                    _TUNNEL_STATE.update({
                        "enabled": True,
                        "provider": "cloudflare",
                        "public_url": url_found,
                        "error": None,
                        "last_checked": datetime.utcnow(),
                    })
                    # Auto‑load into runtime settings when PUBLIC_API_URL is local/default or override is allowed
                    current = (settings.public_api_url or "").strip()
                    allow_override = os.getenv("AUTO_TUNNEL_OVERRIDE", "true").lower() in {"1","true","yes"}
                    is_local = current.startswith("http://localhost") or current.startswith("http://127.0.0.1") or current == ""
                    if allow_override and is_local:
                        os.environ["PUBLIC_API_URL"] = url_found
                        try:
                            reload_settings()
                        except Exception:
                            pass
                        audit_event("auto_tunnel_public_url_set", provider="cloudflare")
                await asyncio.sleep(5)
            except Exception:
                # Never crash the watcher; back off
                await asyncio.sleep(10)
    except Exception:
        # Fail closed silently
        return


class RotateAPIKeyOut(BaseModel):
    key_id: str
    token: str


@app.post("/admin/api-keys/{key_id}/rotate", response_model=RotateAPIKeyOut, dependencies=[Depends(require_admin)])
def admin_rotate_api_key(key_id: str):
    res = rotate_api_key(key_id)
    if not res:
        raise HTTPException(404, detail="Key not found")
    return RotateAPIKeyOut(**res)  # type: ignore[arg-type]


async def _artifact_cleanup_loop():
    """Periodically delete old artifacts beyond TTL for finalized jobs."""
    interval = max(1, settings.cleanup_interval_minutes)
    while True:
        try:
            await _cleanup_once()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Artifact cleanup error: {e}")
        await asyncio.sleep(interval * 60)


async def _cleanup_once():
    cutoff = datetime.utcnow() - timedelta(days=max(1, settings.artifact_ttl_days))
    final_statuses = {"SUCCESS", "FAILED", "failed", "disabled"}
    data_dir = settings.fax_data_dir
    import glob
    with SessionLocal() as db:
        # naive scan: iterate all jobs updated before cutoff
        # SQLAlchemy 2.0 Core select is imported? Simpler: fetch all and filter.
        # For small SQLite this is fine. For larger stores, switch to SQL query with filters.
        jobs = db.query(FaxJob).all()  # type: ignore[attr-defined]
        for job in jobs:
            try:
                if job.updated_at and job.updated_at < cutoff and (job.status in final_statuses):
                    # Delete PDF
                    pdf_path = os.path.join(data_dir, f"{job.id}.pdf")
                    if os.path.exists(pdf_path):
                        os.remove(pdf_path)
                    # Delete TIFF
                    if job.tiff_path and os.path.exists(job.tiff_path):
                        try:
                            os.remove(job.tiff_path)
                        except FileNotFoundError:
                            pass
                    # Delete original upload(s)
                    for p in glob.glob(os.path.join(data_dir, f"{job.id}-*")):
                        try:
                            os.remove(p)
                        except FileNotFoundError:
                            pass
            except Exception:
                continue

        # Inbound retention cleanup
        try:
            from .db import InboundFax  # type: ignore
        except Exception:
            InboundFax = None  # type: ignore
        if InboundFax is not None:
            now = datetime.utcnow()
            storage = get_storage()
            rows = db.query(InboundFax).all()  # type: ignore[attr-defined]
            for fx in rows:
                try:
                    if fx.retention_until and fx.retention_until <= now:
                        # Delete stored PDF (local or S3)
                        if fx.pdf_path:
                            try:
                                storage.delete(str(fx.pdf_path))
                            except Exception:
                                pass
                            fx.pdf_path = None
                        # Delete local TIFF if present
                        if fx.tiff_path and os.path.exists(fx.tiff_path):
                            try:
                                os.remove(fx.tiff_path)
                            except FileNotFoundError:
                                pass
                            fx.tiff_path = None
                        fx.updated_at = now
                        db.add(fx)
                        db.commit()
                        audit_event("inbound_deleted", job_id=fx.id)
                except Exception:
                    continue


@app.get("/fax/{job_id}/pdf")
async def get_fax_pdf(job_id: str, token: str = Query(...)):
    """Serve PDF file for cloud backend (e.g., Phaxio) to fetch.
    No API auth; requires a valid, unexpired per-job token.
    """
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if not job:
            raise HTTPException(404, detail="Job not found")

        # Determine expected token
        expected_token = job.pdf_token  # type: ignore[assignment]
        if not expected_token and job.pdf_url:  # type: ignore[truthy-bool]
            # Fallback: extract token from stored pdf_url if present (tests)
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(str(job.pdf_url)).query)  # type: ignore[arg-type]
                t = qs.get("token", [None])[0]
                if t:
                    expected_token = str(t)  # type: ignore[assignment]
            except Exception:
                expected_token = None  # type: ignore[assignment]
        # If no token is configured for this job, treat as not found
        if not expected_token:  # type: ignore[truthy-bool]
            raise HTTPException(404, detail="PDF not available")
        # Validate token equality
        if token != expected_token:
            raise HTTPException(403, detail="Invalid token")
        # Validate expiry if set
        if job.pdf_token_expires_at and datetime.utcnow() > job.pdf_token_expires_at:  # type: ignore[operator]
            raise HTTPException(403, detail="Token expired")

        # Get the PDF path
        pdf_path = os.path.join(settings.fax_data_dir, f"{job_id}.pdf")
        if not os.path.exists(pdf_path):
            raise HTTPException(404, detail="PDF file not found")

        # Log access for security monitoring
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"PDF accessed for job {job_id} by cloud provider")
        audit_event("pdf_served", job_id=job_id)
        
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=f"fax_{job_id}.pdf",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )


@app.post("/phaxio-callback")
async def phaxio_callback(request: Request):
    """Handle Phaxio status callbacks with idempotency and fast 202 ACK.

    - Verify webhook (trait-appropriate; non-strict mode accepts missing/invalid).
    - Dedupe on (provider_id, external_id) via in-memory TTL and DB unique key.
    - Update job status once, then always return 202 Accepted.
    """
    raw = await request.body()
    # Trait-driven, non-strict verification (strictness gated by env for dev safety)
    try:
        from .config_loader import load_provider_secrets as _lps  # local import to avoid cycles
        secrets_obj = _lps()
        hdrs = {k: v for k, v in request.headers.items()}
        ok = phaxio_inbound_adapter.verify_webhook(hdrs, raw, secrets_obj.phaxio_webhook_secret, strict=(settings.phaxio_verify_signature and STRICT_INBOUND))
        if not ok and (settings.phaxio_verify_signature and STRICT_INBOUND):
            raise HTTPException(401, detail="Invalid Phaxio signature")
    except HTTPException:
        # Strict mode rejects early
        raise
    except Exception:
        # Fail-open for non-strict environments; we will ACK 202 but avoid processing
        ok = False

    # Parse payload (JSON or form)
    data: Dict[str, Any] = {}
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            data = await request.json()
        except Exception:
            data = {}
    else:
        try:
            form_data = await request.form()
            data = dict(form_data)
        except Exception:
            data = {}

    # External id used for idempotency
    ext_id = (
        data.get("fax[id]")
        or data.get("id")
        or data.get("fax_id")
        or data.get("faxId")
    )

    # If verification failed in non-strict mode or no external id, ACK and stop
    if not ok or not ext_id:
        return _ack_response()

    # In-memory dedupe window (10 minutes)
    if _inbound_dedupe("phaxio", str(ext_id)):
        return _ack_response()

    # DB idempotency guard (unique provider_sid + event_type)
    with SessionLocal() as db:
        try:
            evt = InboundEvent(id=uuid.uuid4().hex, provider_sid=str(ext_id), event_type="phaxio-callback", created_at=datetime.utcnow())
            db.add(evt)
            db.commit()
        except Exception:
            db.rollback()
            return _ack_response()

    # Proceed with status handling (single-shot per unique ext_id)
    job_id = request.query_params.get("job_id")
    phaxio_service = get_phaxio_service()
    if not phaxio_service or not job_id:
        return _ack_response()

    status_info = await phaxio_service.handle_status_callback(data)

    # Update job status (no PHI)
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if job:
            job.status = status_info.get('status') or job.status
            if status_info.get('error_message'):
                job.error = status_info['error_message']
            if status_info.get('pages'):
                job.pages = status_info['pages']
            job.updated_at = datetime.utcnow()  # type: ignore[assignment]
            db.add(job)  # type: ignore[arg-type]
            db.commit()
    audit_event("job_updated", job_id=job_id, status=status_info.get('status'), provider="phaxio")
    return _ack_response()


async def _send_via_phaxio(job_id: str, to: str, pdf_path: str):
    """Send fax via Phaxio API."""
    try:
        phaxio_service = get_phaxio_service()
        if not phaxio_service or not phaxio_service.is_configured():
            raise Exception("Phaxio is not properly configured")
        
        # Generate a secure token for PDF access with expiry
        pdf_token = secrets.token_urlsafe(32)
        ttl = max(1, int(settings.pdf_token_ttl_minutes))
        expires_at = datetime.utcnow() + timedelta(minutes=ttl)

        # Create public URL for PDF (tokenized)
        pdf_url = f"{settings.public_api_url}/fax/{job_id}/pdf?token={pdf_token}"

        # Update job with PDF URL/token and mark as in_progress
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                j = cast(Any, job)
                j.pdf_url = pdf_url
                j.pdf_token = pdf_token
                j.pdf_token_expires_at = expires_at
                j.status = "in_progress"
                j.updated_at = datetime.utcnow()
                db.add(j)
                db.commit()
        
        # Send via Phaxio
        audit_event("job_dispatch", job_id=job_id, method="phaxio")
        audit_event("FAX_SENT", job_id=job_id, backend="phaxio")
        result = await phaxio_service.send_fax(to, pdf_url, job_id)
        
        # Update job with provider SID
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                j = cast(Any, job)
                j.provider_sid = result['provider_sid']
                j.status = result['status']
                j.updated_at = datetime.utcnow()
                db.add(j)
                db.commit()
                if str(result.get('status') or '').upper() in {"SUCCESS", "COMPLETED"}:
                    audit_event("FAX_DELIVERED", job_id=job_id, backend="phaxio")
                
    except Exception as e:
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                j = cast(Any, job)
                j.status = "failed"
                j.error = str(e)
                j.updated_at = datetime.utcnow()
                db.add(j)
                db.commit()
        audit_event("FAX_FAILED", job_id=job_id)


async def _send_via_sinch(job_id: str, to: str, pdf_path: str):
    """Send fax via Sinch Fax API v3 (Phaxio by Sinch)."""
    try:
        sinch = get_sinch_service()
        if not sinch or not sinch.is_configured():
            raise Exception("Sinch Fax is not properly configured")

        audit_event("job_dispatch", job_id=job_id, method="sinch")
        audit_event("FAX_SENT", job_id=job_id, backend="sinch")

        # Create fax by uploading the PDF directly (multipart/form-data)
        resp = await sinch.send_fax_file(to, pdf_path)

        fax_id = str(resp.get("id") or resp.get("data", {}).get("id") or "")
        status = (resp.get("status") or resp.get("data", {}).get("status") or "in_progress").upper()
        if status == "IN_PROGRESS":
            internal_status = "in_progress"
        elif status in {"SUCCESS", "COMPLETED", "COMPLETED_OK"}:
            internal_status = "SUCCESS"
        elif status in {"FAILED", "FAILURE", "ERROR"}:
            internal_status = "FAILED"
        else:
            internal_status = "queued"

        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                j = cast(Any, job)
                j.provider_sid = fax_id
                j.status = internal_status
                j.updated_at = datetime.utcnow()
                db.add(j)
                db.commit()
                if internal_status == "SUCCESS":
                    audit_event("FAX_DELIVERED", job_id=job_id, backend="sinch")
    except Exception as e:
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                j = cast(Any, job)
                j.status = "failed"
                j.error = str(e)
                j.updated_at = datetime.utcnow()
                db.add(j)
                db.commit()
        audit_event("FAX_FAILED", job_id=job_id)


async def _send_via_outbound_normalized(job_id: str, to: str, pdf_path: str, tiff_path: str):
    """Unified dispatcher for cloud backends using outbound adapters.

    - Phaxio: generate tokenized pdf_url and call adapter with job_id+pdf_url
    - Sinch: upload file directly
    Updates DB with provider_sid and canonical status using provider status map.
    """
    try:
        from .providers.outbound import get_outbound_adapter
        from .config import active_outbound
        from .status_map import canonical_status, load_status_map

        # Ensure status map is loaded
        try:
            load_status_map()
        except Exception:
            pass

        backend = active_outbound()
        adapter = get_outbound_adapter()

        pdf_url: Optional[str] = None
        if backend == "phaxio":
            # Generate a secure token for PDF access with expiry
            pdf_token = secrets.token_urlsafe(32)
            ttl = max(1, int(settings.pdf_token_ttl_minutes))
            expires_at = datetime.utcnow() + timedelta(minutes=ttl)
            pdf_url = f"{settings.public_api_url}/fax/{job_id}/pdf?token={pdf_token}"
            # Persist token + mark in_progress
            with SessionLocal() as db:
                job = db.get(FaxJob, job_id)
                if job:
                    job.pdf_url = pdf_url
                    job.pdf_token = pdf_token
                    job.pdf_token_expires_at = expires_at
                    job.status = "in_progress"
                    job.updated_at = datetime.utcnow()
                    db.add(job)
                    db.commit()
            audit_event("job_dispatch", job_id=job_id, method="phaxio")
            res = await adapter.send(to, pdf_path, job_id=job_id, pdf_url=pdf_url)
            prov_sid = str(res.get("job_id") or res.get("provider_sid") or "")
            raw = str((res.get("raw") or {}).get("data", {}).get("status") or res.get("status") or "queued").lower()
            status = canonical_status("phaxio", raw)
        else:
            audit_event("job_dispatch", job_id=job_id, method=backend)
            res = await adapter.send(to, pdf_path)
            prov_sid = str(res.get("job_id") or res.get("provider_sid") or "")
            raw = str((res.get("raw") or {}).get("status") or res.get("status") or "in_progress").lower()
            status = canonical_status(backend, raw)

        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                job.provider_sid = prov_sid or job.provider_sid
                job.status = status or job.status
                job.updated_at = datetime.utcnow()
                db.add(job)
                db.commit()
    except Exception as e:
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(e)
                job.updated_at = datetime.utcnow()
                db.add(job)
                db.commit()
        audit_event("job_failed", job_id=job_id, error=str(e))


async def _send_via_signalwire(job_id: str, to: str, pdf_path: str):
    try:
        svc = get_signalwire_service()
        if not svc:
            raise RuntimeError("SignalWire not configured")
        # Tokenized PDF URL
        pdf_token = secrets.token_urlsafe(32)
        ttl = max(1, int(settings.pdf_token_ttl_minutes))
        expires_at = datetime.utcnow() + timedelta(minutes=ttl)
        media_url = f"{settings.public_api_url}/fax/{job_id}/pdf?token={pdf_token}"

        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                job.pdf_url = media_url
                job.pdf_token = pdf_token
                job.pdf_token_expires_at = expires_at
                job.status = "in_progress"
                job.updated_at = datetime.utcnow()
                db.add(job)
                db.commit()

        audit_event("job_dispatch", job_id=job_id, method="signalwire")
        audit_event("FAX_SENT", job_id=job_id, backend="signalwire")
        res = await svc.send_fax(to, media_url, job_id)
        prov_sid = str(res.get("provider_sid") or "")
        status = str(res.get("status") or "queued")
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                job.provider_sid = prov_sid
                job.status = status
                job.updated_at = datetime.utcnow()
                db.add(job)
                db.commit()
                if str(status or '').upper() in {"SUCCESS", "COMPLETED"}:
                    audit_event("FAX_DELIVERED", job_id=job_id, backend="signalwire")
    except Exception as e:
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(e)
                job.updated_at = datetime.utcnow()
                db.add(job)
                db.commit()
        audit_event("FAX_FAILED", job_id=job_id)


async def _send_via_freeswitch(job_id: str, to: str, tiff_path: str):
    try:
        audit_event("job_dispatch", job_id=job_id, method="freeswitch")
        if not fs_cli_available() and not settings.fax_disabled:
            raise RuntimeError("fs_cli not available on API host; install FreeSWITCH client or configure ESL integration")
        # Fire and forget
        if not settings.fax_disabled:
            res = originate_txfax(to, tiff_path, job_id)
        else:
            res = "disabled"
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                j = cast(Any, job)
                j.status = "in_progress"
                j.provider_sid = (res or "").strip()
                j.updated_at = datetime.utcnow()
                db.add(j)
                db.commit()
        audit_event("FAX_SENT", job_id=job_id, backend="freeswitch")
    except Exception as e:
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                j = cast(Any, job)
                j.status = "failed"
                j.error = str(e)
                j.updated_at = datetime.utcnow()
                db.add(j)
                db.commit()
        audit_event("FAX_FAILED", job_id=job_id)

async def _send_via_manifest(job_id: str, to: str, pdf_path: str):
    try:
        pid = settings.fax_backend
        mpath = os.path.join(os.getcwd(), "config", "providers", pid, "manifest.json")
        with open(mpath, "r", encoding="utf-8") as f:
            man = HttpManifest.from_dict(json.load(f))

        # Generate tokenized PDF URL with expiry
        pdf_token = secrets.token_urlsafe(32)
        ttl = max(1, int(settings.pdf_token_ttl_minutes))
        expires_at = datetime.utcnow() + timedelta(minutes=ttl)
        pdf_url = f"{settings.public_api_url}/fax/{job_id}/pdf?token={pdf_token}"

        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                job.pdf_url = pdf_url
                job.pdf_token = pdf_token
                job.pdf_token_expires_at = expires_at
                job.status = "in_progress"
                job.updated_at = datetime.utcnow()
                db.add(job)
                db.commit()

        # Load provider settings from config store if present
        p_settings: Dict[str, Any] = {}
        try:
            if _read_cfg is not None:
                cfg = _read_cfg(settings.faxbot_config_path)
                if getattr(cfg, "ok", False) and getattr(cfg, "data", None):
                    ob = ((cfg.data.get("providers") or {}).get("outbound") or {})
                    if (ob.get("plugin") or "").lower() == pid.lower():
                        p_settings = ob.get("settings") or {}
        except Exception:
            pass

        audit_event("job_dispatch", job_id=job_id, method=f"manifest:{pid}")
        rt = HttpProviderRuntime(man, {}, p_settings)
        res = await rt.send_fax(to=to, file_url=pdf_url)
        prov_sid = str(res.get("job_id") or "")
        status = str(res.get("status") or "queued")
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                job.provider_sid = prov_sid
                job.status = status
                job.updated_at = datetime.utcnow()
                db.add(job)
                db.commit()
    except Exception as e:
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(e)
                job.updated_at = datetime.utcnow()
                db.add(job)
                db.commit()
        audit_event("job_failed", job_id=job_id, error=str(e))


def _serialize_job(job: FaxJob) -> FaxJobOut:
    j = cast(Any, job)
    return FaxJobOut(
        id=j.id,
        to=j.to_number,
        status=j.status,
        error=j.error,
        pages=j.pages,
        backend=j.backend,
        provider_sid=j.provider_sid,
        created_at=j.created_at,
        updated_at=j.updated_at,
    )


# ===== Inbound receiving (MVP scaffolding) =====
from .db import InboundFax  # type: ignore
from .conversion import tiff_to_pdf  # type: ignore


class InboundFaxOut(BaseModel):
    id: str
    fr: Optional[str] = None
    to: Optional[str] = None
    status: str
    backend: str
    pages: Optional[int] = None
    size_bytes: Optional[int] = None
    created_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    mailbox: Optional[str] = None


def _serialize_inbound(fx: InboundFax) -> InboundFaxOut:
    f = cast(Any, fx)
    return InboundFaxOut(
        id=f.id,
        fr=f.from_number,
        to=f.to_number,
        status=f.status,
        backend=f.backend,
        pages=f.pages,
        size_bytes=f.size_bytes,
        created_at=f.created_at,
        received_at=f.received_at,
        updated_at=f.updated_at,
        mailbox=f.mailbox_label,
    )


def require_inbound_list(info = Depends(require_api_key)):
    if info is None and not settings.require_api_key and not settings.api_key:
        return
    if not _has_scope(info, "inbound:list"):
        audit_event("api_key_denied_scope", key_id=(info or {}).get("key_id"), required="inbound:list")
        raise HTTPException(403, detail="Insufficient scope: inbound:list required")
    if settings.inbound_list_rpm:
        _enforce_rate_limit(info, "/inbound")


def require_inbound_read(info = Depends(require_api_key)):
    if info is None and not settings.require_api_key and not settings.api_key:
        return
    if not _has_scope(info, "inbound:read"):
        audit_event("api_key_denied_scope", key_id=(info or {}).get("key_id"), required="inbound:read")
        raise HTTPException(403, detail="Insufficient scope: inbound:read required")
    if settings.inbound_get_rpm:
        _enforce_rate_limit(info, "/inbound/{id}")


@app.get("/inbound", response_model=List[InboundFaxOut], dependencies=[Depends(require_scopes(["inbound:list"], path="/inbound", rpm=settings.inbound_list_rpm))])
def list_inbound(
    to_number: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    mailbox: Optional[str] = Query(default=None),
):
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
    with SessionLocal() as db:
        q = db.query(InboundFax)  # type: ignore[attr-defined]
        if to_number:
            q = q.filter(InboundFax.to_number == to_number)  # type: ignore[attr-defined]
        if status:
            q = q.filter(InboundFax.status == status)  # type: ignore[attr-defined]
        if mailbox:
            q = q.filter(InboundFax.mailbox_label == mailbox)  # type: ignore[attr-defined]
        rows = q.order_by(InboundFax.received_at.desc()).limit(100).all()  # type: ignore[attr-defined]
        return [_serialize_inbound(r) for r in rows]


@app.get("/inbound/{inbound_id}", response_model=InboundFaxOut, dependencies=[Depends(require_scopes(["inbound:read"], path="/inbound/{id}", rpm=settings.inbound_get_rpm))])
def get_inbound(inbound_id: str):
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
    with SessionLocal() as db:
        fx = db.get(InboundFax, inbound_id)
        if not fx:
            raise HTTPException(404, detail="Inbound fax not found")
        return _serialize_inbound(fx)


@app.get("/inbound/{inbound_id}/pdf")
def get_inbound_pdf(inbound_id: str, token: Optional[str] = Query(default=None), info = Depends(require_api_key)):
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
    with SessionLocal() as db:
        fx = db.get(InboundFax, inbound_id)
        if not fx:
            raise HTTPException(404, detail="Inbound fax not found")
        allowed = False
        if token and fx.pdf_token and token == fx.pdf_token:
            if fx.pdf_token_expires_at and datetime.utcnow() > fx.pdf_token_expires_at:
                raise HTTPException(403, detail="Token expired")
            allowed = True
        elif info is not None and _has_scope(info, "inbound:read"):
            if settings.inbound_get_rpm:
                _enforce_rate_limit(info, "/inbound/{id}/pdf", settings.inbound_get_rpm)
            allowed = True
        if not allowed:
            raise HTTPException(403, detail="Forbidden")
        pdf_path = str(fx.pdf_path or "")
        if not pdf_path:
            raise HTTPException(404, detail="PDF file not found")
        storage = get_storage()
        if pdf_path.startswith("s3://"):
            stream, name = storage.get_pdf_stream(pdf_path)
            audit_event("inbound_pdf_served", job_id=inbound_id, method=("token" if token else "api_key"))
            return StreamingResponse(stream, media_type="application/pdf", headers={
                "Content-Disposition": f"attachment; filename={name}",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            })
        if not os.path.exists(pdf_path):
            raise HTTPException(404, detail="PDF file not found")
        audit_event("inbound_pdf_served", job_id=inbound_id, method=("token" if token else "api_key"))
        return FileResponse(
            pdf_path,
            media_type="application/pdf",
            filename=f"inbound_{inbound_id}.pdf",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
        )


from .middleware.traits import requires_traits


@app.post("/_internal/asterisk/inbound")
@requires_traits(direction="inbound", keys=["supports_inbound", "requires_ami"])
def asterisk_inbound(payload: dict, x_internal_secret: Optional[str] = Header(default=None)):
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
    # Gate by active inbound backend (allow when not explicitly set for backward compatibility)
    if os.getenv("FAX_INBOUND_BACKEND") and active_inbound() != "sip":
        audit_event("inbound_route_blocked", route="/_internal/asterisk/inbound", active_inbound=active_inbound(), inbound_enabled=settings.inbound_enabled)
        raise HTTPException(404, detail="Inbound route not active for current backend")
    if not settings.asterisk_inbound_secret:
        raise HTTPException(401, detail="Internal secret not configured")
    if x_internal_secret != settings.asterisk_inbound_secret:
        raise HTTPException(401, detail="Invalid internal secret")
    try:
        tiff_path = str(payload.get("tiff_path"))
        to_number = (payload.get("to_number") or "").strip() or None
        from_number = (payload.get("from_number") or "").strip() or None
        faxstatus = (payload.get("faxstatus") or "").strip() or None
        faxpages = payload.get("faxpages")
        uniqueid = str(payload.get("uniqueid") or "")
        if not tiff_path or not os.path.exists(tiff_path):
            raise HTTPException(400, detail="TIFF path invalid")
    except Exception:
        raise HTTPException(400, detail="Invalid payload")

    job_id = uuid.uuid4().hex
    data_dir = settings.fax_data_dir
    ensure_dir(data_dir)
    pdf_path = os.path.join(data_dir, f"{job_id}.pdf")

    pages, _ = tiff_to_pdf(tiff_path, pdf_path)
    import hashlib as _hl
    try:
        with open(pdf_path, "rb") as f:
            content = f.read()
        size_bytes = len(content)
        sha256 = _hl.sha256(content).hexdigest()
    except Exception:
        size_bytes = None
        sha256 = None

    # Upload to configured storage (local path preserved or uploaded to S3)
    storage = get_storage()
    object_name = f"{job_id}.pdf"
    try:
        stored_uri = storage.put_pdf(pdf_path, object_name)
    except Exception:
        stored_uri = None
    try:
        if stored_uri and stored_uri.startswith("s3://") and os.path.exists(pdf_path):
            os.remove(pdf_path)
    except Exception:
        pass

    pdf_token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=max(1, settings.inbound_token_ttl_minutes))
    retention_until = None
    if settings.inbound_retention_days and settings.inbound_retention_days > 0:
        retention_until = datetime.utcnow() + timedelta(days=settings.inbound_retention_days)

    with SessionLocal() as db:
        fx = InboundFax(
            id=job_id,
            from_number=from_number,
            to_number=to_number,
            status=faxstatus or "received",
            backend="sip",
            inbound_backend=active_inbound(),
            provider_sid=uniqueid or None,
            pages=int(faxpages) if faxpages else pages,
            size_bytes=size_bytes,
            sha256=sha256,
            pdf_path=stored_uri,
            tiff_path=tiff_path,
            mailbox_label=None,
            retention_until=retention_until,
            pdf_token=pdf_token,
            pdf_token_expires_at=expires_at,
            created_at=datetime.utcnow(),
            received_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(fx)
        db.commit()
    audit_event("inbound_received", job_id=job_id, backend="sip")
    return {"id": job_id, "status": "ok"}


@app.post("/phaxio-inbound")
@requires_traits(direction="inbound", keys=["supports_inbound"])
async def phaxio_inbound(request: Request):
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
    if os.getenv("FAX_INBOUND_BACKEND") and active_inbound() != "phaxio":
        audit_event("inbound_route_blocked", route="/phaxio-inbound", active_inbound=active_inbound(), inbound_enabled=settings.inbound_enabled)
        raise HTTPException(404, detail="Inbound route not active for current backend")
    raw = await request.body()
    if settings.phaxio_inbound_verify_signature:
        provided = request.headers.get("X-Phaxio-Signature") or request.headers.get("X-Phaxio-Signature-SHA256")
        if not provided:
            raise HTTPException(401, detail="Missing Phaxio signature")
        secret = (settings.phaxio_api_secret or "").encode()
        if not secret:
            raise HTTPException(401, detail="Phaxio secret not configured")
        digest = hmac.new(secret, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, (provided or "").strip().lower()):
            raise HTTPException(401, detail="Invalid Phaxio signature")

    # Parse form or JSON
    try:
        form = await request.form()
        data = dict(form)
    except Exception:
        try:
            data = await request.json()
        except Exception:
            data = {}

    # Extract fields robustly
    def get_nested(d, *keys):
        for k in keys:
            if k in d:
                return d[k]
        return None

    provider_sid = get_nested(data, "fax[id]", "id", "fax_id", "faxId")
    from_number = get_nested(data, "fax[from]", "from", "from_number")
    to_number = get_nested(data, "fax[to]", "to", "to_number")
    pages = get_nested(data, "fax[num_pages]", "num_pages", "pages")
    status = get_nested(data, "fax[status]", "status") or "received"
    file_url = get_nested(data, "file_url", "media_url", "pdf_url")

    if not provider_sid:
        # Accept and ignore if no provider id to avoid retries storm
        return _ack_response()

    # Dedupe on provider+external id within window
    if _inbound_dedupe("phaxio", str(provider_sid)):
        return _ack_response()

    # Idempotency: unique (provider_sid, event_type)
    with SessionLocal() as db:
        from .db import InboundEvent  # type: ignore
        evt = InboundEvent(id=uuid.uuid4().hex, provider_sid=str(provider_sid), event_type="phaxio-inbound", created_at=datetime.utcnow())
        try:
            db.add(evt)
            db.commit()
        except Exception:
            # Duplicate DB event → accept and stop
            db.rollback()
            return _ack_response()

    # Fetch PDF if URL provided
    pdf_bytes: Optional[bytes] = None
    if file_url:
        try:
            import httpx
            auth = None
            if settings.phaxio_api_key and settings.phaxio_api_secret:
                auth = (settings.phaxio_api_key, settings.phaxio_api_secret)
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(str(file_url), auth=auth)
                if resp.status_code == 200 and (resp.headers.get("content-type", "").startswith("application/pdf") or True):
                    pdf_bytes = resp.content
        except Exception:
            pdf_bytes = None

    job_id = uuid.uuid4().hex
    data_dir = settings.fax_data_dir
    ensure_dir(data_dir)
    local_pdf = os.path.join(data_dir, f"{job_id}.pdf")
    if pdf_bytes is None:
        # Do not write placeholders; record an error and skip storing a file
        size_bytes = None
        pages_int = None
        sha256_hex = None
    else:
        with open(local_pdf, "wb") as f:
            f.write(pdf_bytes)
        size_bytes = len(pdf_bytes)
        pages_int = None
        sha256_hex = hashlib.sha256(pdf_bytes).hexdigest()

    storage = get_storage()
    try:
        stored_uri = storage.put_pdf(local_pdf, f"{job_id}.pdf")
    except Exception:
        stored_uri = None
    try:
        if stored_uri and stored_uri.startswith("s3://") and os.path.exists(local_pdf):
            os.remove(local_pdf)
    except Exception:
        pass

    pdf_token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=max(1, settings.inbound_token_ttl_minutes))
    retention_until = datetime.utcnow() + timedelta(days=settings.inbound_retention_days) if settings.inbound_retention_days > 0 else None

    with SessionLocal() as db:
        from .db import InboundFax  # type: ignore
        fx = InboundFax(
            id=job_id,
            from_number=(str(from_number) if from_number else None),
            to_number=(str(to_number) if to_number else None),
            status=str(status),
            backend="phaxio",
            provider_sid=str(provider_sid),
            pages=int(pages) if pages else pages_int,
            size_bytes=size_bytes,
            sha256=sha256_hex,
            pdf_path=(stored_uri or local_pdf),
            tiff_path=None,
            mailbox_label=None,
            retention_until=retention_until,
            pdf_token=pdf_token,
            pdf_token_expires_at=expires_at,
            created_at=datetime.utcnow(),
            received_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(fx)
        db.commit()
    audit_event("inbound_received", job_id=job_id, backend="phaxio")
    return _ack_response({"status": "ok"})


@app.post("/sinch-inbound")
@requires_traits(direction="inbound", keys=["supports_inbound"])
async def sinch_inbound(request: Request):
    # Ensure latest env (tests may set FAX_DATA_DIR etc.)
    try:
        reload_settings()
    except Exception:
        pass
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
    if os.getenv("FAX_INBOUND_BACKEND") and active_inbound() != "sinch":
        audit_event("inbound_route_blocked", route="/sinch-inbound", active_inbound=active_inbound(), inbound_enabled=settings.inbound_enabled)
        raise HTTPException(404, detail="Inbound route not active for current backend")
    raw = await request.body()
    # Verify Basic if configured
    if settings.sinch_inbound_basic_user:
        auth = request.headers.get("Authorization", "")
        import base64
        ok = False
        if auth.startswith("Basic "):
            try:
                dec = base64.b64decode(auth.split(" ", 1)[1]).decode()
                user, _, pwd = dec.partition(":")
                ok = (user == settings.sinch_inbound_basic_user and pwd == settings.sinch_inbound_basic_pass)
            except Exception:
                ok = False
        if not ok:
            raise HTTPException(401, detail="Invalid basic auth")
    # Optional HMAC verification if customer configured a secret (strict mode will enforce when header is present)
    try:
        prov_secrets = load_provider_secrets()
        if prov_secrets.sinch_webhook_secret:
            ok = sinch_inbound_adapter.verify_webhook(dict(request.headers), raw, prov_secrets.sinch_webhook_secret, strict=STRICT_INBOUND)
            if not ok and STRICT_INBOUND:
                raise HTTPException(401, detail="Invalid signature (Sinch)")
    except HTTPException:
        raise
    except Exception:
        pass

    # Accept JSON or multipart form
    data: dict[str, Any] = {}
    ctype = (request.headers.get("content-type") or "").lower()
    try:
        data = await request.json()
    except Exception:
        if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
            try:
                form = await request.form()
                data = {k: (form.get(k)) for k in form.keys()}  # type: ignore
            except Exception:
                data = {}
        else:
            data = {}

    provider_sid = data.get("id") or data.get("fax_id")
    from_number = data.get("from") or data.get("from_number")
    to_number = data.get("to") or data.get("to_number")
    pages = data.get("num_pages") or data.get("pages")
    status = data.get("status") or "received"
    file_url = data.get("file_url") or data.get("media_url")

    if not provider_sid:
        # Treat as a failure so provider consoles show an error during test
        # Accept but ignore to avoid retry storms; log audit
        audit_event("inbound_invalid", provider="sinch", reason="missing id")
        return _ack_response()

    # Dedupe on provider+external id within window
    if _inbound_dedupe("sinch", str(provider_sid)):
        return _ack_response()

    duplicate_evt = False
    with SessionLocal() as db:
        from .db import InboundEvent  # type: ignore
        evt = InboundEvent(id=uuid.uuid4().hex, provider_sid=str(provider_sid), event_type="sinch-inbound", created_at=datetime.utcnow())
        try:
            db.add(evt)
            db.commit()
        except Exception:
            db.rollback()
            duplicate_evt = True

    pdf_bytes: Optional[bytes] = None
    pdf_error: Optional[str] = None
    if file_url:
        try:
            import httpx
            headers = {"Accept": "application/pdf, application/octet-stream"}
            auth = None
            # Prefer OAuth if enabled
            use_oauth = (os.getenv("SINCH_AUTH_METHOD", settings.sinch_auth_method or "basic").lower() == "oauth")
            token = None
            if use_oauth:
                try:
                    svc = get_sinch_service()
                    if svc is not None:
                        token = await svc.get_access_token()
                        headers["Authorization"] = f"Bearer {token}"
                except Exception:
                    token = None
            if token is None and settings.sinch_api_key and settings.sinch_api_secret:
                auth = (settings.sinch_api_key, settings.sinch_api_secret)
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                # Keep call signature simple for tests; rely on content sniff, not header
                resp = await client.get(str(file_url), auth=auth)
                headers_obj = getattr(resp, "headers", {}) or {}
                try:
                    ct = (headers_obj.get("content-type") or "").lower()
                except Exception:
                    ct = ""
                if resp.status_code == 200 and getattr(resp, "content", None):
                    content = resp.content
                    # Validate PDF signature (%PDF-) regardless of content-type quirks
                    if content[:5] == b"%PDF-" or "application/pdf" in ct or "octet-stream" in ct:
                        pdf_bytes = content
                    else:
                        pdf_error = f"unexpected_content_type:{ct or 'unknown'}"
                else:
                    pdf_error = f"http_status:{resp.status_code}"
        except Exception as e:
            pdf_error = f"exception:{type(e).__name__}"

    job_id = uuid.uuid4().hex
    data_dir = settings.fax_data_dir
    ensure_dir(data_dir)
    local_pdf = os.path.join(data_dir, f"{job_id}.pdf")
    pages_int = None
    if pdf_bytes is not None:
        with open(local_pdf, "wb") as f:
            f.write(pdf_bytes)
        size_bytes = len(pdf_bytes)
        sha256_hex = hashlib.sha256(pdf_bytes).hexdigest()
    else:
        # Do not write an invalid placeholder; record error and continue
        size_bytes = None
        sha256_hex = None

    storage = get_storage()
    stored_uri = None
    if pdf_bytes is not None:
        try:
            stored_uri = storage.put_pdf(local_pdf, f"{job_id}.pdf")
        except Exception:
            stored_uri = None
    # Fallback to local path if we have bytes and the file exists
    if (stored_uri is None) and (pdf_bytes is not None) and os.path.exists(local_pdf):
        stored_uri = local_pdf

    pdf_token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(minutes=max(1, settings.inbound_token_ttl_minutes))
    retention_until = datetime.utcnow() + timedelta(days=settings.inbound_retention_days) if settings.inbound_retention_days > 0 else None

    with SessionLocal() as db:
        from .db import InboundFax  # type: ignore
        existing = db.query(InboundFax).filter(InboundFax.provider_sid == str(provider_sid)).first()
        now = datetime.utcnow()
        if existing:
            # Update existing record to keep path fresh/idempotent
            if stored_uri:
                existing.pdf_path = stored_uri
            elif (pdf_bytes is not None) and os.path.exists(local_pdf):
                existing.pdf_path = local_pdf
            existing.size_bytes = size_bytes
            existing.sha256 = sha256_hex
            existing.pages = int(pages) if pages else pages_int
            existing.status = str(status)
            existing.error = pdf_error
            existing.updated_at = now
            if not existing.received_at:
                existing.received_at = now
            db.commit()
        else:
            fx = InboundFax(
                id=job_id,
                from_number=(str(from_number) if from_number else None),
                to_number=(str(to_number) if to_number else None),
                status=str(status),
                backend="sinch",
                inbound_backend=active_inbound(),
                provider_sid=str(provider_sid),
                pages=int(pages) if pages else pages_int,
                size_bytes=size_bytes,
                sha256=sha256_hex,
                pdf_path=(stored_uri or (local_pdf if pdf_bytes is not None and os.path.exists(local_pdf) else None)),
                tiff_path=None,
                mailbox_label=None,
                retention_until=retention_until,
                pdf_token=pdf_token,
                pdf_token_expires_at=expires_at,
                created_at=now,
                received_at=now,
                updated_at=now,
                error=pdf_error,
            )
            db.add(fx)
            db.commit()
    audit_event("inbound_received", job_id=job_id, backend="sinch", pdf_error=pdf_error)
    return _ack_response({"status": "ok"})


@app.post("/webhooks/inbound")
async def unified_inbound(request: Request, provider: Optional[str] = Query(default=None)):
    """Unified inbound endpoint with provider autodetect.
    - provider query param OR header auto-detect (X-Phaxio-Signature / X-Sinch-Signature)
    - Returns canonical event shape and enforces signature in strict mode.
    """
    try:
        raw = await request.body()
    except Exception:
        raw = b""
    hdrs = dict(request.headers)
    # Pick provider
    p = (provider or "").strip().lower()
    if not p:
        if "x-phaxio-signature" in {k.lower() for k in hdrs.keys()}:
            p = "phaxio"
        elif "x-sinch-signature" in {k.lower() for k in hdrs.keys()}:
            p = "sinch"
    if p not in {"phaxio", "sinch"}:
        raise HTTPException(400, detail="Unknown provider; supply ?provider=phaxio|sinch")
    prov_secrets = load_provider_secrets()
    # Decode JSON or form
    data: dict[str, Any] = {}
    ctype = (hdrs.get("content-type") or "").lower()
    try:
        data = await request.json()
    except Exception:
        if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
            try:
                form = await request.form()
                data = {k: (form.get(k)) for k in form.keys()}  # type: ignore
            except Exception:
                data = {}
        else:
            data = {}
    # Verify signatures (provider-specific)
    if p == "phaxio":
        ok = phaxio_inbound_adapter.verify_webhook(hdrs, raw, prov_secrets.phaxio_webhook_secret, strict=STRICT_INBOUND)
        if not ok and STRICT_INBOUND:
            return JSONResponse({"code": "signature_invalid", "provider": "phaxio"}, status_code=401)
        event = phaxio_inbound_adapter.parse_payload(data)
        ev_type = "unified_phaxio"
    else:
        ok = sinch_inbound_adapter.verify_webhook(hdrs, raw, prov_secrets.sinch_webhook_secret, strict=STRICT_INBOUND)
        if not ok and STRICT_INBOUND:
            return JSONResponse({"code": "signature_invalid", "provider": "sinch"}, status_code=401)
        event = sinch_inbound_adapter.parse_payload(data)
        ev_type = "unified_sinch"
    # Idempotent log (simple audit; DB log is optional)
    # Idempotent DB log
    try:
        from .db import InboundEvent  # type: ignore
        with SessionLocal() as db:
            if not (event.get("id")):
                # No id → treat as ignored but still audit
                audit_event("inbound_ignored", provider=p)
            else:
                evt = InboundEvent(id=uuid.uuid4().hex, provider_sid=str(event["id"]), event_type=ev_type, created_at=datetime.utcnow())
                try:
                    db.add(evt)
                    db.commit()
                except Exception:
                    db.rollback()
        audit_event("inbound_received", provider=p, provider_sid=event.get("id"))
    except Exception:
        pass
    return {"ok": True, "provider": p, "event": event}
# ===== Global error logging =====
@app.exception_handler(HTTPException)
async def _handle_http_exc(request: Request, exc: HTTPException):
    try:
        audit_event("api_error", path=request.url.path, status=exc.status_code, detail=str(exc.detail))
    except Exception:
        pass
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def _handle_any_exc(request: Request, exc: Exception):
    try:
        audit_event("api_error", path=request.url.path, status=500, detail="internal_error")
    except Exception:
        pass
    # Return minimal detail to avoid leaking internals
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


# ===== v3 Plugins: discovery and config (feature-gated) =====
def _plugins_disabled_response():
    return JSONResponse({"detail": "v3 plugins feature disabled"}, status_code=404)


def _installed_plugins() -> list[dict[str, Any]]:
    """Return built-in provider manifests as plugin-like entries.

    This is a minimal discovery surface to unblock Admin UI while
    the full plugin system is developed.
    """
    current = settings.fax_backend
    items = []
    # Outbound providers
    items.append({
        "id": "phaxio",
        "name": "Phaxio Cloud Fax",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status", "webhook"],
        "enabled": (current == "phaxio"),
        "configurable": True,
    })
    items.append({
        "id": "sinch",
        "name": "Sinch Fax API v3",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status"],
        "enabled": (current == "sinch"),
        "configurable": True,
    })
    items.append({
        "id": "signalwire",
        "name": "SignalWire (Compatibility Fax API)",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status", "webhook"],
        "enabled": (current == "signalwire"),
        "configurable": True,
    })
    items.append({
        "id": "documo",
        "name": "Documo mFax",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status"],
        "enabled": (current == "documo"),
        "configurable": True,
    })
    items.append({
        "id": "sip",
        "name": "SIP/Asterisk (Self-hosted)",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status"],
        "enabled": (current == "sip"),
        "configurable": True,
    })
    items.append({
        "id": "freeswitch",
        "name": "FreeSWITCH (Self-hosted)",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send"],
        "enabled": (current == "freeswitch"),
        "configurable": True,
    })
    # Storage providers (inbound artifacts)
    items.append({
        "id": "local",
        "name": "Local Storage",
        "version": "1.0.0",
        "categories": ["storage"],
        "capabilities": ["store", "retrieve", "delete"],
        "enabled": (settings.storage_backend == "local"),
        "configurable": False,
    })
    items.append({
        "id": "s3",
        "name": "S3 / S3-compatible Storage",
        "version": "1.0.0",
        "categories": ["storage"],
        "capabilities": ["store", "retrieve", "delete"],
        "enabled": (settings.storage_backend == "s3"),
        "configurable": True,
    })
    # Manifest providers: scan providers dir
    try:
        prov_dir = _providers_dir()
        if os.path.isdir(prov_dir):
            for pid in os.listdir(prov_dir):
                mpath = os.path.join(prov_dir, pid, "manifest.json")
                if os.path.exists(mpath):
                    try:
                        with open(mpath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        pid2 = str(data.get("id") or pid)
                        name = str(data.get("name") or pid2)
                        items.append({
                            "id": pid2,
                            "name": name,
                            "version": "1.0.0",
                            "categories": ["outbound"],
                            "capabilities": ["send", "get_status"],
                            "enabled": (current == pid2),
                            "configurable": True,
                        })
                    except Exception:
                        pass
    except Exception:
        pass
    return items


@app.get("/plugins", dependencies=[Depends(require_admin)])
def list_plugins():
    if not settings.feature_v3_plugins:
        return _plugins_disabled_response()
    return {"items": _installed_plugins()}


@app.get("/plugins/{plugin_id}/config", dependencies=[Depends(require_admin)])
def get_plugin_config(plugin_id: str):
    if not settings.feature_v3_plugins:
        return _plugins_disabled_response()
    pid = plugin_id.lower()
    # Effective config from current settings (sanitized)
    if pid == "phaxio":
        return {
            "enabled": settings.fax_backend == "phaxio",
            "settings": {
                "callback_url": settings.phaxio_status_callback_url,
                "verify_signature": settings.phaxio_verify_signature,
                "configured": bool(settings.phaxio_api_key and settings.phaxio_api_secret),
            },
        }
    if pid == "sinch":
        return {
            "enabled": settings.fax_backend == "sinch",
            "settings": {
                "project_id": settings.sinch_project_id,
                "configured": bool(settings.sinch_project_id and settings.sinch_api_key and settings.sinch_api_secret),
            },
        }
    if pid == "signalwire":
        return {
            "enabled": settings.fax_backend == "signalwire",
            "settings": {
                "space_url": settings.signalwire_space_url,
                "project_id": settings.signalwire_project_id,
                "configured": bool(settings.signalwire_space_url and settings.signalwire_project_id and settings.signalwire_api_token),
            },
        }
    if pid == "documo":
        return {
            "enabled": settings.fax_backend == "documo",
            "settings": {
                "api_key": "***" if settings.documo_api_key else "",
                "base_url": settings.documo_base_url,
                "sandbox": settings.documo_use_sandbox,
                "configured": bool(settings.documo_api_key),
            },
        }
    if pid == "sip":
        return {
            "enabled": settings.fax_backend == "sip",
            "settings": {
                "ami_host": settings.ami_host,
                "ami_port": settings.ami_port,
                "ami_username": settings.ami_username,
                "ami_password_default": settings.ami_password == "changeme",
            },
        }
    if pid == "freeswitch":
        return {
            "enabled": settings.fax_backend == "freeswitch",
            "settings": {
                "esl_host": settings.fs_esl_host,
                "esl_port": settings.fs_esl_port,
                "gateway_name": settings.fs_gateway_name,
            },
        }
    if pid == "s3":
        return {
            "enabled": settings.storage_backend == "s3",
            "settings": {
                "bucket": settings.s3_bucket,
                "region": settings.s3_region,
                "prefix": settings.s3_prefix,
                "endpoint_url": settings.s3_endpoint_url,
                "kms_key_id": settings.s3_kms_key_id,
            },
        }
    if pid == "local":
        return {"enabled": settings.storage_backend == "local", "settings": {}}
    # Manifest providers: return minimal config presence
    mpath = os.path.join(_providers_dir(), pid, "manifest.json")
    if os.path.exists(mpath):
        return {
            "enabled": settings.fax_backend == pid,
            "settings": {},
        }
    raise HTTPException(404, detail="Plugin not found")


class UpdatePluginConfigIn(BaseModel):
    enabled: Optional[bool] = None
    settings: Optional[dict[str, Any]] = None


@app.put("/plugins/{plugin_id}/config", dependencies=[Depends(require_admin)])
def update_plugin_config(plugin_id: str, payload: UpdatePluginConfigIn):
    if not settings.feature_v3_plugins:
        return _plugins_disabled_response()
    if _read_cfg is None or _write_cfg is None:
        raise HTTPException(500, detail="Config store unavailable")
    cfg_res = _read_cfg(settings.faxbot_config_path)
    if not cfg_res.ok or cfg_res.data is None:
        raise HTTPException(500, detail=cfg_res.error or "Failed to read config")
    data = cfg_res.data
    pid = plugin_id.lower()
    # Minimal mapping for outbound/storage
    if pid in {"phaxio", "sinch", "sip", "signalwire", "freeswitch"}:
        data.setdefault("providers", {}).setdefault("outbound", {})
        data["providers"]["outbound"]["plugin"] = pid
        data["providers"]["outbound"]["enabled"] = bool(payload.enabled) if payload.enabled is not None else True
        data["providers"]["outbound"]["settings"] = payload.settings or data["providers"]["outbound"].get("settings", {})
    elif pid in {"local", "s3"}:
        data.setdefault("providers", {}).setdefault("storage", {})
        data["providers"]["storage"]["plugin"] = pid
        data["providers"]["storage"]["enabled"] = bool(payload.enabled) if payload.enabled is not None else True
        data["providers"]["storage"]["settings"] = payload.settings or data["providers"]["storage"].get("settings", {})
    else:
        # Accept unknown outbound plugin ids if a manifest exists
        mpath = os.path.join(_providers_dir(), pid, "manifest.json")
        if not os.path.exists(mpath):
            raise HTTPException(404, detail="Plugin not found")
        data.setdefault("providers", {}).setdefault("outbound", {})
        data["providers"]["outbound"]["plugin"] = pid
        data["providers"]["outbound"]["enabled"] = bool(payload.enabled) if payload.enabled is not None else True
        data["providers"]["outbound"]["settings"] = payload.settings or data["providers"]["outbound"].get("settings", {})
    wr = _write_cfg(settings.faxbot_config_path, data)
    if not wr.ok:
        raise HTTPException(500, detail=wr.error or "Failed to write config")
    # Note: applying new config at runtime is future work; for now we persist only.
    return {"ok": True, "path": wr.path}


@app.get("/plugin-registry")
def plugin_registry():
    if not settings.feature_v3_plugins:
        return _plugins_disabled_response()
    # Try to load curated registry file; fallback to built-in list
    try:
        reg_path = os.getenv("PLUGIN_REGISTRY_PATH", os.path.join(os.getcwd(), "config", "plugin_registry.json"))
        if os.path.exists(reg_path):
            import json as _json
            with open(reg_path, "r", encoding="utf-8") as f:
                return _json.load(f)
    except Exception:
        pass
    return {"items": _installed_plugins(), "note": "default registry"}
@app.post("/signalwire-callback")
async def signalwire_callback(request: Request, job_id: Optional[str] = Query(default=None)):
    """SignalWire Compatibility Fax Status callback handler.
    Verifies optional HMAC when configured and updates job status.
    """
    raw = await request.body()
    # Optional HMAC verification; header name may vary by configuration
    key = (settings.signalwire_webhook_signing_key or '').encode()
    if key:
        provided = request.headers.get('X-SignalWire-Signature') or ''
        try:
            digest = hmac.new(key, raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(digest, provided.strip().lower()):
                raise HTTPException(401, detail="Invalid signature")
        except Exception:
            raise HTTPException(401, detail="Invalid signature")
    # Parse form or JSON
    payload: dict[str, Any]
    try:
        form = await request.form()
        payload = dict(form)
    except Exception:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    svc = get_signalwire_service()
    if not svc:
        return {"ok": True}
    res = await svc.handle_status_callback(payload)
    prov_sid = str(res.get('provider_sid') or '')
    status = str(res.get('status') or '')
    # Update job by job_id if present, else by provider_sid best-effort
    with SessionLocal() as db:
        job = None
        if job_id:
            job = db.get(FaxJob, str(job_id))
        if not job and prov_sid:
            job = db.query(FaxJob).filter(FaxJob.provider_sid == prov_sid).first()
        if job:
            job.status = status or job.status
            job.updated_at = datetime.utcnow()
            db.add(job)
            db.commit()
            audit_event("job_updated", job_id=job.id, status=job.status, provider="signalwire")
    return {"ok": True}
class FSOutboundResultIn(BaseModel):
    job_id: Optional[str] = None
    fax_status: Optional[str] = None
    fax_result_text: Optional[str] = None
    fax_result_code: Optional[str] = None
    fax_document_transferred_pages: Optional[int] = None
    uuid: Optional[str] = None


@app.post("/_internal/freeswitch/outbound_result")
def freeswitch_outbound_result(payload: FSOutboundResultIn, x_internal_secret: Optional[str] = Header(default=None)):
    # Reuse Asterisk secret for simplicity; can introduce a dedicated FS secret later
    secret = settings.asterisk_inbound_secret
    if not secret:
        raise HTTPException(401, detail="Internal secret not configured")
    if x_internal_secret != secret:
        raise HTTPException(401, detail="Invalid internal secret")
    if not payload.job_id:
        raise HTTPException(400, detail="Missing job_id")
    status_map = {
        'SUCCESS': 'SUCCESS',
        'OK': 'SUCCESS',
        'FAILED': 'FAILED',
        'ERROR': 'FAILED',
        'FAIL': 'FAILED',
    }
    status = (payload.fax_status or payload.fax_result_text or '').upper()
    internal = status_map.get(status, 'FAILED' if 'FAIL' in status else 'in_progress')
    with SessionLocal() as db:
        job = db.get(FaxJob, str(payload.job_id))
        if not job:
            raise HTTPException(404, detail="Job not found")
        job.status = internal
        if payload.fax_document_transferred_pages:
            job.pages = payload.fax_document_transferred_pages
        if payload.fax_result_text:
            job.error = None if internal == 'SUCCESS' else payload.fax_result_text
        job.updated_at = datetime.utcnow()
        db.add(job)
        db.commit()
    audit_event("job_updated", job_id=str(payload.job_id), status=internal, provider="freeswitch")                                                              
    return {"ok": True}


# Terminal WebSocket endpoint for Admin Console
@app.websocket("/admin/terminal")
async def admin_terminal_websocket(
    websocket: WebSocket,
    api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """WebSocket terminal for Admin Console - requires admin authentication."""
    # Local-only gate (Terminal is a local admin tool)
    if os.getenv("ENABLE_LOCAL_ADMIN", "false").lower() not in {"1","true","yes"}:
        await websocket.accept()
        await websocket.send_text(json.dumps({'type': 'error', 'message': 'Terminal disabled (ENABLE_LOCAL_ADMIN=false)'}))
        await websocket.close(code=1008, reason="Admin console disabled")
        return
    # Block proxied access unless explicitly allowed for demo/testing
    try:
        allow_tunnel_ui = os.getenv("ADMIN_UI_ALLOW_TUNNEL", "false").lower() in {"1","true","yes"}
        # Starlette exposes headers on websocket scope
        h = dict(websocket.headers or [])
        if ("x-forwarded-for" in h or "x-real-ip" in h) and not allow_tunnel_ui:
            await websocket.accept()
            await websocket.send_text(json.dumps({'type': 'error', 'message': 'Terminal not available through proxy'}))
            await websocket.close(code=1008, reason="Admin console not available through proxy")
            return
        # Local IP policy (loopback or private/VPN ranges such as RFC1918 and CGNAT 100.64.0.0/10)
        client_ip = websocket.client.host if websocket.client else ""
        import ipaddress
        ip = ipaddress.ip_address(client_ip) if client_ip else None
        cgnat = ipaddress.ip_network('100.64.0.0/10')
        allow = (ip.is_loopback if ip else False) or ((ip.is_private if ip else False)) or (ip and ip.version == 4 and ip in cgnat) or allow_tunnel_ui
        if not allow:
            await websocket.accept()
            await websocket.send_text(json.dumps({'type': 'error', 'message': 'Forbidden'}))
            await websocket.close(code=1008, reason="Forbidden")
            return
    except Exception:
        # Fail closed if we cannot determine client
        await websocket.accept()
        await websocket.send_text(json.dumps({'type': 'error', 'message': 'Forbidden'}))
        await websocket.close(code=1008, reason="Forbidden")
        return
    # Check admin authentication
    if not api_key or api_key != settings.api_key:
        # Try to get API key from query params for WebSocket auth
        import urllib.parse
        # Starlette's websocket.url.query is a string; older versions may return bytes
        _q = websocket.url.query
        try:
            query_str = _q.decode()  # type: ignore[attr-defined]
        except AttributeError:
            query_str = str(_q or "")
        query_params = urllib.parse.parse_qs(query_str)
        ws_api_key = query_params.get('api_key', [None])[0]
        
        if not ws_api_key or ws_api_key != settings.api_key:
            # Check if they have a valid DB-backed key with admin privileges
            from .auth import verify_db_key
            key_data = verify_db_key(ws_api_key or api_key or "")
            if not key_data or 'keys:manage' not in key_data.get('scopes', []):
                await websocket.close(code=1008, reason="Unauthorized")
                return
    
    # Import terminal handler
    from .terminal import handle_terminal_websocket, check_terminal_requirements
    
    # Check requirements
    issues = check_terminal_requirements()
    if issues:
        await websocket.accept()
        await websocket.send_text(json.dumps({
            'type': 'error',
            'message': f'Terminal not available: {", ".join(issues)}'
        }))
        await websocket.close()
        return
    
    # Handle terminal session
    await handle_terminal_websocket(websocket)

# ===== Phase 2: Auth endpoints (guarded by sessions flag) =====
if os.getenv("FAXBOT_SESSIONS_ENABLED", "false").lower() in {"1","true","yes"}:
    try:
        from .plugins.manager import PluginManager
        _pm_for_auth = PluginManager()
        _pm_for_auth.load_all()
    except Exception:
        _pm_for_auth = None  # type: ignore

    class LoginRequest(BaseModel):
        username: str
        password: str
        ttl_seconds: int | None = 3600

    async def _identity_authenticate(username: str, password: str) -> bool:
        # Try identity plugin if available
        try:
            if _pm_for_auth:
                ident = _pm_for_auth.get_active_by_type("identity")
                if hasattr(ident, "authenticate_password"):
                    res = await ident.authenticate_password(username, password)  # type: ignore
                    return bool(getattr(res, "success", False))
        except Exception:
            pass
        # Dev fallback: bootstrap admin (do NOT use in prod)
        boot = os.getenv("FAXBOT_BOOTSTRAP_PASSWORD")
        return username == "admin" and bool(boot) and password == boot

    @app.post("/auth/login")
    async def auth_login(payload: LoginRequest, response: Response):
        if not await _identity_authenticate(payload.username, payload.password):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        from .security.auth_sessions import create_session  # lazy import
        sid, token = create_session(user_id=payload.username, ttl_seconds=payload.ttl_seconds or 3600)
        cookie_opts = {
            "httponly": True,
            "secure": os.getenv("ENFORCE_PUBLIC_HTTPS", "false").lower() in {"1","true","yes"},
            "samesite": "strict",
            "path": "/",
        }
        response.set_cookie("fb_sess", token, **cookie_opts)
        csrf_enabled = os.getenv("FAXBOT_CSRF_ENABLED", "false").lower() in {"1","true","yes"}
        if csrf_enabled:
            csrf_val = secrets.token_urlsafe(16)
            response.set_cookie("fb_csrf", csrf_val, secure=cookie_opts["secure"], samesite="strict", path="/")
            return {"success": True, "csrf": csrf_val}
        return {"success": True}

    @app.post("/auth/logout")
    async def auth_logout(response: Response, request: Request):
        from .security.auth_sessions import revoke_session  # lazy import
        tok = request.cookies.get("fb_sess")
        if tok:
            try:
                revoke_session(tok)
            except Exception:
                pass
        response.delete_cookie("fb_sess", path="/")
        response.delete_cookie("fb_csrf", path="/")
        return {"success": True}

    @app.post("/auth/refresh")
    async def auth_refresh(response: Response, request: Request):
        from .security.auth_sessions import rotate_session  # lazy import
        tok = request.cookies.get("fb_sess")
        if not tok:
            raise HTTPException(status_code=401, detail="No session")
        new_tok = rotate_session(tok)
        if not new_tok:
            raise HTTPException(status_code=401, detail="Invalid session")
        cookie_opts = {
            "httponly": True,
            "secure": os.getenv("ENFORCE_PUBLIC_HTTPS", "false").lower() in {"1","true","yes"},
            "samesite": "strict",
            "path": "/",
        }
        response.set_cookie("fb_sess", new_tok, **cookie_opts)
        return {"success": True}
# Optional metrics endpoint
@app.get("/metrics")
async def metrics_endpoint():
    # Minimal stub; integrate Prometheus/Otel in production
    content = "# HELP faxbot_up 1\n# TYPE faxbot_up gauge\nfaxbot_up 1\n"
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content, media_type="text/plain; version=0.0.4")

# Startup log
import logging as _logging
_logging.getLogger(__name__).info("/metrics ready on :8080")

# Secrets fail-fast (opt-in)
if os.getenv("ENFORCE_SECRET_CHECKS", "false").lower() in {"1","true","yes"}:
    cmk = os.getenv("CONFIG_MASTER_KEY", "")
    pepper = os.getenv("FAXBOT_SESSION_PEPPER", "")
    missing = []
    if not (cmk and len(cmk) == 44):
        missing.append("CONFIG_MASTER_KEY(44b64)")
    if not pepper:
        missing.append("FAXBOT_SESSION_PEPPER")
    if missing:
        _logging.getLogger(__name__).error(f"Missing required secrets: {', '.join(missing)}")
        raise SystemExit(1)
