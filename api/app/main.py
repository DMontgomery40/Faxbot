import os
import shutil
import re
import uuid
import asyncio
import secrets
from datetime import datetime, timedelta
import tempfile
from typing import Optional, Any, List, cast
import time
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Header, Depends, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .config import settings, reload_settings
from .db import init_db, SessionLocal, FaxJob
from .models import FaxJobOut
from .conversion import ensure_dir, txt_to_pdf, pdf_to_tiff
from .ami import ami_client
from .phaxio_service import get_phaxio_service
from .sinch_service import get_sinch_service
import hmac
import hashlib
from urllib.parse import urlparse
from fastapi.responses import StreamingResponse
from .audit import init_audit_logger, audit_event
from .audit import query_recent_logs
from .storage import get_storage, reset_storage
from .auth import verify_db_key, create_api_key, list_api_keys, revoke_api_key, rotate_api_key

# v3 plugins (feature-gated)
try:
    from .plugins.config_store import read_config as _read_cfg, write_config as _write_cfg  # type: ignore
except Exception:  # pragma: no cover - optional
    _read_cfg = None  # type: ignore
    _write_cfg = None  # type: ignore
from pydantic import BaseModel


app = FastAPI(title="Faxbot API", version="1.0.0")
# Expose phaxio_service module for tests that reference app.phaxio_service
from . import phaxio_service as _phaxio_module  # noqa: E402
app.phaxio_service = _phaxio_module  # type: ignore[attr-defined]


PHONE_RE = re.compile(r"^[+]?\d{6,20}$")
ALLOWED_CT = {"application/pdf", "text/plain"}


