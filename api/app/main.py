import os
import shutil
import re
import uuid
import asyncio
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta
import tempfile
from typing import Optional, Any, List, Dict, cast
import subprocess
import time
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Depends, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .config import (
    settings,
    reload_settings,
    active_outbound,
    active_inbound,
    providerHasTrait,
    providerTraitValue,
)
from .db import init_db, SessionLocal, FaxJob
from .models import FaxJobOut
from .conversion import ensure_dir
from .documents import prepare_upload, UploadPreparationError
from .ami import ami_client
from .phaxio_service import get_phaxio_service
from .sinch_service import get_sinch_service
from .signalwire_service import get_signalwire_service
from .freeswitch_service import originate_txfax, fs_cli_available
import hmac
import hashlib
from urllib.parse import urlparse
from fastapi.responses import StreamingResponse
import json
from .audit import init_audit_logger, close_audit_logger, audit_event
from .audit import query_recent_logs
from .storage import get_storage
from .auth import verify_db_key, create_api_key, list_api_keys, revoke_api_key, rotate_api_key
from .plugins.http_provider import HttpManifest, HttpProviderRuntime
from .config_paths import (
    InvalidProviderPath, plugin_examples_path, plugin_registry_path,
    provider_manifest_path, providers_dir,
)
from .signalwire_service import get_signalwire_service

from pydantic import BaseModel, ConfigDict, Field, StrictBool, create_model
from .config_values import ConfigurationValues, ConfigurationValueError
from .config_views import project_admin_settings
from .config_activation import ConfigurationActivationError
from .config_store import ConfigurationConflict, ConfigurationStoreError, ConfigurationCommitUncertain, UnboundProviderProfile
from .provider_execution import service_from_profile, ProviderExecutionError
from .config_file import ConfigurationFileError, format_environment, parse_environment, write_environment
from .config_secrets import ConfigurationSecretError
from .provider_catalog import ProviderCatalogError, validate_http_provider_document
from pathlib import Path
from .config_runtime import ConfigurationRuntime, ConfigurationMiddleware, run_lifecycle_step
from .outbound_store import OutboundStore, DeliveryConflict, TERMINAL
from .outbound_worker import OutboundWorker
from .outbound_polling import OutboundPoller
from .provider_execution import UnsupportedProviderExecutionError
from .outbound_transport import CapturedTransport, normalize_status
from .outbound_callbacks import CapturedCallbacks, CallbackRejected
from .request_identity import RequestIdentity, IdempotentReplay, IdempotencyConflict, fingerprint_upload


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Own API tasks and explicitly enter lifespans of enabled MCP mounts."""
    if getattr(application.state, "runtime_active", False):
        raise RuntimeError("Faxbot API lifespan is already running")
    application.state.runtime_active = True
    tasks: list[asyncio.Task] = []
    mounts = []
    owns_ami = False
    runtime = ConfigurationRuntime(os.environ)
    try:
        await run_lifecycle_step(runtime.prepare)
        application.state.configuration_runtime = runtime
        with runtime.frame(runtime.candidate):
            try:
                owns_ami = await _initialize_runtime(tasks)
                if owns_ami:
                    ami_client.on_fax_result(_handle_fax_result)
                    ami_client.on_originate_response(_handle_originate_response)
                    tasks.append(asyncio.create_task(ami_client.connect(), name="faxbot-ami-connect"))
                    await asyncio.wait_for(ami_client._connected.wait(), timeout=10)
                async with AsyncExitStack() as stack:
                    _mount_enabled_mcp(application, mounts)
                    for mount in mounts:
                        await stack.enter_async_context(mount.app.router.lifespan_context(mount.app))
                    await run_lifecycle_step(runtime.publish_ready)
                    delivery = OutboundStore(runtime.manager.store)
                    worker = OutboundWorker(delivery, CapturedTransport(delivery, runtime, ami=ami_client))
                    tasks.append(asyncio.create_task(worker.run(), name='faxbot-outbound-worker'))
                    tasks.append(asyncio.create_task(OutboundPoller(delivery).run(), name='faxbot-outbound-poller'))
                    yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if owns_ami:
                    await ami_client.close()
                for mount in mounts:
                    application.router.routes.remove(mount)
                close_audit_logger()
    finally:
        runtime.close()
        application.state.configuration_runtime = None
        application.state.runtime_active = False


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
    lifespan=lifespan,
)
app.add_middleware(ConfigurationMiddleware)


async def _configuration_error_handler(request, exc):
    if isinstance(exc, ConfigurationConflict):
        status = 409
    elif isinstance(exc, (ConfigurationStoreError, ConfigurationSecretError)):
        status = 503
    else:
        status = 400
    return JSONResponse({'detail': str(exc)}, status_code=status)


for _error_class in (ConfigurationValueError, ConfigurationActivationError, ConfigurationConflict,
                     ConfigurationStoreError, ConfigurationFileError, ConfigurationSecretError, ProviderCatalogError):
    app.add_exception_handler(_error_class, _configuration_error_handler)


from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler


@app.exception_handler(RequestValidationError)
async def _request_validation_error(request, exc):
    if request.url.path.startswith('/admin/fax-jobs/') and request.url.path.endswith('/reconcile'):
        # Pydantic errors include raw rejected values and arbitrary extra keys.
        return JSONResponse({'detail': 'Invalid provider identity reconciliation input.'}, status_code=422)
    if request.url.path.startswith('/admin/settings') or request.url.path.startswith('/plugins/'):
        return JSONResponse({'detail': [
            {'loc': error['loc'], 'type': error['type'], 'msg': 'Invalid configuration input.'}
            for error in exc.errors()]}, status_code=422)
    return await request_validation_exception_handler(request, exc)

# Expose phaxio_service module for tests that reference app.phaxio_service
from . import phaxio_service as _phaxio_module  # noqa: E402
app.phaxio_service = _phaxio_module  # type: ignore[attr-defined]


PHONE_RE = re.compile(r"^[+]?\d{6,20}$")
ALLOWED_CT = {"application/pdf", "text/plain"}


# In-memory per-key rate limiter (fixed window, per minute)
_rate_buckets: dict[str, dict[str, int]] = {}


def _enforce_rate_limit(info: Optional[dict], path: str, limit: Optional[int] = None):
    # Choose provided per-route limit, else global
    limit = settings.max_requests_per_minute if limit is None else int(limit)
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

# ===== Embedded MCP mounts (optional, startup failures are fatal when enabled) =====
def _mount_enabled_mcp(application: FastAPI, mounts: list):
    if not (settings.enable_mcp_sse or settings.enable_mcp_http):
        return
    from python_mcp.transport_config import APIConfiguration
    runtime = application.state.configuration_runtime
    # Local API transport location is a deployment input, not PUBLIC_API_URL
    # (which belongs to provider document retrieval and may point through a tunnel).
    api_base_url = runtime.environment.get('FAX_API_URL', 'http://localhost:8080')
    def api_configuration():
        # An SSE connection may outlive many active revisions. Sample once per
        # tool invocation rather than inheriting its connection's old frame.
        return APIConfiguration(api_base_url, runtime.manager.store.read().active.values.api_key)
    options = dict(api_base_url=api_base_url, api_key=settings.api_key,
        api_config_provider=api_configuration, require_oauth=settings.require_mcp_oauth,
        oauth_issuer=settings.oauth_issuer, oauth_audience=settings.oauth_audience,
        oauth_jwks_url=settings.oauth_jwks_url)
    if settings.enable_mcp_sse:
        from python_mcp import server as _mcp_server
        application.mount(settings.mcp_sse_path, _mcp_server.create_app(**options))
        mounts.append(application.router.routes[-1])
    if settings.enable_mcp_http:
        from python_mcp import http_server as _mcp_http
        application.mount(settings.mcp_http_path, _mcp_http.create_app(**options))
        mounts.append(application.router.routes[-1])


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


async def _initialize_runtime(tasks: list[asyncio.Task]) -> bool:
    # Ensure data dir
    ensure_dir(settings.fax_data_dir)
    # Validate Ghostscript availability — required for all configurations
    _gs_missing = shutil.which("gs") is None
    if _gs_missing and not settings.fax_disabled:
        if str(os.getenv("FAXBOT_TEST_MODE", "false")).lower() in {"1","true","yes"}:
            print("[warn] Ghostscript (gs) not found; allowed via FAXBOT_TEST_MODE for unit tests only")
        else:
            raise RuntimeError("Ghostscript (gs) not found. Install 'ghostscript' — it is required for fax file processing.")
    # Security posture warnings
    if not settings.require_api_key and not settings.api_key and not settings.fax_disabled:
        print("[warn] API auth is not enforced (REQUIRE_API_KEY=false and API_KEY unset); /fax requests are unauthenticated. Set API_KEY or REQUIRE_API_KEY for production.")
    pu = urlparse(settings.public_api_url)
    insecure = pu.scheme == "http" and pu.hostname not in {"localhost", "127.0.0.1", "::1"}
    if insecure:
        msg = "PUBLIC_API_URL must use HTTPS for provider document retrieval."
        if settings.enforce_public_https and active_outbound() == "phaxio":
            raise RuntimeError(msg)
        print(f"[warn] {msg}")

    # Start periodic cleanup task for artifacts
    if settings.artifact_ttl_days > 0:
        tasks.append(asyncio.create_task(_artifact_cleanup_loop(), name="faxbot-artifact-cleanup"))
    # Init audit logger
    init_audit_logger(
        enabled=settings.audit_log_enabled,
        fmt=settings.audit_log_format,
        filepath=(settings.audit_log_file or None),
        use_syslog=settings.audit_log_syslog,
        syslog_address=(settings.audit_log_syslog_address or None),
    )
    # Start AMI when required by traits (either direction)
    return not settings.fax_disabled and providerHasTrait("any", "requires_ami")


def _deliveries():
    return OutboundStore(_configuration_manager().store)


def _delivery_fields(job_id):
    row = _deliveries().get(job_id)
    reason = None
    if row['state'] == 'reconciliation_required':
        reason = ('Historical delivery has no verified transmission record.' if row['dispatch_mode'] == 'legacy'
                  else 'Transmission outcome is uncertain. Check the original provider before taking action.')
    return {'delivery_state': row['state'], 'dispatch_mode': row['dispatch_mode'],
            'delivery_version': row['version'], 'reconciliation_reason': reason}


def _observe_native(job_id, attempt_id, status, provider, *, event_key, secret=None):
    if (not isinstance(job_id, str) or re.fullmatch('[a-f0-9]{32}', job_id) is None
            or not isinstance(attempt_id, str) or re.fullmatch('[a-f0-9]{32}', attempt_id) is None):
        raise DeliveryConflict('Native result has no verified attempt identity.')
    delivery = _deliveries()
    revision, profile = delivery.configuration.outbound_context(job_id)
    if profile.configuration.provider_id != provider or profile.configuration.manifest is not None:
        raise DeliveryConflict('Native result does not match the original provider.')
    if provider == 'freeswitch':
        expected = revision.values.asterisk_inbound_secret
        if not expected or not isinstance(secret, str) or not hmac.compare_digest(expected.encode(), secret.encode()):
            raise HTTPException(401, detail='Invalid internal callback secret.')
    # FreeSWITCH's channel UUID and bgapi Job-UUID are different namespaces.
    # The authenticated job/attempt locator binds this event; never overwrite
    # the create acknowledgement's SID with a channel UUID.
    return delivery.observe(job_id, attempt_id=attempt_id, profile_id=profile.id,
        provider_sid=job_id if provider == 'sip' else None, status=normalize_status(status), event_key=attempt_id + ':' + event_key)


def _handle_fax_result(event):
    fields = {str(key).lower(): value for key, value in event.items()}
    job_id, attempt = fields.get('jobid'), fields.get('attemptid')
    try:
        status = fields.get('status', '')
        _observe_native(job_id, attempt, status, 'sip', event_key='ami-result:' + str(status))
    except Exception:
        audit_event('native_result_requires_reconciliation', provider='sip')


def _handle_originate_response(event):
    fields = {str(key).lower(): value for key, value in event.items()}
    if str(fields.get('response', '')).lower() != 'failure':
        return
    parts = str(fields.get('actionid', '')).split(':')
    if len(parts) != 3 or parts[0] != 'faxbot':
        return
    try:
        _observe_native(parts[1], parts[2], 'failed', 'sip', event_key='ami-originate-failure')
    except Exception:
        audit_event('native_result_requires_reconciliation', provider='sip')


@app.get("/health")
def health():
    return {"status": "ok"}


def _outbound_profile_ready(revision):
    """Inspect the active captured adapter without contacting a fax provider."""
    identity = revision.profile_id('outbound')
    if identity is None:
        return False
    try:
        profile = _configuration_manager().store.read_profile(identity)
        configuration = profile.configuration
        if configuration.manifest is not None or configuration.provider_id not in {'sip', 'freeswitch'}:
            return service_from_profile(profile).is_configured()
        if configuration.provider_id == 'sip':
            return bool(configuration.settings.get('ami_host')
                        and configuration.settings.get('ami_username')
                        and configuration.credentials.get('ami_password'))
        return bool(fs_cli_available() and configuration.settings.get('gateway_name')
                    and configuration.settings.get('caller_id_number'))
    except (ProviderExecutionError, ConfigurationStoreError, ConfigurationSecretError, ValueError):
        return False


def _readiness_status(request: Request):
    """Shared local readiness snapshot; does not prove provider delivery.

    Inspect active configuration, DB and native dependencies, plus inbound
    storage when required. Never contact a cloud fax provider from this probe.
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
    revision = request.scope["faxbot.configuration"].active
    outbound_ok = _outbound_profile_ready(revision)
    inbound_ok = not settings.inbound_enabled or revision.profile_id("inbound") is not None

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
        except Exception:
            storage_ok = False
            storage_error = "Configured inbound storage is unavailable."

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
    return {
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
        }