# In-memory per-key rate limiter (fixed window, per minute)
_rate_buckets: dict[str, dict[str, int]] = {}


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
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(max(1, retry_after))},
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
        # Block proxied access by default (defensive)
        h = request.headers
        if "x-forwarded-for" in h or "x-real-ip" in h:
            return Response(content="Admin console not available through proxy", status_code=403)
        # Local access policy
        # Allow loopback; when running inside a container, also allow private bridge IPs (RFC1918)
        client_ip = str(request.client.host)
        try:
            import ipaddress
            ip = ipaddress.ip_address(client_ip)
        except Exception:
            return Response(content="Forbidden", status_code=403)
        in_container = os.path.exists("/.dockerenv")
        allow = ip.is_loopback or (in_container and ip.is_private)
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
    # Validate Ghostscript availability for all backends (core dependency)
    if shutil.which("gs") is None:
        raise RuntimeError("Ghostscript (gs) not found. Install 'ghostscript' — it is required for fax file processing.")
    # Security posture warnings
    if not settings.require_api_key and not settings.api_key and not settings.fax_disabled:
        print("[warn] API auth is not enforced (REQUIRE_API_KEY=false and API_KEY unset); /fax requests are unauthenticated. Set API_KEY or REQUIRE_API_KEY for production.")
    try:
        pu = urlparse(settings.public_api_url)
        insecure = pu.scheme == "http" and pu.hostname not in {"localhost", "127.0.0.1"}
        if insecure:
            msg = "PUBLIC_API_URL is not HTTPS; cloud providers will fetch PDFs over HTTP. Use HTTPS in production."
            if settings.enforce_public_https and settings.fax_backend == "phaxio":
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
    # Start AMI listener and result handler (only for SIP backend)
    if not settings.fax_disabled and settings.fax_backend == "sip":
        asyncio.create_task(ami_client.connect())
        ami_client.on_fax_result(_handle_fax_result)


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

    # Backend config check
    backend = settings.fax_backend
    backend_ok = True
    backend_warnings: List[str] = []
    if backend == "phaxio":
        backend_ok = bool(settings.phaxio_api_key and settings.phaxio_api_secret)
        # PUBLIC_API_URL must be https for production enforcement, unless localhost
        try:
            pu = urlparse(settings.public_api_url)
            if settings.enforce_public_https and (pu.scheme != "https") and pu.hostname not in {"localhost", "127.0.0.1"}:
                backend_ok = False
                backend_warnings.append("PUBLIC_API_URL must be HTTPS when ENFORCE_PUBLIC_HTTPS=true")
        except Exception:
            backend_warnings.append("Invalid PUBLIC_API_URL")
    elif backend == "sinch":
        backend_ok = bool(settings.sinch_project_id and settings.sinch_api_key and settings.sinch_api_secret)
    elif backend == "sip":
        # Presence checks only; AMI connection is handled asynchronously
        if not settings.ami_password or settings.ami_password == "changeme":
            backend_warnings.append("ASTERISK_AMI_PASSWORD must be set to a non-default value")
        backend_ok = True
    else:
        backend_warnings.append(f"Unknown backend: {backend}")

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

    # System dependency check (Ghostscript — required for all backends)
    gs_installed = shutil.which("gs") is not None
    if not gs_installed:
        backend_ok = False
        backend_warnings.append("Ghostscript (gs) not installed — required for fax file processing")

    # AMI connection hint for SIP
    ami_connected = False
    try:
        from .ami import ami_client as _ac  # type: ignore
        ami_connected = bool(getattr(_ac, "_connected").is_set())  # type: ignore[union-attr]
    except Exception:
        ami_connected = False

    ready = bool(db_ok and backend_ok and gs_installed and (storage_ok or not settings.inbound_enabled))
    status_code = 200 if ready else 503
    from fastapi.responses import JSONResponse as _JR
    return _JR(
        content={
            "status": "ready" if ready else "not_ready",
            "backend": backend,
            "checks": {
                "db": db_ok,
                "backend_config": backend_ok,
                "storage": storage_ok if settings.inbound_enabled else None,
                "ami_connected": ami_connected if backend == "sip" else None,
                "ghostscript": gs_installed,
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
    backend = settings.fax_backend
    # Configured flags
    cfg = {
        "backend": backend,
        "allow_restart": settings.admin_allow_restart,
        "require_api_key": settings.require_api_key,
        "enforce_public_https": settings.enforce_public_https,
        "phaxio_verify_signature": settings.phaxio_verify_signature,
        "persisted_settings_enabled": (os.getenv("ENABLE_PERSISTED_SETTINGS", "false").lower() in {"1","true","yes"}),
        "branding": {
            "docs_base": os.getenv("DOCS_BASE_URL", "https://dmontgomery40.github.io/Faxbot"),
            "logo_path": "/admin/ui/faxbot_full_logo.png",
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
            "sip_ami_configured": bool(settings.ami_username and settings.ami_password),
            "sip_ami_password_default": (settings.ami_password == "changeme"),
        },
        "public_api_url": settings.public_api_url,
    }
    # v3 plugins status (feature-gated)
    if settings.feature_v3_plugins:
        cfg["v3_plugins"] = {
            "enabled": True,
            "active_outbound": settings.fax_backend,
            "config_path": settings.faxbot_config_path,
            "plugin_install_enabled": settings.feature_plugin_install,
        }
    return cfg


@app.get("/admin/settings", dependencies=[Depends(require_admin)])
def get_admin_settings():
    """Return effective settings with sensitive values masked."""
    return {
        "backend": {
            "type": settings.fax_backend,
            "disabled": settings.fax_disabled,
        },
        "phaxio": {
            "api_key": mask_secret(settings.phaxio_api_key),
            "api_secret": mask_secret(settings.phaxio_api_secret),
            "callback_url": settings.phaxio_status_callback_url,
            "verify_signature": settings.phaxio_verify_signature,
            "configured": bool(settings.phaxio_api_key and settings.phaxio_api_secret),
        },
        "sinch": {
            "project_id": settings.sinch_project_id,
            "api_key": mask_secret(settings.sinch_api_key),
            "api_secret": mask_secret(settings.sinch_api_secret),
            "configured": bool(settings.sinch_project_id and settings.sinch_api_key and settings.sinch_api_secret),
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
                "verify_signature": settings.sinch_inbound_verify_signature,
                "basic_auth_configured": bool(settings.sinch_inbound_basic_user),
                "hmac_configured": bool(settings.sinch_inbound_hmac_secret),
            },
        },
        "limits": {
            "max_file_size_mb": settings.max_file_size_mb,
            "pdf_token_ttl_minutes": settings.pdf_token_ttl_minutes,
            "rate_limit_rpm": settings.max_requests_per_minute,
            "inbound_list_rpm": settings.inbound_list_rpm,
            "inbound_get_rpm": settings.inbound_get_rpm,
        },
    }


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
        # v1 presence-only
        results["checks"]["auth"] = bool(
            payload.sinch_project_id and payload.sinch_api_key and payload.sinch_api_secret
        )
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
    sinch_inbound_verify_signature: Optional[bool] = None
    sinch_inbound_basic_user: Optional[str] = None
    sinch_inbound_basic_pass: Optional[str] = None
    sinch_inbound_hmac_secret: Optional[str] = None

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
    _set_env_bool("SINCH_INBOUND_VERIFY_SIGNATURE", payload.sinch_inbound_verify_signature)
    _set_env_opt("SINCH_INBOUND_BASIC_USER", payload.sinch_inbound_basic_user)
    _set_env_opt("SINCH_INBOUND_BASIC_PASS", payload.sinch_inbound_basic_pass)
    _set_env_opt("SINCH_INBOUND_HMAC_SECRET", payload.sinch_inbound_hmac_secret)

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
    old_storage = (settings.storage_backend or "local").lower()
    reload_settings()
    new_backend = settings.fax_backend
    new_storage = (settings.storage_backend or "local").lower()
    backend_changed = (old_backend != new_backend)
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
        "storage_changed": storage_changed,
        "restart_recommended": backend_changed or new_backend == "sip" or storage_changed or mcp_changed,
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


@app.get("/admin/inbound/callbacks", dependencies=[Depends(require_admin)])
def admin_inbound_callbacks():
    base = settings.public_api_url.rstrip("/")
    backend = settings.fax_backend
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
            "auth": {
                "basic": bool(settings.sinch_inbound_basic_user),
                "hmac": bool(settings.sinch_inbound_hmac_secret),
            },
            "notes": "Set webhook in Sinch Fax console. Optionally use Basic and/or HMAC.",
        })
    elif backend == "sip":
        out["callbacks"].append({
            "name": "Asterisk Internal",
            "url": f"/_internal/asterisk/inbound",
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
    # Create a tiny placeholder PDF
    pdf_path = os.path.join(data_dir, f"{job_id}.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"%PDF-1.4\n% inbound simulation\n%%EOF")
    size_bytes = os.path.getsize(pdf_path)
    sha256_hex = hashlib.sha256(b"%PDF-1.4\n% inbound simulation\n%%EOF").hexdigest()
    storage = get_storage()
    stored_uri = storage.put_pdf(pdf_path, f"{job_id}.pdf")
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


@app.post("/admin/diagnostics/run", dependencies=[Depends(require_admin)])
async def run_diagnostics():
    """Run bounded, non-destructive diagnostics for v1."""
    diag: dict[str, Any] = {
        "timestamp": datetime.utcnow().isoformat(),
        "backend": settings.fax_backend,
        "checks": {},
    }
    # Backend configured flags
    if settings.fax_backend == "phaxio":
        diag["checks"]["phaxio"] = {
            "api_key_set": bool(settings.phaxio_api_key),
            "api_secret_set": bool(settings.phaxio_api_secret),
            "callback_url_set": bool(settings.phaxio_status_callback_url),
            "signature_verification": settings.phaxio_verify_signature,
            "public_url_https": settings.public_api_url.startswith("https://"),
        }
    elif settings.fax_backend == "sinch":
        diag["checks"]["sinch"] = {
            "project_id_set": bool(settings.sinch_project_id),
            "api_key_set": bool(settings.sinch_api_key),
            "api_secret_set": bool(settings.sinch_api_secret),
        }
    elif settings.fax_backend == "sip":
        sip_checks: dict[str, Any] = {
            "ami_host": settings.ami_host,
            "ami_port": settings.ami_port,
            "ami_password_not_default": settings.ami_password != "changeme",
            "station_id_set": bool(settings.fax_station_id),
        }
        # Probe AMI reachability best-effort
        try:
            from .ami import test_ami_connection
            sip_checks["ami_reachable"] = await test_ami_connection(
                settings.ami_host, settings.ami_port, settings.ami_username, settings.ami_password
            )
        except Exception as e:
            sip_checks["ami_reachable"] = False
            sip_checks["ami_error"] = str(e)
        diag["checks"]["sip"] = sip_checks

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

    # Storage
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

    # Inbound flags
    if settings.inbound_enabled:
        inbound = {"enabled": True, "retention_days": settings.inbound_retention_days}
        if settings.fax_backend == "phaxio":
            inbound["signature_verification"] = settings.phaxio_inbound_verify_signature
        elif settings.fax_backend == "sinch":
            inbound["auth_configured"] = bool(settings.sinch_inbound_basic_user or settings.sinch_inbound_hmac_secret)
        elif settings.fax_backend == "sip":
            inbound["asterisk_secret_set"] = bool(settings.asterisk_inbound_secret)
        diag["checks"]["inbound"] = inbound

    # Security summary
    diag["checks"]["security"] = {
        "enforce_https": settings.enforce_public_https,
        "audit_logging": settings.audit_log_enabled,
        "rate_limiting": settings.max_requests_per_minute > 0,
        "pdf_token_ttl": settings.pdf_token_ttl_minutes,
    }

    # Summary
    critical: list[str] = []
    warnings: list[str] = []
    if settings.fax_backend == "phaxio":
        p = diag["checks"].get("phaxio", {})
        if not p.get("api_key_set"):
            critical.append("Phaxio API key not set")
        if not p.get("callback_url_set"):
            warnings.append("PHAXIO_STATUS_CALLBACK_URL not set")
        if not p.get("public_url_https"):
            warnings.append("PUBLIC_API_URL should be HTTPS")
    if settings.fax_backend == "sip":
        s = diag["checks"].get("sip", {})
        if not s.get("ami_password_not_default"):
            critical.append("AMI password is default 'changeme'")
        if not s.get("ami_reachable"):
            warnings.append("AMI not reachable")
    # Ghostscript is required for all backends
    if not sys.get("ghostscript"):
        critical.append("Ghostscript (gs) not installed — required for fax file processing")
    if not sys.get("fax_data_writable"):
        critical.append("Cannot write to fax data dir")
    if not sys.get("database_connected"):
        critical.append("Database connection failed")
    diag["summary"] = {"healthy": len(critical) == 0, "critical_issues": critical, "warnings": warnings}
    return diag


@app.get("/admin/settings/export", dependencies=[Depends(require_admin)])
def export_settings_env():
    """Generate a .env snippet for current settings (secrets redacted)."""
    lines: List[str] = []
    lines.append(f"FAX_BACKEND={settings.fax_backend}")
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
    kv["REQUIRE_API_KEY"] = "true" if settings.require_api_key else "false"
    kv["ENFORCE_PUBLIC_HTTPS"] = "true" if settings.enforce_public_https else "false"
    kv["FAX_DISABLED"] = "true" if settings.fax_disabled else "false"
    kv["PUBLIC_API_URL"] = settings.public_api_url
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
    kv["SINCH_INBOUND_VERIFY_SIGNATURE"] = "true" if settings.sinch_inbound_verify_signature else "false"
    if settings.sinch_inbound_basic_user:
        kv["SINCH_INBOUND_BASIC_USER"] = settings.sinch_inbound_basic_user
        kv["SINCH_INBOUND_BASIC_PASS"] = settings.sinch_inbound_basic_pass or ""
    if settings.sinch_inbound_hmac_secret:
        kv["SINCH_INBOUND_HMAC_SECRET"] = settings.sinch_inbound_hmac_secret
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
async def send_fax(background: BackgroundTasks, to: str = Form(...), file: UploadFile = File(...)):
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
            # Test mode - skip conversion
            with open(pdf_path, "wb") as f:
                f.write(b"%PDF-1.4\ntest\n%%EOF")
        else:
            txt_to_pdf(orig_path, pdf_path)
    else:
        # Copy the PDF directly
        shutil.copyfile(orig_path, pdf_path)

    # Backend-specific file preparation
    if settings.fax_backend == "phaxio":
        # Cloud backend: skip TIFF conversion; provider callback will set final pages
        pages = None
        if settings.fax_disabled:
            # Test mode: nothing else to prepare
            pass
    elif settings.fax_backend == "sinch":
        # Sinch cloud backend: no local TIFF; provider handles rasterization
        pages = None
        # nothing else to prepare here
    else:
        # SIP/Asterisk requires TIFF
        if settings.fax_disabled:
            pages = 1
            with open(tiff_path, "wb") as f:
                f.write(b"TIFF_PLACEHOLDER")
        else:
            pages, _ = pdf_to_tiff(pdf_path, tiff_path)

    # Create job in DB with backend info
    with SessionLocal() as db:
        job = FaxJob(
            id=job_id,
            to_number=to,
            file_name=file.filename,
            tiff_path=tiff_path,
            status="queued",
            pages=pages,
            backend=settings.fax_backend,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
    audit_event("job_created", job_id=job_id, backend=settings.fax_backend)

    # Kick off fax sending based on backend
    if not settings.fax_disabled:
        if settings.fax_backend == "phaxio":
            background.add_task(_send_via_phaxio, job_id, to, pdf_path)
        elif settings.fax_backend == "sinch":
            background.add_task(_send_via_sinch, job_id, to, pdf_path)
        else:
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
        audit_event("job_failed", job_id=job_id, error=str(e))


@app.get("/fax/{job_id}", response_model=FaxJobOut, dependencies=[Depends(require_fax_read)])
def get_fax(job_id: str):
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if not job:
            raise HTTPException(404, detail="Job not found")
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
    """Handle Phaxio status callbacks."""
    # Verify signature if enabled
    raw_body = await request.body()
    if settings.phaxio_verify_signature:
        provided = request.headers.get("X-Phaxio-Signature") or request.headers.get("X-Phaxio-Signature-SHA256")
        if not provided:
            raise HTTPException(401, detail="Missing Phaxio signature")
        secret = (settings.phaxio_api_secret or "").encode()
        if not secret:
            raise HTTPException(401, detail="Phaxio secret not configured")
        digest = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, provided.strip().lower()):
            raise HTTPException(401, detail="Invalid Phaxio signature")

    form_data = await request.form()
    callback_data = dict(form_data)
    
    # Get job ID from query params
    job_id = request.query_params.get("job_id")
    if not job_id:
        return {"status": "no job_id provided"}
    
    phaxio_service = get_phaxio_service()
    if not phaxio_service:
        return {"status": "phaxio not configured"}
    
    # Process the callback
    status_info = await phaxio_service.handle_status_callback(callback_data)
    
    # Update job status
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if job:
            job.status = status_info['status']
            if status_info.get('error_message'):
                job.error = status_info['error_message']
            if status_info.get('pages'):
                job.pages = status_info['pages']
            job.updated_at = datetime.utcnow()  # type: ignore[assignment]
            db.add(job)  # type: ignore[arg-type]
            db.commit()
    audit_event("job_updated", job_id=job_id, status=status_info.get('status'), provider="phaxio")
    
    return {"status": "ok"}


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
        audit_event("job_failed", job_id=job_id, error=str(e))


async def _send_via_sinch(job_id: str, to: str, pdf_path: str):
    """Send fax via Sinch Fax API v3 (Phaxio by Sinch)."""
    try:
        sinch = get_sinch_service()
        if not sinch or not sinch.is_configured():
            raise Exception("Sinch Fax is not properly configured")

        audit_event("job_dispatch", job_id=job_id, method="sinch")

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


@app.post("/_internal/asterisk/inbound")
def asterisk_inbound(payload: dict, x_internal_secret: Optional[str] = Header(default=None)):
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
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
    stored_uri = storage.put_pdf(pdf_path, object_name)
    try:
        if stored_uri.startswith("s3://") and os.path.exists(pdf_path):
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
async def phaxio_inbound(request: Request):
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
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
        return {"status": "ignored"}

    # Idempotency: unique (provider_sid, event_type)
    with SessionLocal() as db:
        from .db import InboundEvent  # type: ignore
        evt = InboundEvent(id=uuid.uuid4().hex, provider_sid=str(provider_sid), event_type="phaxio-inbound", created_at=datetime.utcnow())
        try:
            db.add(evt)
            db.commit()
        except Exception:
            # Duplicate → ignore
            db.rollback()
            return {"status": "ok"}

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
        # Minimal placeholder PDF so record exists; operators can re-fetch if needed
        with open(local_pdf, "wb") as f:
            f.write(b"%PDF-1.4\n% placeholder inbound\n%%EOF")
        size_bytes = len(b"%PDF-1.4\n% placeholder inbound\n%%EOF")
        pages_int = None
        sha256_hex = hashlib.sha256(b"%PDF-1.4\n% placeholder inbound\n%%EOF").hexdigest()
    else:
        with open(local_pdf, "wb") as f:
            f.write(pdf_bytes)
        size_bytes = len(pdf_bytes)
        pages_int = None
        sha256_hex = hashlib.sha256(pdf_bytes).hexdigest()

    storage = get_storage()
    stored_uri = storage.put_pdf(local_pdf, f"{job_id}.pdf")
    try:
        if stored_uri.startswith("s3://") and os.path.exists(local_pdf):
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
    audit_event("inbound_received", job_id=job_id, backend="phaxio")
    return {"status": "ok"}


@app.post("/sinch-inbound")
async def sinch_inbound(request: Request):
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
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
    # Verify HMAC if configured
    if settings.sinch_inbound_hmac_secret:
        provided = request.headers.get("X-Sinch-Signature", "")
        secret = settings.sinch_inbound_hmac_secret.encode()
        digest = hmac.new(secret, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, (provided or "").strip().lower()):
            raise HTTPException(401, detail="Invalid signature")

    try:
        data = await request.json()
    except Exception:
        data = {}

    provider_sid = data.get("id") or data.get("fax_id")
    from_number = data.get("from") or data.get("from_number")
    to_number = data.get("to") or data.get("to_number")
    pages = data.get("num_pages") or data.get("pages")
    status = data.get("status") or "received"
    file_url = data.get("file_url") or data.get("media_url")

    if not provider_sid:
        return {"status": "ignored"}

    with SessionLocal() as db:
        from .db import InboundEvent  # type: ignore
        evt = InboundEvent(id=uuid.uuid4().hex, provider_sid=str(provider_sid), event_type="sinch-inbound", created_at=datetime.utcnow())
        try:
            db.add(evt)
            db.commit()
        except Exception:
            db.rollback()
            return {"status": "ok"}

    pdf_bytes: Optional[bytes] = None
    if file_url:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(str(file_url))
                if resp.status_code == 200:
                    pdf_bytes = resp.content
        except Exception:
            pdf_bytes = None

    job_id = uuid.uuid4().hex
    data_dir = settings.fax_data_dir
    ensure_dir(data_dir)
    local_pdf = os.path.join(data_dir, f"{job_id}.pdf")
    if pdf_bytes is None:
        with open(local_pdf, "wb") as f:
            f.write(b"%PDF-1.4\n% placeholder inbound\n%%EOF")
        size_bytes = len(b"%PDF-1.4\n% placeholder inbound\n%%EOF")
        pages_int = None
        sha256_hex = hashlib.sha256(b"%PDF-1.4\n% placeholder inbound\n%%EOF").hexdigest()
    else:
        with open(local_pdf, "wb") as f:
            f.write(pdf_bytes)
        size_bytes = len(pdf_bytes)
        pages_int = None
        sha256_hex = hashlib.sha256(pdf_bytes).hexdigest()

    storage = get_storage()
    stored_uri = storage.put_pdf(local_pdf, f"{job_id}.pdf")

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
            backend="sinch",
            provider_sid=str(provider_sid),
            pages=int(pages) if pages else pages_int,
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
    audit_event("inbound_received", job_id=job_id, backend="sinch")
    return {"status": "ok"}
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
        "id": "sip",
        "name": "SIP/Asterisk (Self-hosted)",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status"],
        "enabled": (current == "sip"),
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
    if pid in {"phaxio", "sinch", "sip"}:
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
        raise HTTPException(404, detail="Plugin not found")
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