@app.get("/health/ready")
def health_ready(request: Request):
    status = _readiness_status(request)
    return JSONResponse(status, status_code=200 if status['status'] == 'ready' else 503)


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


def require_scopes(required: List[str], path: Optional[str] = None, rpm: int | str | None = None):
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
        if rpm is not None and path:
            limit = getattr(settings, rpm) if isinstance(rpm, str) else rpm
            _enforce_rate_limit(info, path, limit)
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
    ob = active_outbound()
    ib = active_inbound()
    # Configured flags
    cfg = {
        "backend": backend,
        "fax_disabled": settings.fax_disabled,
        "max_file_size_mb": settings.max_file_size_mb,
        "hybrid": {
            "outbound": ob,
            "inbound": ib,
            "outbound_explicit": bool(settings.outbound_backend),
            "inbound_explicit": bool(settings.inbound_backend),
        },
        "allow_restart": settings.admin_allow_restart,
        "require_api_key": settings.require_api_key,
        "enforce_public_https": settings.enforce_public_https,
        "phaxio_verify_signature": settings.phaxio_verify_signature,
        "persisted_settings_enabled": settings.enable_persisted_settings,
        "branding": {
            "docs_base": os.getenv("DOCS_BASE_URL", "https://dmontgomery40.github.io/Faxbot"),
            "logo_path": "/admin/ui/faxbot_full_logo.png",
        },
        "mcp": {
            "sse_enabled": settings.enable_mcp_sse,
            "sse_path": settings.mcp_sse_path,
            "require_oauth": settings.require_mcp_oauth,
            "oauth": {
                "issuer": settings.oauth_issuer,
                "audience": settings.oauth_audience,
                "jwks_url": settings.oauth_jwks_url,
            },
            "http_enabled": settings.enable_mcp_http,
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
    return cfg


def _configuration_manager():
    runtime = getattr(app.state, 'configuration_runtime', None)
    if runtime is None or not runtime.serving:
        raise HTTPException(503, detail="Installation configuration is not ready.")
    return runtime.manager


def _settings_view(snapshot):
    return project_admin_settings(snapshot, _configuration_manager().pending_fields(snapshot))


@app.get("/admin/settings", dependencies=[Depends(require_admin)])
def get_admin_settings():
    """Read the desired editor revision; credentials remain opaque."""
    return _settings_view(_configuration_manager().store.read())


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


# Keep the flat legacy patch names, generated from the canonical value model.
# Whole-candidate validation owns constraints and sanitized error reporting.
UpdateSettingsRequest = create_model(
    'UpdateSettingsRequest',
    __config__=ConfigDict(extra='forbid', hide_input_in_errors=True),
    expected_revision_id=(str | None, None),
    **{(field.json_schema_extra or {}).get('patch_name', name): (field.annotation | None, None)
       for name, field in ConfigurationValues.model_fields.items()},
)


@app.put("/admin/settings", dependencies=[Depends(require_admin)])
def update_admin_settings(payload: UpdateSettingsRequest, request: Request):
    """Validate and durably apply one candidate, or stage it for coordinated restart."""
    manager = _configuration_manager()
    expected = request.scope['faxbot.configuration']
    if payload.expected_revision_id is not None and payload.expected_revision_id != expected.desired.id:
        raise HTTPException(409, detail="Settings changed since this editor loaded; reload before applying.")
    changes = payload.model_dump(exclude_unset=True, exclude={'expected_revision_id'})
    snapshot = manager.patch(expected, changes, actor='admin')
    out = _settings_view(snapshot)
    old, new = expected.active.values, snapshot.active.values
    out['_meta'].update(
        backend_changed=old.fax_backend != new.fax_backend,
        outbound_changed=old.effective_outbound != new.effective_outbound,
        inbound_changed=old.effective_inbound != new.effective_inbound,
        storage_changed=old.storage_backend != new.storage_backend,
        restart_recommended=snapshot.pending is not None,
    )
    return out


@app.post("/admin/settings/reload", dependencies=[Depends(require_admin)])
def admin_reload_settings():
    """Read durable active/desired state without importing environment or promoting it."""
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
async def get_health_status(request: Request):
    def inspect():
        from sqlalchemy import or_
        from .db import APIKey
        from .outbound_summary import dashboard_counts
        store = _configuration_manager().store
        now = datetime.utcnow()
        with store.engine.connect() as connection:
            jobs = dashboard_counts(connection, store.delivery_tables['outbound_deliveries'], now=now)
        readiness = _readiness_status(request)
        with SessionLocal() as db:
            db_key_present = db.query(APIKey.id).filter(APIKey.revoked_at.is_(None),
                or_(APIKey.expires_at.is_(None), APIKey.expires_at > now)).first() is not None
        return {
            "timestamp": now.isoformat() + 'Z',
            "backend": readiness['backend'],
            "backend_healthy": readiness['status'] == 'ready',
            "jobs": jobs,
            "inbound_enabled": settings.inbound_enabled,
            "api_keys_configured": bool(settings.api_key) or db_key_present,
            "require_auth": bool(settings.require_api_key or settings.api_key),
        }
    return await run_lifecycle_step(inspect)


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
    return str(providers_dir())


def _manifest_path(provider_id: str) -> str:
    try:
        return str(provider_manifest_path(provider_id))
    except InvalidProviderPath as error:
        raise HTTPException(400, detail=str(error)) from None


class ManifestIn(BaseModel):
    manifest: dict


@app.post("/admin/plugins/http/install", dependencies=[Depends(require_admin)])
def install_http_manifest(payload: ManifestIn):
    document = payload.manifest or {}
    if not document.get("id"):
        raise HTTPException(400, detail="Manifest id is required")
    path = _manifest_path(document["id"])
    try:
        validate_http_provider_document(document)
    except ProviderCatalogError as error:
        raise HTTPException(400, detail=str(error)) from None
    man = HttpManifest.from_dict(document)
    dest_dir = os.path.dirname(path)
    os.makedirs(dest_dir, exist_ok=True)
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
    """Validate a draft without issuing a provider request.

    Provider transmission belongs to a captured, durable outbound attempt.
    Keep the legacy flag for a clear refusal to older administrative clients.
    """
    if not payload.render_only:
        raise HTTPException(
            409,
            detail="Manifest validation cannot send a fax. Install and configure the provider, select it for outbound, then use Send to create a tracked job.",
        )
    document = payload.manifest or {}
    if not document.get("id"):
        raise HTTPException(400, detail="Manifest id required")
    _manifest_path(document["id"])
    try:
        validate_http_provider_document(document)
    except ProviderCatalogError as error:
        raise HTTPException(400, detail=str(error)) from None
    man = HttpManifest.from_dict(document)
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
    return info


class ImportManifestsIn(BaseModel):
    items: Optional[List[dict]] = None
    markdown: Optional[str] = None
    source: Optional[str] = None  # 'repo_scrape' reads bundled API examples


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
            scrape_path = plugin_examples_path()
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
            if not data.get("id"):
                raise ValueError("manifest.id missing")
            path = str(provider_manifest_path(data["id"]))
            validate_http_provider_document(data)
            man = HttpManifest.from_dict(data)
            dest_dir = os.path.dirname(path)
            os.makedirs(dest_dir, exist_ok=True)
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
    # Tunnel helpers (local-only admin actions)
    "tunnel_status_cloudflared_logs_tail": {
        "label": "Cloudflared logs (tail 50)",
        "kind": "shell",
        "cmd": ["sh", "-lc", "docker logs faxbot-cloudflared 2>&1 | tail -n 50"],
        "timeout": 5,
        "backend": ["*"]
    },
    "tunnel_start_cloudflared": {
        "label": "Start Cloudflared (compose profile)",
        "kind": "shell",
        "cmd": ["sh", "-lc", "docker compose --profile cloudflare up -d cloudflared"],
        "timeout": 20,
        "backend": ["*"]
    },
    "tunnel_stop_cloudflared": {
        "label": "Stop Cloudflared",
        "kind": "shell",
        "cmd": ["sh", "-lc", "docker compose stop cloudflared || true"],
        "timeout": 15,
        "backend": ["*"]
    },
    "tunnel_start_wireguard": {
        "label": "Start WireGuard client",
        "kind": "shell",
        "cmd": ["sh", "-lc", "docker compose --profile wireguard up -d wireguard"],
        "timeout": 20,
        "backend": ["*"]
    },
    "tunnel_stop_wireguard": {
        "label": "Stop WireGuard client",
        "kind": "shell",
        "cmd": ["sh", "-lc", "docker compose stop wireguard || true"],
        "timeout": 15,
        "backend": ["*"]
    },
    "tunnel_start_tailscale": {
        "label": "Start Tailscale client",
        "kind": "shell",
        "cmd": ["sh", "-lc", "docker compose --profile tailscale up -d tailscale"],
        "timeout": 20,
        "backend": ["*"]
    },
    "tunnel_stop_tailscale": {
        "label": "Stop Tailscale client",
        "kind": "shell",
        "cmd": ["sh", "-lc", "docker compose stop tailscale || true"],
        "timeout": 15,
        "backend": ["*"]
    },
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
    # Perform a bounded local probe; do not reach out to public endpoints from here
    try:
        import http.client
        from urllib.parse import urlparse as _up
        url = settings.public_api_url or "http://localhost:8080"
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


@app.get("/admin/inbound/callbacks", dependencies=[Depends(require_admin)])
def admin_inbound_callbacks():
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
            "auth": {
                "basic": bool(settings.sinch_inbound_basic_user),
                "hmac": bool(settings.sinch_inbound_hmac_secret),
            },
            "notes": "Set webhook in Sinch Fax console. Optionally use Basic and/or HMAC.",
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
            delivery = _deliveries().deliveries
            q = q.join(delivery, FaxJob.id == delivery.c.id).filter(delivery.c.state == status)
        if backend:
            q = q.filter(FaxJob.backend == backend)
        total = q.count()
        rows = q.order_by(FaxJob.created_at.desc()).offset(offset).limit(limit).all()
        return {
            "total": total,
            "jobs": [
                {
                    "id": r.id,
                    **_delivery_fields(r.id),
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
            **_delivery_fields(job.id),
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


class ProviderIdentityConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(strict=True, gt=0)
    provider_sid: str = Field(strict=True, pattern=r'^[A-Za-z0-9_-]{1,100}$')
    confirm_original_account: StrictBool


def _operator_delivery(job_id: str):
    with SessionLocal() as db:
        if db.get(FaxJob, job_id) is None:
            raise HTTPException(404, detail='Job not found')
    try:
        return _deliveries().operator_view(job_id)
    except DeliveryConflict:
        raise HTTPException(409, detail='Delivery history is unavailable; reload the job before continuing.') from None


@app.get('/admin/fax-jobs/{job_id}/delivery', dependencies=[Depends(require_admin)])
async def admin_delivery_history(job_id: str):
    """Bounded evidence from the accepted account, excluding captured secrets."""
    return await run_lifecycle_step(lambda: _operator_delivery(job_id))


@app.post('/admin/fax-jobs/{job_id}/reconcile')
async def admin_bind_provider_identity(job_id: str, confirmation: ProviderIdentityConfirmation,
                                       principal=Depends(require_admin)):
    """Attach an operator-confirmed receipt; this never authorizes transmission."""
    if confirmation.confirm_original_account is not True:
        raise HTTPException(400, detail='Confirm that this fax ID matches the fax in its original provider account.')

    def bind():
        with SessionLocal() as db:
            if db.get(FaxJob, job_id) is None:
                raise HTTPException(404, detail='Job not found')
        delivery = _deliveries()
        try:
            delivery.bind_provider_identity(job_id,
                expected_version=confirmation.expected_version,
                provider_sid=confirmation.provider_sid,
                actor='key:' + principal['key_id'])
        except DeliveryConflict as exc:
            # Store conflicts are fixed messages and contain no provider payloads.
            raise HTTPException(409, detail=str(exc)) from None
        except ValueError:
            raise HTTPException(400, detail='Invalid provider identity reconciliation input.') from None
        return delivery.operator_view(job_id)

    return await run_lifecycle_step(bind)


@app.get("/admin/fax-jobs/{job_id}/pdf", dependencies=[Depends(require_admin)])
def admin_get_job_pdf(job_id: str):
    """Admin-only: download the outbound fax PDF for a job if present.
    Works for all backends; the API generates/keeps a PDF per job prior to sending.
    """
    with SessionLocal() as db:
        if db.get(FaxJob, job_id) is None:
            raise HTTPException(404, detail='Job not found')
    pdf_path = _outbound_document_path(job_id, '.pdf')
    if pdf_path.is_symlink() or not pdf_path.is_file():
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
    """Read status from the accepted account without authorizing another send."""
    with SessionLocal() as db:
        if db.get(FaxJob, job_id) is None:
            raise HTTPException(404, detail="Job not found")
    try:
        await OutboundPoller(_deliveries()).refresh(job_id)
    except UnsupportedProviderExecutionError:
        raise HTTPException(400, detail="This provider reports status through callbacks; refresh is unsupported.") from None
    except (DeliveryConflict, UnboundProviderProfile):
        raise HTTPException(409, detail="This fax requires reconciliation with its original provider account before refresh.") from None
    except Exception:
        raise HTTPException(502, detail="Provider status is temporarily unavailable. This fax has not been resubmitted.") from None
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if job is None:
            raise HTTPException(404, detail="Job not found")
        return _serialize_job(job)


@app.post("/admin/diagnostics/run", dependencies=[Depends(require_admin)])
async def run_diagnostics():
    """Run bounded, non-destructive diagnostics for v1."""
    ob = active_outbound()
    ib = active_inbound()
    diag: dict[str, Any] = {
        "timestamp": datetime.utcnow().isoformat(),
        "backend": settings.fax_backend,
        "outbound_backend": ob,
        "inbound_backend": ib,
        "checks": {},
    }
    # Legacy single-backend flags (kept for compatibility in UI until hybrid UI lands)
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

    # Hybrid per-direction checks (trait-driven)
    # Outbound
    out: dict[str, Any] = {"backend": ob, "requires_ami": providerHasTrait("outbound", "requires_ami")}
    if ob == "phaxio":
        out["auth_configured"] = bool(settings.phaxio_api_key and settings.phaxio_api_secret)
        out["public_url_https"] = settings.public_api_url.startswith("https://")
    elif ob == "sinch":
        out["auth_configured"] = bool(settings.sinch_project_id and settings.sinch_api_key and settings.sinch_api_secret)
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
            inbound["hmac_configured"] = bool(settings.sinch_inbound_hmac_secret)
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
                    try:
                        mpath = provider_manifest_path(pid)
                        if not os.path.exists(mpath):
                            continue
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

    # Summary
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
    # Ghostscript is required for all backends
    if not sys.get("ghostscript"):
        critical.append("Ghostscript (gs) not installed — required for fax file processing")
    if not sys.get("fax_data_writable"):
        critical.append("Cannot write to fax data dir")
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
    diag["summary"] = {"healthy": len(critical) == 0, "critical_issues": critical, "warnings": warnings}
    return diag


@app.get("/admin/settings/export", dependencies=[Depends(require_admin)])
def export_settings_env():
    """Display the complete desired configuration with opaque secret placeholders."""
    values = _configuration_manager().store.read().desired.values
    content = format_environment(values.to_environment(redact_secrets=True))
    return {"env": content, "env_content": content}


def _export_settings_full_env() -> str:
    return format_environment(_configuration_manager().store.read().desired.values.to_environment())


class PersistSettingsIn(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    content: str | None = None
    path: str | None = None


@app.post("/admin/settings/persist", dependencies=[Depends(require_admin)])
def persist_settings(payload: PersistSettingsIn):
    """Atomically export desired settings to the installation's private recovery file.

    Restoring historical jobs also requires the database and original installation
    key. This export does not activate settings or change the canonical store.
    """
    values = _configuration_manager().store.read().desired.values
    target = Path(values.persisted_env_path).absolute()
    if payload.path is not None and Path(payload.path).absolute() != target:
        raise HTTPException(403, detail="Recovery exports must use the configured persistence path.")
    environment = values.to_environment()
    if payload.content is not None:
        supplied = parse_environment(payload.content, allowed_keys=ConfigurationValues.environment_keys())
        if supplied != environment:
            raise HTTPException(400, detail="Recovery content must match the complete desired revision; use an explicit configuration import to change it.")
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_environment(target, environment, allowed_keys=ConfigurationValues.environment_keys())
    except OSError:
        raise HTTPException(500, detail="Cannot write the configured recovery artifact.") from None
    return {"ok": True, "path": str(target)}

@app.post("/fax", response_model=FaxJobOut, status_code=202, dependencies=[Depends(require_fax_send)])
async def send_fax(request: Request, to: str = Form(...), file: UploadFile = File(...),
                   queue_only: bool = Form(False),
                   idempotency_key: Optional[str] = Header(default=None, alias='Idempotency-Key',
                       description='Optional key for replaying the same fax request; 1 to 128 printable ASCII characters without spaces.'),
                   principal=Depends(require_api_key)):
    manager = _configuration_manager()
    request_identity = None
    keys = request.headers.getlist('idempotency-key')
    if keys:
        if len(keys) != 1:
            raise HTTPException(400, detail='Supply one Idempotency-Key header.')
        try:
            scope = 'key:' + str(principal['key_id']) if principal is not None else 'development'
            validated = RequestIdentity.from_key(idempotency_key, principal_scope=scope, fingerprint='0' * 64)
            max_bytes = await run_lifecycle_step(lambda: manager.store.outbound_replay_max_bytes(validated))
            if max_bytes is None:
                max_bytes = settings.max_file_size_mb * 1024 * 1024
            fingerprint = await fingerprint_upload(file, to=to, queue_only=queue_only,
                max_bytes=max_bytes)
            request_identity = RequestIdentity(scope, validated.idempotency_digest, fingerprint)
            existing = await run_lifecycle_step(lambda: manager.store.find_outbound_replay(request_identity))
        except UploadPreparationError as error:
            raise HTTPException(error.status_code, detail=str(error)) from None
        except IdempotencyConflict as error:
            raise HTTPException(409, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        if existing is not None:
            return await run_lifecycle_step(lambda: _accepted_job_response(existing))
    if queue_only and not settings.fax_disabled:
        raise HTTPException(409, detail="Queue-only request refused because outbound sending is now enabled. Refresh Send before submitting again.")
    revision = request.scope['faxbot.configuration'].active
    identity = revision.profile_id('outbound')
    if identity is None:
        raise HTTPException(409, detail="Outbound fax delivery is disabled in this configuration.")
    profile = manager.store.read_profile(identity)
    ob = profile.configuration.provider_id
    use_manifest = profile.configuration.manifest is not None
    if use_manifest or ob not in {'sip', 'freeswitch'}:
        try:
            service_from_profile(profile)
        except ProviderExecutionError:
            raise HTTPException(400, detail="Selected provider has no supported outbound adapter.") from None
    if not PHONE_RE.match(to):
        raise HTTPException(400, detail="'to' must be E.164 or digits only")
    job_id = uuid.uuid4().hex
    requires_tiff = ((not use_manifest and ob in {'sip', 'freeswitch'})
                     or profile.configuration.traits.get('requires_tiff', False) is True)
    try:
        prepared = await prepare_upload(
            file, job_id=job_id, data_dir=settings.fax_data_dir,
            max_bytes=settings.max_file_size_mb * 1024 * 1024,
            requires_tiff=requires_tiff,
        )
    except UploadPreparationError as error:
        raise HTTPException(error.status_code, detail=str(error)) from None
    pdf_path = prepared.pdf_path
    tiff_path = prepared.tiff_path or ""

    # One transaction accepts the row and its immutable account/profile binding.
    try:
        accepted_at = datetime.utcnow()
        result = FaxJobOut(id=job_id, to=to, status='queued', pages=prepared.pages,
                          backend=ob, created_at=accepted_at, updated_at=accepted_at,
                          delivery_state='held' if revision.values.fax_disabled else 'ready',
                          dispatch_mode='held' if revision.values.fax_disabled else 'normal',
                          delivery_version=1)
        await run_lifecycle_step(lambda: manager.store.accept_outbound(revision, {
            'id': job_id, 'to_number': to, 'file_name': prepared.original_name,
            'tiff_path': tiff_path, 'status': 'queued', 'pages': prepared.pages,
            'created_at': accepted_at, 'updated_at': accepted_at,
        }, request_identity=request_identity))
    except IdempotentReplay as replay:
        prepared.cleanup()
        return await run_lifecycle_step(lambda: _accepted_job_response(replay.job_id))
    except IdempotencyConflict as error:
        prepared.cleanup()
        raise HTTPException(409, detail=str(error)) from None
    except ConfigurationCommitUncertain:
        # COMMIT can succeed after the acknowledgement is lost. Keep the document
        # and never automatically resubmit an uncertain accepted fax.
        raise HTTPException(503, detail=f"Fax acceptance is uncertain. Retain job {job_id} for reconciliation.") from None
    except Exception:
        prepared.cleanup()
        raise
    # Serialize before COMMIT; a second database read must not turn confirmed
    # acceptance into an unidentifiable error and invite duplicate submission.
    audit_event("job_created", job_id=job_id, backend=ob)
    return result


def _accepted_job_response(job_id):
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if job is None:
            raise HTTPException(409, detail='Accepted fax record is unavailable; reconcile before submitting another request.')
        return _serialize_job(job)


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
            logging.getLogger(__name__).error("Artifact cleanup requires operator attention.")
        await asyncio.sleep(interval * 60)


def _outbound_document_path(job_id, suffix):
    if not isinstance(job_id, str) or re.fullmatch('[a-f0-9]{32}', job_id) is None:
        raise HTTPException(404, detail='Document not found.')
    try:
        revision, _ = _configuration_manager().store.outbound_context(job_id)
        root = revision.values.fax_data_dir
    except UnboundProviderProfile:
        # Pre-migration files remain in the installation data directory; no
        # provider/account association is inferred for their transmission.
        root = settings.fax_data_dir
    return Path(root) / (job_id + suffix)


def _cleanup_outbound_documents(cutoff):
    import sqlalchemy as sa
    delivery = _deliveries()
    with delivery.configuration.engine.connect() as connection:
        identities = connection.execute(sa.select(delivery.deliveries.c.id).where(
            delivery.deliveries.c.state.in_(tuple(TERMINAL)),
            delivery.deliveries.c.updated_at < cutoff)).scalars().all()
    for identity in identities:
        for suffix in ('.pdf', '.tiff'):
            try:
                path = _outbound_document_path(identity, suffix)
                if not path.is_symlink():
                    path.unlink(missing_ok=True)
            except (OSError, HTTPException, ConfigurationStoreError):
                audit_event('outbound_retention_requires_attention', job_id=identity)


async def _cleanup_once():
    cutoff = datetime.utcnow() - timedelta(days=max(1, settings.artifact_ttl_days))
    await run_lifecycle_step(lambda: _cleanup_outbound_documents(cutoff))
    with SessionLocal() as db:
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
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), str(expected_token).encode()):
            raise HTTPException(403, detail="Invalid token")
        # Validate expiry if set
        if job.pdf_token_expires_at and datetime.utcnow() > job.pdf_token_expires_at:  # type: ignore[operator]
            raise HTTPException(403, detail="Token expired")

        # Get the PDF path
        pdf_path = _outbound_document_path(job_id, '.pdf')
        if pdf_path.is_symlink() or not pdf_path.is_file():
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


async def _receive_outbound_callback(request, provider):
    from .callback_forms import read_callback_form, CallbackFormError
    try:
        fields, files = await read_callback_form(request)
        result = await run_lifecycle_step(lambda: CapturedCallbacks(_deliveries()).receive(
            provider, request.query_params.get('job_id'), request.query_params.get('attempt_id'),
            fields=fields, files=files, signature=request.headers.get(
                'X-Phaxio-Signature' if provider == 'phaxio' else 'X-SignalWire-Signature', '')))
    except CallbackFormError as error:
        raise HTTPException(error.status_code, detail=str(error)) from None
    except CallbackRejected:
        raise HTTPException(401, detail='Callback could not be authenticated for this attempt.') from None
    except DeliveryConflict:
        raise HTTPException(409, detail='Callback does not match the current delivery attempt.') from None
    return {'ok': True, 'applied': result}


@app.post('/phaxio-callback')
async def phaxio_callback(request: Request):
    return await _receive_outbound_callback(request, 'phaxio')


def _serialize_job(job: FaxJob) -> FaxJobOut:
    j = cast(Any, job)
    return FaxJobOut(
        id=j.id,
        **_delivery_fields(j.id),
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


@app.get("/inbound", response_model=List[InboundFaxOut], dependencies=[Depends(require_scopes(["inbound:list"], path="/inbound", rpm="inbound_list_rpm"))])
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


@app.get("/inbound/{inbound_id}", response_model=InboundFaxOut, dependencies=[Depends(require_scopes(["inbound:read"], path="/inbound/{id}", rpm="inbound_get_rpm"))])
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
            inbound_backend=active_inbound(),
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


def _installed_plugins(snapshot=None) -> list[dict[str, Any]]:
    """Describe one installed definition per provider identity, with manifest precedence."""
    manager = _configuration_manager()
    snapshot = snapshot or manager.store.read()
    current = snapshot.desired.values.effective_outbound
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
    # Use the same validated catalog as activation. Installed overrides replace
    # the built-in card instead of exposing two contradictory definitions.
    by_id = {item['id']: {**item, 'source': 'builtin'} for item in items}
    catalog = manager.catalog_loader(snapshot.desired.values)
    for identity in sorted(catalog.provider_ids):
        definition = catalog.get(identity)
        if definition.manifest is None:
            continue
        manifest = definition.manifest.as_dict()
        actions = manifest['actions']
        by_id[identity] = {
            'id': identity,
            'name': str(manifest.get('name') or identity),
            'version': str(manifest.get('version') or '1.0.0'),
            'source': 'manifest',
            'description': str(manifest.get('description') or 'Installed HTTP provider manifest.'),
            'categories': ['outbound'],
            'capabilities': [capability for action, capability in
                             (('send_fax', 'send'), ('get_status', 'get_status')) if action in actions],
            'enabled': current == identity,
            'configurable': True,
        }
    return list(by_id.values())


@app.get("/plugins", dependencies=[Depends(require_admin)])
def list_plugins():
    if not settings.feature_v3_plugins:
        return _plugins_disabled_response()
    snapshot = _configuration_manager().store.read()
    items = _installed_plugins(snapshot)
    for item in items:
        item['enabled'] = _plugin_view(snapshot, item['id'])['enabled']
    return {"items": items}


def _plugin_view(snapshot, plugin_id, role=None):
    from .config_plugin_fields import PLUGIN_FIELDS
    from .config_views import mask_secret as opaque_secret
    values = snapshot.desired.values
    role = role or ('storage' if plugin_id in {'local', 's3'} else 'outbound')
    if role not in {'outbound', 'inbound', 'storage'}:
        raise HTTPException(400, detail='Invalid plugin role.')
    state = snapshot.desired.plugins.as_dict()
    selected = values.storage_backend if role == 'storage' else getattr(values, 'effective_' + role)
    enabled = selected == plugin_id and state['roles'][role]['enabled']
    if plugin_id in PLUGIN_FIELDS:
        configuration = {}
        for key, name in PLUGIN_FIELDS[plugin_id].items():
            value = getattr(values, name)
            configuration[key] = opaque_secret(value) if (ConfigurationValues.model_fields[name].json_schema_extra or {}).get('secret') else value
    else:
        _configuration_manager().catalog_loader(values).get(plugin_id)
        # Arbitrary manifest schemas can designate nested fields as credentials.
        # Do not expose private values from unrecognized schema paths. Clients
        # merge only fields they intentionally replace; omission preserves them.
        def private_view(value):
            if isinstance(value, dict):
                return {key: private_view(item) for key, item in value.items()}
            if isinstance(value, list):
                return [private_view(item) for item in value]
            return '' if value == '' or value is None else '***'
        configuration = private_view(state['settings'].get(plugin_id, {}))
    return {'enabled': enabled, 'settings': configuration, 'role': role,
            '_meta': _settings_view(snapshot)['_meta']}


@app.get("/plugins/{plugin_id}/config", dependencies=[Depends(require_admin)])
def get_plugin_config(plugin_id: str, role: str | None = None):
    if not settings.feature_v3_plugins:
        return _plugins_disabled_response()
    return _plugin_view(_configuration_manager().store.read(), plugin_id.lower(), role)


class UpdatePluginConfigIn(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    enabled: Optional[bool] = None
    settings: Optional[dict[str, Any]] = None
    role: str | None = None
    expected_revision_id: str | None = None


@app.put("/plugins/{plugin_id}/config", dependencies=[Depends(require_admin)])
def update_plugin_config(plugin_id: str, payload: UpdatePluginConfigIn, request: Request):
    if not settings.feature_v3_plugins:
        return _plugins_disabled_response()
    expected = request.scope['faxbot.configuration']
    if payload.expected_revision_id is not None and payload.expected_revision_id != expected.desired.id:
        raise HTTPException(409, detail='Plugin settings changed; reload before applying.')
    snapshot = _configuration_manager().patch_plugin(expected, plugin_id.lower(),
        settings=payload.settings, enabled=payload.enabled, role=payload.role, actor='admin')
    return {'ok': True, 'path': snapshot.desired.values.faxbot_config_path,
            **_plugin_view(snapshot, plugin_id.lower(), payload.role)}


@app.get("/plugin-registry")
def plugin_registry():
    if not settings.feature_v3_plugins:
        return _plugins_disabled_response()
    # Try to load curated registry file; fallback to built-in list
    try:
        reg_path = plugin_registry_path()
        if os.path.exists(reg_path):
            import json as _json
            with open(reg_path, "r", encoding="utf-8") as f:
                return _json.load(f)
    except Exception:
        pass
    return {"items": _installed_plugins(), "note": "default registry"}
@app.post('/signalwire-callback')
async def signalwire_callback(request: Request):
    return await _receive_outbound_callback(request, 'signalwire')


class FSOutboundResultIn(BaseModel):
    attempt_id: Optional[str] = None
    job_id: Optional[str] = None
    fax_status: Optional[str] = None
    fax_result_text: Optional[str] = None
    fax_result_code: Optional[str] = None
    fax_document_transferred_pages: Optional[int] = None
    uuid: Optional[str] = None


@app.post("/_internal/freeswitch/outbound_result")
def freeswitch_outbound_result(payload: FSOutboundResultIn, x_internal_secret: Optional[str] = Header(default=None)):
    status = str(payload.fax_status or '').lower()
    status = {'true': 'success', 'false': 'failed', 'ok': 'success', 'fail': 'failed'}.get(status, status)
    try:
        applied = _observe_native(payload.job_id, payload.attempt_id, status, 'freeswitch',
            event_key='fs-result:' + status, secret=x_internal_secret)
    except (DeliveryConflict, ValueError):
        raise HTTPException(409, detail='Native result does not match a verified delivery attempt.') from None
    return {'ok': True, 'applied': applied}


# Terminal WebSocket endpoint for Admin Console
@app.websocket("/admin/terminal")
async def admin_terminal_websocket(
    websocket: WebSocket,
    api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """WebSocket terminal for Admin Console - requires admin authentication."""
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
    
    # Authentication does not override the installation's host execution gate.
    if not _admin_exec_enabled():
        await websocket.close(code=1008, reason="Administrative execution is disabled")
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
