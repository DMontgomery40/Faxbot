import os
import shutil
import re
import uuid
import asyncio
import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
import tempfile
from typing import Optional, Any, List, Dict, Literal
import time
import sqlalchemy as sa
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Depends, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .config import (
    settings,
    reload_settings,
    active_outbound,
    active_inbound,
    get_provider_traits,
    providerHasTrait,
)
from .db import init_db, SessionLocal, FaxJob
from .models import FaxJobOut
from .conversion import ensure_dir
from .documents import prepare_upload, UploadPreparationError
from .ami import ami_client
from . import sip_calls, sip_fax_mode, sip_network
from .sip_http import router as sip_router, sip_trunk_message, watch_public_address
from .hylafax_http import router as hylafax_router
from .freeswitch_service import originate_txfax, fs_cli_available
import hmac
import hashlib
from urllib.parse import urlparse
from fastapi.responses import StreamingResponse
import json
from .audit import init_audit_logger, close_audit_logger, audit_event
from .audit import query_recent_logs
from .storage import get_storage
from .plugins.http_provider import HttpManifest, HttpProviderRuntime
from .config_paths import (
    InvalidProviderPath,
    provider_manifest_path, providers_dir,
)

from pydantic import BaseModel, ConfigDict, Field, StrictBool, create_model
from .config_values import ENVIRONMENT_MANAGED_REFUSAL, ConfigurationValues, ConfigurationValueError
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
from .request_identity import (RequestIdentity, IdempotentReplay, IdempotencyConflict, digest_upload,
                               request_fingerprints)
from .routing.numbers import InvalidNumber, normalize_number
from .access.runtime import AccessRuntime
from .access.transport import CredentialTransport
from .access.catalog import KEY_SCOPES
from .access.mutation_types import IntegrationKeyValues, MutationDeniedError, MutationReason, VersionedEntity
from .access.types import AccessError, AccessUnavailableError, ResourceRef
from .access.http import router as authentication_router, PrivateAuthMiddleware, access_error_response
from .access.management_http import router as management_router
from .access.http import require_identity, runtime as access_runtime, private_operation
from .access.http import PRIVATE_HEADERS, private_response_path, utcnow as access_utcnow
from .access.configuration_access import configuration_write_receipt
from .access.fax_resources import FaxAccessError
from .routing.http import router as routing_router
from .intake.http import router as intake_router
from .direct.http import router as direct_router
from .cases.http import router as cases_router
from .inbound.http import router as inbound_router
from .work.http import imports_router, router as work_router
from .routing.transport import RoutedTransport
from .batching.http import router as batching_router, summaries as batching_summaries
from .diagnostics_report import router as diagnostics_router
from .batching.transport import BatchingTransport
from .batching import acceptance as batching_acceptance, results as batching_results
import logging

# How long startup gives Asterisk to accept Faxbot's manager login before the
# worker starts; a refused login ends the wait at once.
AMI_STARTUP_WAIT_SECONDS = 10.0


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
        application.state.credential_transport = CredentialTransport(os.environ)
        application.state.access_runtime = await run_lifecycle_step(lambda: AccessRuntime(
            runtime.manager.store, docs_base=runtime.candidate.values.docs_base_url))
        # Inbound faxes stored without an access resource are placed in the unassigned inbox.
        await run_lifecycle_step(application.state.access_runtime.inbound.backfill)
        with runtime.frame(runtime.candidate):
            try:
                owns_ami = await _initialize_runtime(tasks)
                if owns_ami:
                    ami_client.on_fax_result(_handle_fax_result)
                    ami_client.on_originate_response(_handle_originate_response)
                    sip_calls.attach(ami_client, runtime.manager.store.engine)
                    # A T.38 call whose fax data never came back switches new calls to audio fax.
                    sip_fax_mode.attach(ami_client, runtime)
                    tasks.append(asyncio.create_task(ami_client.connect(), name="faxbot-ami-connect"))
                    # Start without the fax engine rather than lock people out of the
                    # console that fixes it; the client keeps retrying in the background.
                    if not await ami_client.settle(AMI_STARTUP_WAIT_SECONDS):
                        logging.getLogger(__name__).warning(ami_client.engine_message())
                async with AsyncExitStack() as stack:
                    _mount_enabled_mcp(application, mounts)
                    for mount in mounts:
                        await stack.enter_async_context(mount.app.router.lifespan_context(mount.app))
                    await run_lifecycle_step(runtime.publish_ready)
                    delivery = OutboundStore(runtime.manager.store)
                    # Faxes to the installation's own numbers are delivered inside Faxbot (routing/local.py).
                    from .routing.local import installation_route
                    worker = OutboundWorker(delivery, BatchingTransport(
                        RoutedTransport(CapturedTransport(delivery, runtime, ami=ami_client),
                                        local=installation_route(application, runtime))))
                    tasks.append(asyncio.create_task(worker.run(), name='faxbot-outbound-worker'))
                    tasks.append(asyncio.create_task(OutboundPoller(delivery).run(), name='faxbot-outbound-poller'))
                    # The task's frame keeps the startup values; the watcher reads the current ones.
                    tasks.append(asyncio.create_task(watch_public_address(
                        values_source=lambda: runtime.manager.store.read().active.values, runtime=runtime),
                        name='faxbot-public-address'))
                    # The network check for fax over IP at every start (it also decides T.38 for new calls).
                    tasks.append(asyncio.create_task(sip_network.check_at_start(runtime), name='faxbot-network-check'))
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
        application.state.access_runtime = None
        application.state.credential_transport = None
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
app.add_middleware(PrivateAuthMiddleware)
app.add_exception_handler(AccessError, access_error_response)
app.include_router(authentication_router)
app.include_router(management_router)
app.include_router(routing_router)
app.include_router(intake_router)
app.include_router(direct_router)
app.include_router(cases_router)
app.include_router(inbound_router)
app.include_router(work_router)
app.include_router(imports_router)
app.include_router(hylafax_router)
app.include_router(batching_router)
app.include_router(diagnostics_router)


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
    if request.url.path.startswith('/auth/') or request.url.path.startswith('/access/'):
        return JSONResponse({'detail':'Invalid access request.'}, status_code=422)
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


ALLOWED_CT = {"application/pdf", "text/plain"}


# In-memory per-key rate limiter (fixed window, per minute)
_rate_buckets: dict[str, dict[str, int]] = {}


def _enforce_rate_limit(info: dict, path: str, limit: Optional[int] = None):
    # Callers pass the authenticated caller's stable replay scope as key_id.
    # Choose provided per-route limit, else global
    limit = settings.max_requests_per_minute if limit is None else int(limit)
    if not limit or limit <= 0:
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
        logging.getLogger("faxbot").warning(
            "MCP over SSE (ENABLE_MCP_SSE) is removed in the next release; use Streamable HTTP (ENABLE_MCP_HTTP).")
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
    if not settings.api_key:
        print("[info] No installation key (API_KEY) is saved. Every API request still needs an API key or a signed-in "
              "console session. An Owner can save an installation key through the settings API for owner recovery.")
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
    return _ami_required()


def _provider_requires_ami(provider_id: str) -> bool:
    try:
        return (get_provider_traits(provider_id).get("traits") or {}).get("requires_ami") is True
    except Exception:
        return False


def _ami_required() -> bool:
    """Connect AMI when sending is on and Asterisk serves this installation.

    That is when the provider in either direction needs AMI, or when an extra
    outbound route (FAX_OUTBOUND_ROUTES) does: a fax routed to SIP as an
    alternative is sent through the same Asterisk connection.
    """
    if settings.fax_disabled:
        return False
    if providerHasTrait("any", "requires_ami"):
        return True
    return any(_provider_requires_ami(identity) for identity in settings.outbound_route_providers)


def _deliveries():
    return OutboundStore(_configuration_manager().store)


def _observe_native(job_id, attempt_id, status, provider, *, event_key, secret=None, error=None):
    if (not isinstance(job_id, str) or re.fullmatch('[a-f0-9]{32}', job_id) is None
            or not isinstance(attempt_id, str) or re.fullmatch('[a-f0-9]{32}', attempt_id) is None):
        raise DeliveryConflict('Native result has no verified attempt identity.')
    delivery = _deliveries()
    revision, profile = delivery.attempt_context(job_id, attempt_id)
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
        provider_sid=job_id if provider == 'sip' else None, status=normalize_status(status), event_key=attempt_id + ':' + event_key,
        error=error)


def _handle_fax_result(event):
    fields = {str(key).lower(): value for key, value in event.items()}
    job_id, attempt = fields.get('jobid'), fields.get('attemptid')
    try:
        # A call that carried several faxes gives each its own outcome from the confirmed pages.
        if batching_results.apply_fax_result(_deliveries(), event, failure_sentence=sip_calls.result_summary(event)):
            return
        status = fields.get('status', '')
        _observe_native(job_id, attempt, status, 'sip', event_key='ami-result:' + str(status),
                        error=sip_calls.result_summary(event))
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
        if batching_results.apply_originate_failure(_deliveries(), event,
                                                    failure_sentence=sip_calls.originate_summary(event)):
            return
        _observe_native(parts[1], parts[2], 'failed', 'sip', event_key='ami-originate-failure',
                        error=sip_calls.originate_summary(event))
    except Exception:
        audit_event('native_result_requires_reconciliation', provider='sip')


app.include_router(sip_router)


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


NO_PROVIDER = "No fax provider set up yet."


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
    ami_connected = bool(ami_client._connected.is_set())

    # Required traits for readiness
    ami_required = providerHasTrait("any", "requires_ami")
    # One plain reason when the fax engine is missing: no provider, or Faxbot cannot sign in or reach it,
    # or the SIP trunk it would call through is not set up yet.
    trunk_message = sip_trunk_message(settings)
    message = NO_PROVIDER if not ob else (ami_client.engine_message() if ami_required else None) or trunk_message
    storage_required = settings.inbound_enabled and providerHasTrait("inbound", "needs_storage")
    ready = bool(
        db_ok and gs_installed and outbound_ok and inbound_ok and
        (not ami_required or ami_connected) and
        (not storage_required or storage_ok) and trunk_message is None
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
            **({"message": message} if message else {}),
        }


@app.get("/health/ready")
def health_ready(request: Request):
    status = _readiness_status(request)
    return JSONResponse(status, status_code=200 if status['status'] == 'ready' else 503)


# Every protected route either declares its permission with require_permission
# or authenticates with require_identity and checks permission on the resource.
from .access.route_policy import authorize as authorize_operation, require_permission  # noqa: E402


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


class PublicDetailErrorResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    detail: str


class ConfigurationInputError(BaseModel):
    model_config = ConfigDict(extra='forbid')
    loc: list[str | int]
    type: str
    msg: Literal['Invalid configuration input.']


class ConfigurationValidationErrorResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', json_schema_extra={'examples': [
        {'detail': [{'loc': ['body', 'max_file_size_mb'], 'type': 'int_parsing',
                     'msg': 'Invalid configuration input.'}]},
    ]})
    detail: list[ConfigurationInputError]


_PUBLIC_DETAIL_RESPONSES = {status: {'model': PublicDetailErrorResponse, 'description': description}
    for status, description in (
        (400, 'Configuration or provider metadata could not be validated.'),
        (401, 'Authentication required or credentials no longer valid.'),
        (403, 'Transport, browser request verification or operation is not permitted.'),
        (404, 'The v3 plugins feature is disabled.'),
        (409, 'Configuration changed. Reload before applying edits.'),
        (429, 'Too many authentication attempts. Try again later.'),
        (503, 'Configuration, authentication or access service is unavailable.'),
    )}
_CONFIGURATION_VALIDATION_RESPONSES = {422: {'model': ConfigurationValidationErrorResponse,
    'description': 'Invalid configuration input; raw input and error context are omitted.'}}
_CONFIGURATION_READ_RESPONSES = {status: _PUBLIC_DETAIL_RESPONSES[status] for status in (401, 403, 429, 503)}
_PROVIDER_READ_RESPONSES = {status: _PUBLIC_DETAIL_RESPONSES[status] for status in (400, 401, 403, 404, 429, 503)}


@app.get("/admin/config", responses=_CONFIGURATION_READ_RESPONSES)
def get_admin_config(request: Request, identity=Depends(require_identity)):
    """Return sanitized effective configuration for operators.
    Requires current settings:read and uses one authorized active snapshot.
    """
    access = access_runtime(request)
    snapshot = access.configuration_access.settings(identity.actor)
    values = snapshot.active.values
    backend = values.fax_backend
    ob = values.effective_outbound
    ib = values.effective_inbound
    # Configured flags
    cfg = {
        "backend": backend,
        "fax_disabled": values.fax_disabled,
        "max_file_size_mb": values.max_file_size_mb,
        "hybrid": {
            "outbound": ob,
            "inbound": ib,
            "outbound_explicit": bool(values.outbound_backend),
            "inbound_explicit": bool(values.inbound_backend),
        },
        "allow_restart": values.admin_allow_restart,
        "require_api_key": values.require_api_key,
        "enforce_public_https": values.enforce_public_https,
        "phaxio_verify_signature": values.phaxio_verify_signature,
        "persisted_settings_enabled": values.enable_persisted_settings,
        "branding": {
            "docs_base": values.docs_base_url,
            "logo_path": "/admin/ui/faxbot_full_logo.png",
        },
        "mcp": {
            "sse_enabled": values.enable_mcp_sse,
            "sse_path": values.mcp_sse_path,
            "require_oauth": values.require_mcp_oauth,
            "oauth": {
                "issuer": values.oauth_issuer,
                "audience": values.oauth_audience,
                "jwks_url": values.oauth_jwks_url,
            },
            "http_enabled": values.enable_mcp_http,
            "http_path": values.mcp_http_path,
        },
        "audit_log_enabled": values.audit_log_enabled,
        "rate_limits": {
            "global_rpm": values.max_requests_per_minute,
            "inbound_list_rpm": values.inbound_list_rpm,
            "inbound_get_rpm": values.inbound_get_rpm,
        },
        "inbound": {
            "enabled": values.inbound_enabled,
            "retention_days": values.inbound_retention_days,
            "token_ttl_minutes": values.inbound_token_ttl_minutes,
        },
        "storage": {
            "backend": values.storage_backend,
            "s3_bucket": (values.s3_bucket[:4] + "…" if values.s3_bucket else ""),
            "s3_region": values.s3_region,
            "s3_prefix": values.s3_prefix,
            "s3_endpoint_url": values.s3_endpoint_url,
            "s3_kms_key_id": (values.s3_kms_key_id[:8] + "…" if values.s3_kms_key_id else ""),
        },
        "backend_configured": {
            "phaxio": bool(values.phaxio_api_key and values.phaxio_api_secret),
            "sinch": bool(values.sinch_project_id and values.sinch_api_key and values.sinch_api_secret),
            "signalwire": bool(values.signalwire_space_url and values.signalwire_project_id and values.signalwire_api_token),
            "documo": bool(values.documo_api_key),
            "humblefax": bool(values.humblefax_access_key and values.humblefax_secret_key),
            "efax": bool(values.efax_app_id and values.efax_api_key and values.efax_user_id),
            "sip_ami_configured": bool(values.ami_username and values.ami_password),
            "sip_ami_password_default": (values.ami_password == "changeme"),
        },
        "public_api_url": values.public_api_url,
    }
    # v3 plugins status (feature-gated)
    if values.feature_v3_plugins:
        cfg["v3_plugins"] = {
            "enabled": True,
            "active_outbound": ob,
            "config_path": values.faxbot_config_path,
            "plugin_install_enabled": values.feature_plugin_install,
        }
    return cfg


def _configuration_manager():
    runtime = getattr(app.state, 'configuration_runtime', None)
    if runtime is None or not runtime.serving:
        raise HTTPException(503, detail="Installation configuration is not ready.")
    return runtime.manager


def _environment_managed():
    runtime = getattr(app.state, 'configuration_runtime', None)
    return getattr(runtime, 'env_managed', frozenset())


def _refuse_environment_managed(expected, changes):
    """A credential set in the environment is changed there, never through the API; nothing applies."""
    managed = _environment_managed()
    current = expected.desired.values
    if any(name in managed and value is not None and value != getattr(current, name)
           for name, value in changes.items()):
        raise HTTPException(409, detail=ENVIRONMENT_MANAGED_REFUSAL)


def _settings_view(snapshot):
    return project_admin_settings(snapshot, _configuration_manager().pending_fields(snapshot),
                                  env_managed=_environment_managed(), environment=os.environ)


@app.get("/admin/settings", responses={**_CONFIGURATION_READ_RESPONSES, **_CONFIGURATION_VALIDATION_RESPONSES})
def get_admin_settings(request: Request, identity=Depends(require_identity)):
    """Read the desired editor revision; credentials remain opaque."""
    return _settings_view(access_runtime(request).configuration_access.settings(identity.actor))


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
    efax_app_id: Optional[str] = None
    efax_api_key: Optional[str] = None
    efax_user_id: Optional[str] = None


_PERMISSION_RESPONSES = {status: _PUBLIC_DETAIL_RESPONSES[status] for status in (401, 403, 429, 503)}


@app.post("/admin/settings/validate", dependencies=[Depends(require_permission('providers:write'))],
          responses={**_PERMISSION_RESPONSES, **_CONFIGURATION_VALIDATION_RESPONSES})
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
    elif payload.backend == "efax":
        # GET /health needs no sign-in; a sign-in with the given keys proves them without sending a fax.
        from .efax_service import EfaxError, EfaxFaxService
        results["checks"]["api_answering"] = await EfaxFaxService("", "", "").health()
        if payload.efax_app_id and payload.efax_api_key and payload.efax_user_id:
            try:
                await EfaxFaxService(payload.efax_app_id, payload.efax_api_key, payload.efax_user_id).authenticate()
                results["checks"]["auth"] = True
            except (EfaxError, ValueError) as error:
                results["checks"]["auth"] = False
                results["checks"]["error"] = str(error)
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


# Keep the flat legacy patch names, generated from the canonical value model.
# Whole-candidate validation owns constraints and sanitized error reporting.
UpdateSettingsRequest = create_model(
    'UpdateSettingsRequest',
    __config__=ConfigDict(extra='forbid', hide_input_in_errors=True, json_schema_extra={
        'description': 'Submit only changed fields and the loaded expected_revision_id. An empty object submits no field edits.',
        'examples': [{}],
    }),
    expected_revision_id=(str | None, None),
    **{(field.json_schema_extra or {}).get('patch_name', name): (field.annotation | None, None)
       for name, field in ConfigurationValues.model_fields.items()},
)


class ConfigurationWriteMeta(BaseModel):
    model_config = ConfigDict(extra='forbid')
    active_revision_id: str = Field(min_length=1, max_length=40, pattern=r'^[ -~]+$')
    desired_revision_id: str = Field(min_length=1, max_length=40, pattern=r'^[ -~]+$')
    generation: int = Field(ge=1)
    apply_state: Literal['applied', 'pending_restart']
    restart_recommended: bool


_CONFIGURATION_WRITE_EXAMPLES = {
    'applied': {'summary': 'Confirmed applied configuration', 'value': {
        'ok': True, 'changed': True, '_meta': {
            'active_revision_id': '00000000-0000-4000-8000-000000000001',
            'desired_revision_id': '00000000-0000-4000-8000-000000000001',
            'generation': 2, 'apply_state': 'applied', 'restart_recommended': False,
        },
    }},
    'pending_restart': {'summary': 'Confirmed desired configuration pending restart', 'value': {
        'ok': True, 'changed': True, '_meta': {
            'active_revision_id': '00000000-0000-4000-8000-000000000001',
            'desired_revision_id': '00000000-0000-4000-8000-000000000002',
            'generation': 3, 'apply_state': 'pending_restart', 'restart_recommended': True,
        },
    }},
}


class ConfigurationWriteResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', json_schema_extra={
        'examples': [example['value'] for example in _CONFIGURATION_WRITE_EXAMPLES.values()],
    })
    ok: Literal[True]
    changed: bool
    metadata: ConfigurationWriteMeta = Field(alias='_meta')


_CONFIGURATION_WRITE_RESPONSES = {
    **{status: _PUBLIC_DETAIL_RESPONSES[status] for status in (400, 401, 403, 409, 429, 503)},
    **_CONFIGURATION_VALIDATION_RESPONSES,
    200: {'description': 'Confirmed canonical write receipt; read configuration values separately.',
          'content': {'application/json': {'examples': _CONFIGURATION_WRITE_EXAMPLES}}},
}


@app.put("/admin/settings", response_model=ConfigurationWriteResponse, responses=_CONFIGURATION_WRITE_RESPONSES)
def update_admin_settings(payload: UpdateSettingsRequest, request: Request, identity=Depends(require_identity)):
    """Validate and durably apply one candidate, or stage it for coordinated restart."""
    manager = _configuration_manager()
    expected = request.scope['faxbot.configuration']
    access = access_runtime(request)
    access.configuration_access.prepare_settings_write(identity.actor, expected, payload.expected_revision_id)
    changes = payload.model_dump(exclude_unset=True, exclude={'expected_revision_id'})
    _refuse_environment_managed(expected, changes)
    snapshot = manager.patch_authorized(expected, changes, principal=identity.actor, control=access.control)
    return configuration_write_receipt(expected, snapshot)


@app.post("/admin/settings/reload", responses={**_CONFIGURATION_READ_RESPONSES, **_CONFIGURATION_VALIDATION_RESPONSES})
def admin_reload_settings(request: Request, identity=Depends(require_identity)):
    """Read durable active/desired state without importing environment or promoting it."""
    return _settings_view(access_runtime(request).configuration_access.settings(identity.actor))


@app.post("/admin/restart", dependencies=[Depends(require_permission('host:restart', audit=True))],
          responses=_PERMISSION_RESPONSES)
async def admin_restart():
    """Optional: restart the API process (for containerized deployments). Controlled by ADMIN_ALLOW_RESTART."""
    if not settings.admin_allow_restart:
        raise HTTPException(403, detail="Restart not allowed")
    async def _exit_soon():
        await asyncio.sleep(0.5)
        os._exit(0)
    asyncio.create_task(_exit_soon())
    return {"ok": True, "note": "Process will exit; container manager should restart it."}


def _waiting_for_line(store, now):
    try:
        from .capacity import Capacity
        from .batching.store import tables as batching_tables, waiting_ids
        values = store.read().active.values
        return Capacity(store.engine).waiting_for_line(values, now, waiting=waiting_ids(batching_tables(store.engine)))
    except Exception:
        return 0


def _waiting_reason(store, job_id, now):
    """One sentence while a sent fax waits for room on its number or the trunk (capacity.py), else None."""
    try:
        from .capacity import Capacity
        return Capacity(store.engine).waiting_sentence(job_id, store.read().active.values, now)
    except Exception:
        return None


@app.get("/admin/health-status", dependencies=[Depends(require_permission('diagnostics:read'))],
         responses=_PERMISSION_RESPONSES)
async def get_health_status(request: Request):
    def inspect():
        from sqlalchemy import or_
        from .db import APIKey
        from .outbound_summary import dashboard_counts
        store = _configuration_manager().store
        now = datetime.utcnow()
        with store.engine.connect() as connection:
            jobs = dashboard_counts(connection, store.delivery_tables['outbound_deliveries'], now=now)
        # Faxes ready to go that wait for room on their number or the trunk (Overview: "Waiting for a free line").
        jobs['waiting_for_line'] = _waiting_for_line(store, now)
        readiness = _readiness_status(request)
        with SessionLocal() as db:
            db_key_present = db.query(APIKey.id).filter(APIKey.revoked_at.is_(None),
                or_(APIKey.expires_at.is_(None), APIKey.expires_at > now)).first() is not None
        return {
            "timestamp": now.isoformat() + 'Z',
            "backend": readiness['backend'],
            "backend_healthy": readiness['status'] == 'ready',
            # One plain reason when sending cannot work, such as the fax engine refusing Faxbot's login.
            "backend_message": readiness.get('message'),
            "jobs": jobs,
            "inbound_enabled": settings.inbound_enabled,
            "api_keys_configured": bool(settings.api_key) or db_key_present,
            # Every API route needs an API key or a signed-in session.
            "require_auth": True,
        }
    return await run_lifecycle_step(inspect)


@private_operation
def _visible_counts(service, actor):
    """Count only rows this actor could list; the key total requires keys:manage."""
    with service.store.transaction() as connection:
        now = access_utcnow()
        def visible(permission, kind):
            ids = service.control.visible_resource_ids_on(connection, actor, permission, kind, now=now)
            return connection.execute(sa.select(sa.func.count()).select_from(ids.subquery())).scalar_one()
        keys = None
        if service.control.authorize_on(connection, actor, 'keys:manage', ResourceRef('installation'), now=now).allowed:
            keys = connection.execute(sa.select(sa.func.count()).select_from(service.store.tables['api_keys'])).scalar_one()
        return {"fax_jobs": visible('fax:read', 'outbound'), "api_keys": keys,
                "inbound_fax": visible('inbound:list', 'inbound')}


@app.get("/admin/db-status", responses=_PERMISSION_RESPONSES)
def admin_db_status(request: Request, identity=Depends(require_permission('diagnostics:read'))):
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
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            connected = True
    except Exception as e:  # pragma: no cover
        connected = False
        err = str(e)
    counts = _visible_counts(access_runtime(request), identity.actor) if connected else {}

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


@app.post("/admin/plugins/http/install", dependencies=[Depends(require_permission('providers:install', audit=True))],
          responses={**_PERMISSION_RESPONSES, 404: _PUBLIC_DETAIL_RESPONSES[404]})
def install_http_manifest(payload: ManifestIn, request: Request):
    if not request.scope["faxbot.configuration"].active.values.feature_v3_plugins:
        return _plugins_disabled_response()
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


@app.post("/admin/plugins/http/validate", dependencies=[Depends(require_permission('providers:write'))],
          responses=_PERMISSION_RESPONSES)
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


@app.post("/admin/plugins/http/import-manifests",
          dependencies=[Depends(require_permission('providers:install', audit=True))],
          responses={**_PERMISSION_RESPONSES, 404: _PUBLIC_DETAIL_RESPONSES[404]})
def import_http_manifests(payload: ImportManifestsIn, request: Request):
    """Bulk import provider manifests from a JSON list or Markdown.
    For markdown, extracts JSON code fences and imports objects that look like manifests.
    """
    if not request.scope["faxbot.configuration"].active.values.feature_v3_plugins:
        return _plugins_disabled_response()
    candidates: List[dict] = []
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


@app.get("/admin/logs", dependencies=[Depends(require_permission('logs:read'))], responses=_PERMISSION_RESPONSES)
def admin_logs(q: Optional[str] = None, event: Optional[str] = None, since: Optional[str] = None, limit: int = 200):
    """Return recent audit logs from in-process ring buffer with simple filtering.
    For persistent logs, configure AUDIT_LOG_FILE and use external tooling; this endpoint focuses on interactive UI needs.
    """
    try:
        rows = query_recent_logs(q=q, event=event, since=since, limit=limit)
    except Exception as e:
        raise HTTPException(500, detail=str(e))
    return {"items": rows, "count": len(rows)}


@app.get("/admin/logs/tail", dependencies=[Depends(require_permission('logs:read'))], responses=_PERMISSION_RESPONSES)
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


def _admin_exec_enabled() -> bool:
    # The console terminal: on when the console is served here, unless ENABLE_ADMIN_EXEC says otherwise.
    val = os.getenv("ENABLE_ADMIN_EXEC", None)
    if val is not None:
        return val.lower() in {"1", "true", "yes"}
    return os.getenv("ENABLE_LOCAL_ADMIN", "false").lower() in {"1","true","yes"}


# Mobile pairing. The console mints a six-digit, single-use code bound to its
# issuer; the phone exchanges it at /mobile/pair for its own device key: a new
# integration identity holding exactly _DEVICE_SCOPES at installation, listed
# and revocable under /access/keys like any other key.
from .access.mutation_types import StaleVersionError  # noqa: E402
from .access.types import ScopedPermission  # noqa: E402

_DEVICE_SCOPES = frozenset({"fax:send", "fax:read", "fax:document", "inbound:list", "inbound:read", "inbound:document"})
_PAIR_TTL = timedelta(minutes=5)
_PAIR_WINDOW_SECONDS = 60.0
_PAIR_ATTEMPTS_PER_IP = 5
# Across all addresses, so a six-digit code cannot be guessed from many clients at once.
_PAIR_ATTEMPTS_TOTAL = 30
_PAIR_ATTEMPTS: Dict[str, List[float]] = {}
_PAIR_REFUSED = "This pairing code did not work. Create a new code in the console and try again."


class PairOut(BaseModel):
    code: str
    expires_at: datetime


class MobilePairOut(BaseModel):
    base_urls: Dict[str, Optional[str]]
    token: str


@private_operation
def _mint_pairing_code(service, actor):
    # A code is only useful if its issuer may also issue the device key it turns into.
    with service.store.transaction() as connection:
        now = access_utcnow()
        decisions = (
            service.control.authorize_on(connection, actor, 'keys:manage', ResourceRef('installation'), now=now),
            service.control.can_grant_on(connection, actor, tuple(ScopedPermission(permission, ResourceRef('installation'))
                                                                 for permission in sorted(_DEVICE_SCOPES)), now=now))
    if not all(decision.allowed for decision in decisions):
        reset = any(decision.reason.value == 'reset_required' for decision in decisions)
        raise MutationDeniedError(MutationReason.RESET_REQUIRED if reset else MutationReason.FORBIDDEN)
    return service.capabilities.mint('pairing', actor, _PAIR_TTL, permission='tunnels:pair')


@app.post("/admin/tunnel/pair", response_model=PairOut, responses=_PERMISSION_RESPONSES)
async def admin_tunnel_pair(request: Request, identity=Depends(require_permission('tunnels:pair'))):
    """A six-digit code, valid once for five minutes, that pairs one phone as a device of this installation."""
    issued = await run_lifecycle_step(lambda: _mint_pairing_code(access_runtime(request), identity.actor))
    return PairOut(code=issued.secret, expires_at=issued.expires_at)


def _pair_attempt_allowed(client_ip: str) -> bool:
    now = time.monotonic()
    for address in list(_PAIR_ATTEMPTS):
        recent = [at for at in _PAIR_ATTEMPTS[address] if now - at < _PAIR_WINDOW_SECONDS]
        if recent:
            _PAIR_ATTEMPTS[address] = recent
        else:
            del _PAIR_ATTEMPTS[address]
    attempts = _PAIR_ATTEMPTS.get(client_ip, [])
    if len(attempts) >= _PAIR_ATTEMPTS_PER_IP or sum(map(len, _PAIR_ATTEMPTS.values())) >= _PAIR_ATTEMPTS_TOTAL:
        return False
    _PAIR_ATTEMPTS[client_ip] = attempts + [now]
    return True


def _device_name(value) -> str:
    text = "".join(c for c in value if c.isprintable()).strip() if isinstance(value, str) else ""
    return text[:60].strip() or "Mobile device"


@private_operation
def _issue_device_key(service, actor, device_name):
    values = IntegrationKeyValues(display_name=f"Device: {device_name}", owner=None, name=device_name,
                                  note="Paired from the mobile app.", expires_at=None, permissions=_DEVICE_SCOPES)
    for attempt in range(2):
        prepared = service.credential_codec.prepare_new_key()
        try:
            receipt = service.mutations.issue_integration_key(actor, values, prepared,
                expected_policy_version=_policy_version(service), now=access_utcnow())
        except StaleVersionError:
            # Another change landed between reading and issuing; nothing was written.
            if attempt:
                raise
            continue
        return receipt, prepared


def _mobile_base_urls() -> Dict[str, Optional[str]]:
    # "tunnel" stays in the paired app's contract; Faxbot runs no tunnel, so it is always empty.
    return {"local": settings.mobile_local_base or None, "tunnel": None,
            "public": settings.public_api_url or None}


async def _pair_payload(request: Request) -> dict:
    """The JSON object body, at most 4 KiB; anything else is an empty payload."""
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            return {}
    try:
        payload = json.loads(bytes(raw))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


@app.post("/mobile/pair", response_model=MobilePairOut,
          responses={403: {"model": PublicDetailErrorResponse, "description": "Pairing failed."}},
          openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": {
              "type": "object", "required": ["code"], "properties": {
                  "code": {"type": "string", "pattern": "^[0-9]{6}$"}, "device_name": {"type": "string"}}}}}}})
async def mobile_pair(request: Request):
    """Exchange a pairing code from the console for this device's own API key. Any failure is 403."""
    def refused(message=_PAIR_REFUSED, reason="invalid_code"):
        audit_event("mobile_pair_refused", reason=reason)
        return JSONResponse({"detail": message}, status_code=403, headers=PRIVATE_HEADERS)

    if not _pair_attempt_allowed(request.client.host if request.client else "unknown"):
        return refused("Too many pairing attempts. Wait a minute and try again.", "rate_limited")
    payload = await _pair_payload(request)
    code = payload.get("code").strip() if isinstance(payload.get("code"), str) else ""
    if re.fullmatch(r"[0-9]{6}", code) is None:
        return refused()
    try:
        service = access_runtime(request)
        record = await run_lifecycle_step(lambda: service.capabilities.consume("pairing", code))
    except AccessError:
        return refused()
    device_name = _device_name(payload.get("device_name"))
    try:
        receipt, prepared = await service.work.run(lambda: _issue_device_key(service, record.actor, device_name))
    except AccessError:
        return refused("Pairing could not finish. Create a new code in the console and try again.", "issue_failed")
    audit_event("mobile_paired", key_id=receipt.public_key_id, principal_id=receipt.principal_id,
                issued_by=record.principal_id)
    # The token is disclosed once, only after the device key committed.
    return JSONResponse({"base_urls": _mobile_base_urls(), "token": prepared._token_for_committed_adapter()},
                        headers=PRIVATE_HEADERS)


@app.get("/admin/inbound/callbacks", dependencies=[Depends(require_permission('providers:read'))],
         responses=_PERMISSION_RESPONSES)
def admin_inbound_callbacks(request: Request):
    base = settings.public_api_url.rstrip("/")
    backend = active_inbound()
    out: dict[str, Any] = {"backend": backend, "callbacks": []}
    if backend == "phaxio":
        out["callbacks"].append({
            "name": "Phaxio Inbound",
            "url": f"{base}/phaxio-inbound",
            "verify_signature": settings.phaxio_inbound_verify_signature,
            "notes": "Set this as the receive callback URL in Phaxio. Faxbot checks Phaxio's signature with the Callback Token.",
        })
    elif backend == "sinch":
        out["callbacks"].append({
            "name": "Sinch Fax Inbound",
            "url": f"{base}/sinch-inbound",
            "auth": {
                "basic": bool(settings.sinch_inbound_basic_user and settings.sinch_inbound_basic_pass),
                "hmac": bool(settings.sinch_inbound_hmac_secret),
            },
            "notes": "Set this as the incoming fax webhook in Sinch. Without basic auth, Faxbot confirms each fax with Sinch first.",
        })
    elif backend == "efax" and settings.efax_webhook_secret:
        out["callbacks"].append({
            "name": "eFax notification",
            "url": f"{base}/efax-inbound",
            "notes": "Optional. Give eFax this address with the notification secret; Faxbot then checks eFax at once.",
        })
    elif backend == "signalwire":
        out["callbacks"].append({
            "name": "SignalWire Fax Status",
            "url": f"{base}/signalwire-callback",
            "notes": "Configure StatusCallback on send; this endpoint will process updates.",
        })
    elif backend == "sip":
        # Faxbot's own Asterisk hands received faxes over by itself; there is no URL to configure.
        out["receiving"] = _sip_receiving_status(request)
    return out


def _sip_receiving_status(request):
    """One sentence about receiving over the SIP trunk, the same one the trunk screen shows."""
    from . import sip_http, sip_trunk
    values = request.scope["faxbot.configuration"].active.values
    if not values.inbound_enabled:
        return {"ready": False, "message": "Receiving faxes is turned off in Settings."}
    if not sip_trunk.configured(values):
        return {"ready": False, "message": sip_http.NO_TRUNK}
    try:
        from .routing.background import installation_engine
        engine, _ = installation_engine(request.app)
        last = sip_http._last_call(sip_calls.SipCallRecords(engine))
    except Exception:
        last = None
    handover = sip_http._handover(values, sip_trunk.engine_managed(values), last)
    if handover is None:
        return {"ready": False, "message": "Receiving faxes is turned off in Settings."}
    return {"ready": handover["ready"], "message": handover["text"]}


class SimulateInboundIn(BaseModel):
    backend: Optional[str] = None  # default to current backend
    fr: Optional[str] = None
    to: Optional[str] = None
    pages: Optional[int] = 1
    status: Optional[str] = "received"


@app.post("/admin/inbound/simulate", responses=_PERMISSION_RESPONSES)
async def admin_inbound_simulate(payload: SimulateInboundIn, request: Request,
                                 identity=Depends(require_permission('providers:write'))):
    """Add a test fax with a real one-page PDF; it is marked as a test everywhere."""
    if not settings.inbound_enabled:
        raise HTTPException(400, detail="Inbound not enabled")
    from .inbound.acquisition import account_identity, store_document
    from .conversion import txt_to_pdf
    service = getattr(request.app.state, "inbound_acquisition", None)
    if service is None:
        raise HTTPException(503, detail="Receiving faxes is not ready yet; try again shortly.")
    backend = (payload.backend or settings.fax_backend).lower()[:20]

    def create():
        from .people_time import date_and_time
        created_at = date_and_time(datetime.now(timezone.utc), settings.time_zone)
        with tempfile.TemporaryDirectory() as folder:
            text = os.path.join(folder, "test-fax.txt")
            document = os.path.join(folder, "test-fax.pdf")
            with open(text, "w", encoding="utf-8") as handle:
                handle.write(f"Test fax created in Faxbot on {created_at}.\n")
            txt_to_pdf(text, document)
            with open(document, "rb") as handle:
                data = handle.read()
        begun = service.store.begin(
            source="test", account=account_identity("test", identity.actor.principal_id),
            operation_id=uuid.uuid4().hex, backend=backend, inbound_backend=active_inbound(),
            to_number=payload.to, from_number=payload.fr, reported_pages=1,
            report={"created_by": "simulate"}, schedule=False, country=settings.fax_default_country)
        artifact = store_document(data, begun.inbound_fax_id, provider="Faxbot")
        service.store.complete(begun.import_id, artifact_path=artifact.path, digest=artifact.digest,
                               size=artifact.size, pages=artifact.pages, media_type=artifact.media_type)
        return begun.inbound_fax_id
    inbound_id = await run_lifecycle_step(private_operation(create))
    return {"id": inbound_id, "status": "ok"}


@app.get("/admin/fax-jobs")
async def list_admin_jobs(
    request: Request,
    status: Optional[str] = None,
    backend: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    identity=Depends(require_identity),
):
    page = await run_lifecycle_step(private_operation(lambda: access_runtime(request).queries.page(
        identity.actor, status=status, backend=backend, limit=limit, offset=offset)))
    together = await run_lifecycle_step(lambda: batching_summaries(
        _configuration_manager().store.engine, [row['id'] for row in page['jobs']]))
    return {'total': page['total'], 'jobs': [{**_admin_fax_view(row), 'together': together.get(row['id'])}
                                             for row in page['jobs']]}


@app.get("/admin/fax-jobs/{job_id}")
async def get_admin_job(job_id: str, request: Request, identity=Depends(require_identity)):
    row = await run_lifecycle_step(private_operation(lambda: access_runtime(request).queries.job(identity.actor, job_id)))
    together = await run_lifecycle_step(lambda: batching_summaries(_configuration_manager().store.engine, [job_id]))
    # Over the SIP trunk: which fax engine carried it, and SSL Fax's line or the built-in engine's reason.
    from .hylafax_records import records_for, safely
    fax_engine = await run_lifecycle_step(
        lambda: safely(records_for(_configuration_manager().store.engine).sent_detail, job_id))
    return {**_admin_fax_view(row), 'provider_sid': row['provider_sid'], 'file_name': row['file_name'],
            'together': together.get(job_id), 'fax_engine': fax_engine,
            # The sender asked for a real call through the carrier, even to one of this installation's own numbers.
            'send_by_call': bool(row.get('send_by_call')), 'urgent': bool(row.get('urgent')),
            # Why it has not started yet, or why its number stays reserved (capacity.py); None otherwise.
            'waiting_reason': await run_lifecycle_step(
                lambda: _waiting_reason(_configuration_manager().store, job_id, datetime.utcnow()))}


def _admin_fax_view(row):
    fields = ('id', 'status', 'backend', 'pages', 'created_at', 'updated_at',
              'delivery_state', 'dispatch_mode', 'delivery_version', 'reconciliation_reason')
    return {**{name: row[name] for name in fields},
            'to_number': mask_phone(row['to_number']), 'error': sanitize_error(row['error'])}


class ProviderIdentityConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(strict=True, gt=0)
    provider_sid: str = Field(strict=True, pattern=r'^[A-Za-z0-9_-]{1,100}$')
    confirm_original_account: StrictBool


@app.get('/admin/fax-jobs/{job_id}/delivery')
async def admin_delivery_history(job_id: str, request: Request, identity=Depends(require_identity)):
    """Bounded evidence from the accepted account, excluding captured secrets."""
    return await run_lifecycle_step(private_operation(lambda: access_runtime(request).queries.history(identity.actor, job_id)))


@app.post('/admin/fax-jobs/{job_id}/reconcile')
async def admin_bind_provider_identity(job_id: str, confirmation: ProviderIdentityConfirmation,
                                       request: Request, identity=Depends(require_identity)):
    """Attach an operator-confirmed receipt; this never authorizes transmission."""
    if confirmation.confirm_original_account is not True:
        raise HTTPException(400, detail='Confirm that this fax ID matches the fax in its original provider account.')

    def bind():
        try:
            return access_runtime(request).queries.reconcile(identity.actor, job_id,
                expected_version=confirmation.expected_version,
                provider_sid=confirmation.provider_sid)
        except DeliveryConflict as exc:
            # Store conflicts are fixed messages and contain no provider payloads.
            raise HTTPException(409, detail=str(exc)) from None
        except ValueError:
            raise HTTPException(400, detail='Invalid provider identity reconciliation input.') from None

    return await run_lifecycle_step(bind)


@app.get("/admin/fax-jobs/{job_id}/pdf")
def admin_get_job_pdf(job_id: str, request: Request, identity=Depends(require_identity)):
    """Download the retained PDF with independent current document permission."""
    private_operation(access_runtime(request).queries.document)(identity.actor, job_id)
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


@app.post("/admin/fax-jobs/{job_id}/refresh")
async def admin_refresh_job(job_id: str, request: Request, identity=Depends(require_identity)):
    """Read status from the accepted account without authorizing another send."""
    try:
        target = await run_lifecycle_step(lambda: access_runtime(request).queries.poll_target(identity.actor, job_id))
        await OutboundPoller(_deliveries()).refresh_target(job_id, target)
    except AccessError:
        raise
    except UnsupportedProviderExecutionError:
        raise HTTPException(400, detail="This provider reports status through callbacks; refresh is unsupported.") from None
    except (DeliveryConflict, UnboundProviderProfile):
        raise HTTPException(409, detail="This fax requires reconciliation with its original provider account before refresh.") from None
    except Exception:
        raise HTTPException(502, detail="Provider status is temporarily unavailable. This fax has not been resubmitted.") from None
    return await run_lifecycle_step(private_operation(lambda: _accepted_job_response(access_runtime(request), identity.actor, job_id)))


@app.get("/admin/settings/export", responses={**_CONFIGURATION_READ_RESPONSES, **_CONFIGURATION_VALIDATION_RESPONSES})
def export_settings_env(request: Request, identity=Depends(require_identity)):
    """Display the complete desired configuration with opaque secret placeholders."""
    values = access_runtime(request).configuration_access.settings(identity.actor).desired.values
    content = format_environment(values.to_environment(redact_secrets=True))
    return {"env": content, "env_content": content}


class PersistSettingsIn(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    content: str | None = None
    path: str | None = None


@app.post("/admin/settings/persist", deprecated=True,
          dependencies=[Depends(require_permission('owner:recover', audit=True, complete_owner=True))],
          responses={**_PERMISSION_RESPONSES, **_CONFIGURATION_VALIDATION_RESPONSES})
def persist_settings(payload: PersistSettingsIn):
    """Deprecated: removed in the next release; back up with `faxbot system backup` instead.

    Atomically export desired settings to the installation's private recovery file.

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

@app.post("/fax", response_model=FaxJobOut, status_code=202)
async def send_fax(request: Request, to: str = Form(...), file: UploadFile = File(...),
                   queue_only: bool = Form(False),
                   send_now: bool = Form(False, description='Send at once even when this number sends faxes '
                                                            'together; faxes waiting for it go in the same call.'),
                   send_by_call: bool = Form(False, description="Place a real call through your fax provider or "
                                             "carrier even when the number is one of this installation's own "
                                             "numbers, instead of delivering it inside Faxbot. Test faxes use this."),
                   urgent: bool = Form(False, description='Send before other faxes waiting for the same number or '
                                       'line, and without waiting to go together with other faxes.'),
                   idempotency_key: Optional[str] = Header(default=None, alias='Idempotency-Key',
                       description='Optional key for replaying the same fax request; 1 to 128 printable ASCII characters without spaces.'),
                   identity=Depends(require_identity)):
    manager = _configuration_manager()
    access = access_runtime(request)
    await run_lifecycle_step(private_operation(lambda: access.outbound.check_send(identity.actor)))
    _enforce_rate_limit({'key_id': identity.actor.replay_scope}, '/fax')
    revision = request.scope['faxbot.configuration'].active

    def resolve(country):
        try:
            return normalize_number(to, country=country), None
        except InvalidNumber as error:
            return None, error

    # One canonical destination, resolved before fingerprinting, provider
    # selection and acceptance; the job stores it so settings cannot redirect it.
    destination, destination_error = resolve(revision.values.fax_default_country)
    request_identity = None
    keys = request.headers.getlist('idempotency-key')
    if keys:
        if len(keys) != 1:
            raise HTTPException(400, detail='Supply one Idempotency-Key header.')
        try:
            scope = identity.actor.replay_scope
            validated = RequestIdentity.from_key(idempotency_key, principal_scope=scope, fingerprint='0' * 64)
            accepted = await run_lifecycle_step(lambda: access.outbound.replay_values(identity.actor, validated))
            max_bytes = (accepted.max_file_size_mb if accepted is not None else settings.max_file_size_mb) * 1024 * 1024
            document_sha256 = await digest_upload(file, max_bytes=max_bytes)
            # A replay is the same request when it resolves to the same number
            # under the country its original was accepted with.
            original = destination if accepted is None else resolve(accepted.fax_default_country)[0]
            fingerprint, legacy = request_fingerprints(entered=to, destination=original,
                queue_only=queue_only, document_sha256=document_sha256, by_call=send_by_call, urgent=urgent)
            replay_identity = RequestIdentity(scope, validated.idempotency_digest, fingerprint, legacy)
            existing = await run_lifecycle_step(lambda: access.outbound.find_replay(identity.actor, replay_identity))
            if destination is not None:
                fingerprint, legacy = request_fingerprints(entered=to, destination=destination,
                    queue_only=queue_only, document_sha256=document_sha256, by_call=send_by_call, urgent=urgent)
                request_identity = RequestIdentity(scope, validated.idempotency_digest, fingerprint, legacy)
        except UploadPreparationError as error:
            raise HTTPException(error.status_code, detail=str(error)) from None
        except IdempotencyConflict as error:
            raise HTTPException(409, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        if existing is not None:
            return await run_lifecycle_step(private_operation(lambda: _accepted_job_response(access, identity.actor, existing)))
    if destination is None:
        raise HTTPException(400, detail=str(destination_error))
    if queue_only and not settings.fax_disabled:
        raise HTTPException(409, detail="Queue-only request refused because outbound sending is now enabled. Refresh Send before submitting again.")
    profile_id = revision.profile_id('outbound')
    if profile_id is None:
        if not revision.values.effective_outbound:
            raise HTTPException(409, detail=NO_PROVIDER)
        raise HTTPException(409, detail="Outbound fax delivery is disabled in this configuration.")
    profile = manager.store.read_profile(profile_id)
    ob = profile.configuration.provider_id
    use_manifest = profile.configuration.manifest is not None
    if use_manifest or ob not in {'sip', 'freeswitch'}:
        try:
            service_from_profile(profile)
        except ProviderExecutionError:
            raise HTTPException(400, detail="Selected provider has no supported outbound adapter.") from None
    elif ob == 'sip' and not revision.values.fax_disabled and not ami_client._connected.is_set():
        # A fax accepted now would fail before it is sent; refuse it with the reason instead.
        # Held test faxes are never sent, so they are still accepted.
        raise HTTPException(503, detail=ami_client.engine_message())
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
    hold = None
    try:
        hold = await run_lifecycle_step(lambda: batching_acceptance.hold_plan(
            manager.store.engine, revision, profile, destination=destination, pages=prepared.pages,
            actor=identity.actor, send_now=send_now or urgent))
    except Exception:
        # Sending together is optional: without a usable answer the fax goes straight away.
        logging.getLogger(__name__).warning('Sending together is unavailable; the fax goes straight away.')

    # One transaction accepts the row and its immutable account/profile binding.
    try:
        accepted_at = datetime.utcnow()
        result = FaxJobOut(id=job_id, to=destination, status='queued', pages=prepared.pages,
                          backend=ob, created_at=accepted_at, updated_at=accepted_at,
                          delivery_state='held' if revision.values.fax_disabled else 'ready',
                          dispatch_mode='held' if revision.values.fax_disabled else 'normal',
                          delivery_version=1)
        await run_lifecycle_step(lambda: access.outbound.accept(identity.actor, revision, {
            'id': job_id, 'to_number': destination, 'file_name': prepared.original_name,
            'tiff_path': tiff_path, 'status': 'queued', 'pages': prepared.pages,
            'created_at': accepted_at, 'updated_at': accepted_at,
            # A real call even to one of this installation's own numbers (never delivered inside Faxbot).
            **({'send_by_call': 1} if send_by_call else {}),
            # Goes before other faxes waiting for the same room (capacity.py).
            **({'urgent': 1} if urgent else {}),
        }, request_identity=request_identity, also=None if hold is None else batching_acceptance.recorder(
            manager.store.engine, job_id, hold, identity.actor)))
    except IdempotentReplay as replay:
        prepared.cleanup()
        return await run_lifecycle_step(private_operation(lambda: _accepted_job_response(access, identity.actor, replay.job_id)))
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


def _accepted_job_response(access, actor, job_id):
    row = access.queries.job(actor, job_id)
    fields = ('id', 'status', 'pages', 'backend', 'provider_sid', 'created_at', 'updated_at',
              'delivery_state', 'dispatch_mode', 'delivery_version', 'reconciliation_reason')
    return FaxJobOut(**{name: row[name] for name in fields}, to=row['to_number'], error=sanitize_error(row['error']))


@app.get("/fax/{job_id}", response_model=FaxJobOut)
def get_fax(job_id: str, request: Request, identity=Depends(require_identity)):
    _enforce_rate_limit({'key_id': identity.actor.replay_scope}, '/fax/{id}')
    return private_operation(lambda: _accepted_job_response(access_runtime(request), identity.actor, job_id))()


# Admin API key management
def _key_display_name(name, owner, key_id):
    parts = [value.strip() for value in (name, owner) if value and value.strip()]
    display = f"{parts[0]} ({parts[1]})" if len(parts) == 2 else parts[0] if parts else f"API key {key_id}"
    return display[:200]


def _naive_utc(value):
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _policy_version(service):
    with service.store.transaction() as connection:
        return service.store.require_lock_on(connection)


def _may_manage_keys(service, connection, actor):
    return service.control.authorize_on(connection, actor, 'keys:manage',
        ResourceRef('installation'), now=access_utcnow()).allowed


@private_operation
def _list_keys(service, actor):
    """The /access/keys projection in the legacy shape; a revoked key keeps its revoked_at."""
    items, cursor = [], None
    while True:
        page = service.reads.keys(actor, cursor=cursor, limit=200)
        items.extend(page['items'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    owners: Dict[str, Optional[str]] = {}
    if items:
        keys = service.store.tables['api_keys']
        with service.store.transaction() as connection:
            owners = dict(connection.execute(sa.select(keys.c.key_id, keys.c.owner)
                .where(keys.c.key_id.in_([item['id'] for item in items]))).all())
    return [APIKeyMeta(key_id=item['id'], name=item['name'], owner=owners.get(item['id']),
                       scopes=[grant['permission'] for grant in item['ceiling'] if grant['resource_id'] == 'installation'],
                       created_at=item['created_at'], last_used_at=item['last_used_at'], expires_at=item['expires_at'],
                       revoked_at=item['revoked_at'], note=item['note']) for item in items]


@private_operation
def _key_target(service, actor, key_id):
    """Resolve a public key id; existence is disclosed only to key managers."""
    with service.store.transaction() as connection:
        version = service.store.require_lock_on(connection)
        allowed = _may_manage_keys(service, connection, actor)
        keys, bindings = service.store.tables['api_keys'], service.store.tables['access_key_bindings']
        row = connection.execute(sa.select(bindings.c.id, bindings.c.version)
            .select_from(bindings.join(keys, keys.c.id == bindings.c.id)).where(keys.c.key_id == key_id)).first()
    if row is None:
        return allowed, None, version
    return allowed, VersionedEntity(row.id, row.version), version


def _require_key_target(service, actor, key_id):
    allowed, target, version = _key_target(service, actor, key_id)
    if target is None:
        if allowed:
            raise HTTPException(404, detail="Key not found")
        raise MutationDeniedError(MutationReason.FORBIDDEN)
    return target, version


@app.post("/admin/api-keys", response_model=CreateAPIKeyOut)
def admin_create_api_key(payload: CreateAPIKeyIn, request: Request, identity=Depends(require_identity)):
    """Issue a key for a new integration identity holding exactly the requested scopes."""
    scopes = list(dict.fromkeys(payload.scopes or []))
    unknown = [scope for scope in scopes if scope not in KEY_SCOPES]
    if unknown:
        raise HTTPException(400, detail=f"Unknown scope: {', '.join(unknown)}. "
                                        f"Allowed scopes: {', '.join(sorted(KEY_SCOPES))}.")
    service = access_runtime(request)

    @private_operation
    def issue():
        prepared = service.credential_codec.prepare_new_key()
        values = IntegrationKeyValues(
            display_name=_key_display_name(payload.name, payload.owner, prepared.public_key_id),
            owner=payload.owner, name=payload.name, note=payload.note,
            expires_at=_naive_utc(payload.expires_at), permissions=frozenset(scopes))
        receipt = service.mutations.issue_integration_key(identity.actor, values, prepared,
            expected_policy_version=_policy_version(service), now=access_utcnow())
        return receipt, prepared

    receipt, prepared = issue()
    # Disclosed once, only after the issuing transaction committed.
    return CreateAPIKeyOut(key_id=receipt.public_key_id, token=prepared._token_for_committed_adapter(),
                           name=payload.name, owner=payload.owner,
                           scopes=[grant.permission for grant in receipt.ceiling], expires_at=payload.expires_at)


@app.get("/admin/api-keys", response_model=List[APIKeyMeta])
def admin_list_api_keys(request: Request, identity=Depends(require_identity)):
    return _list_keys(access_runtime(request), identity.actor)


@app.delete("/admin/api-keys/{key_id}")
def admin_revoke_api_key(key_id: str, request: Request, identity=Depends(require_identity)):
    service = access_runtime(request)
    target, version = _require_key_target(service, identity.actor, key_id)
    private_operation(service.mutations.revoke_key)(identity.actor, target,
        expected_policy_version=version, now=access_utcnow())
    return {"status": "ok"}


class RotateAPIKeyOut(BaseModel):
    key_id: str
    token: str


@app.post("/admin/api-keys/{key_id}/rotate", response_model=RotateAPIKeyOut)
def admin_rotate_api_key(key_id: str, request: Request, identity=Depends(require_identity)):
    """Replace the secret; the key id, identity and scopes stay the same."""
    service = access_runtime(request)
    target, version = _require_key_target(service, identity.actor, key_id)

    @private_operation
    def rotate():
        prepared = service.credential_codec.prepare_key_rotation(key_id)
        receipt = service.mutations.rotate_key(identity.actor, target, prepared,
            expected_policy_version=version, now=access_utcnow())
        return receipt, prepared

    receipt, prepared = rotate()
    return RotateAPIKeyOut(key_id=receipt.public_key_id, token=prepared._token_for_committed_adapter())


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
    # A shared call's image copies its faxes' pages; it is normally removed when the call ends.
    try:
        for path in Path(settings.fax_data_dir).glob('batch-*.tiff'):
            if not path.is_symlink() and datetime.utcfromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink(missing_ok=True)
    except OSError:
        audit_event('outbound_retention_requires_attention')


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


# ===== Inbound receiving: notifications and fetching live in inbound/http.py =====


class InboundFaxOut(BaseModel):
    id: str
    fr: Optional[str] = None
    to: Optional[str] = None
    # waiting (the document has not been fetched yet), received, or failed.
    status: str
    backend: str
    pages: Optional[int] = None
    size_bytes: Optional[int] = None
    created_at: Optional[datetime] = None
    # When Faxbot recorded the fax; the provider's own time is source_received_at.
    received_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    mailbox: Optional[str] = None
    status_text: Optional[str] = None
    source_received_at: Optional[datetime] = None
    provider_fax_id: Optional[str] = None
    sha256: Optional[str] = None
    is_test: bool = False
    retry_at: Optional[datetime] = None
    problem: Optional[str] = None
    can_fetch_again: bool = False
    # Brought in later from an image the fax engine stored but could not hand over;
    # source_received_at is then the image's modification time.
    recovered: bool = False
    # A sentence about the provider's own copy, such as an eFax deletion Faxbot is still retrying.
    provider_note: Optional[str] = None


def _inbound_pdf_response(inbound_id: str, pdf_path: Optional[str], method: str, status: Optional[str] = "received"):
    pdf_path = str(pdf_path or "")
    if status != "received":
        raise HTTPException(404, detail="The document has not been received yet.")
    if not pdf_path:
        raise HTTPException(404, detail="PDF file not found")
    no_store = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
    if pdf_path.startswith("s3://"):
        stream, name = get_storage().get_pdf_stream(pdf_path)
        audit_event("inbound_pdf_served", job_id=inbound_id, method=method)
        return StreamingResponse(stream, media_type="application/pdf", headers={
            "Content-Disposition": f"attachment; filename={name}", **no_store})
    if not os.path.exists(pdf_path):
        raise HTTPException(404, detail="PDF file not found")
    audit_event("inbound_pdf_served", job_id=inbound_id, method=method)
    return FileResponse(pdf_path, media_type="application/pdf", filename=f"inbound_{inbound_id}.pdf", headers=no_store)


@app.get("/inbound", response_model=List[InboundFaxOut])
async def list_inbound(
    request: Request,
    to_number: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    mailbox: Optional[str] = Query(default=None),
    identity=Depends(require_identity),
):
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
    rows = await run_lifecycle_step(private_operation(lambda: access_runtime(request).inbound_queries.page(
        identity.actor, to_number=to_number, status=status, mailbox=mailbox,
        country=settings.fax_default_country)))
    _enforce_rate_limit({'key_id': identity.actor.replay_scope}, "/inbound", settings.inbound_list_rpm)
    return [InboundFaxOut(**row) for row in rows]


@app.get("/inbound/{inbound_id}", response_model=InboundFaxOut)
async def get_inbound(inbound_id: str, request: Request, identity=Depends(require_identity)):
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
    row = await run_lifecycle_step(private_operation(lambda: access_runtime(request).inbound_queries.item(
        identity.actor, inbound_id)))
    _enforce_rate_limit({'key_id': identity.actor.replay_scope}, "/inbound/{id}", settings.inbound_get_rpm)
    return InboundFaxOut(**row)


@app.get("/inbound/{inbound_id}/pdf")
async def get_inbound_pdf(inbound_id: str, request: Request, token: Optional[str] = Query(default=None)):
    """Document bytes need inbound:document, or the fax's unexpired download token."""
    if not settings.inbound_enabled:
        raise HTTPException(404, detail="Inbound not enabled")
    service = access_runtime(request)
    if token:
        try:
            shared = await run_lifecycle_step(private_operation(
                lambda: service.inbound_queries.shared_document(inbound_id, token)))
        except FaxAccessError:
            shared = None
        if shared is not None:
            return _inbound_pdf_response(inbound_id, shared["pdf_path"], "token",
                                         _document_status(shared.get("status"), shared.get("sha256")))
    identity = await require_identity(request)
    document = await run_lifecycle_step(private_operation(lambda: service.inbound_queries.document(
        identity.actor, inbound_id)))
    if settings.inbound_get_rpm:
        _enforce_rate_limit({'key_id': identity.actor.replay_scope}, "/inbound/{id}/pdf", settings.inbound_get_rpm)
    return _inbound_pdf_response(inbound_id, document["pdf_path"], "api_key" if identity.source == "key" else "session",
                                 _document_status(document.get("status"), document.get("sha256")))


def _document_status(status: Optional[str], sha256: Optional[str] = None) -> str:
    """Rows from before acquisition records kept provider statuses; waiting, failed and old stand-ins have no document."""
    from .inbound.acquisition import PLACEHOLDER_DIGESTS
    return "missing" if status in ("waiting", "failed") or sha256 in PLACEHOLDER_DIGESTS else "received"


# ===== Global error logging =====
@app.exception_handler(HTTPException)
async def _handle_http_exc(request: Request, exc: HTTPException):
    try:
        audit_event("api_error", path=request.url.path, status=exc.status_code, detail=str(exc.detail))
    except Exception:
        pass
    headers = {**(exc.headers or {})}
    if private_response_path(request.url.path):
        headers.update(PRIVATE_HEADERS)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)


@app.exception_handler(Exception)
async def _handle_any_exc(request: Request, exc: Exception):
    if private_response_path(request.url.path):
        # ServerErrorMiddleware is outside the privacy middleware. Its fallback
        # response must carry the same fixed unavailable body and private headers.
        return await access_error_response(request, AccessUnavailableError())
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
        "name": "Phaxio",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status", "webhook"],
        "enabled": (current == "phaxio"),
        "configurable": True,
    })
    items.append({
        "id": "sinch",
        "name": "Sinch",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status"],
        "enabled": (current == "sinch"),
        "configurable": True,
    })
    items.append({
        "id": "signalwire",
        "name": "SignalWire",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status", "webhook"],
        "enabled": (current == "signalwire"),
        "configurable": True,
    })
    items.append({
        "id": "documo",
        "name": "Documo",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status"],
        "enabled": (current == "documo"),
        "configurable": True,
    })
    items.append({
        "id": "humblefax",
        "name": "HumbleFax",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status"],
        "enabled": (current == "humblefax"),
        "configurable": True,
    })
    items.append({
        "id": "efax",
        "name": "eFax",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status", "receive"],
        "enabled": (current == "efax"),
        "configurable": True,
    })
    items.append({
        "id": "sip",
        "name": "SIP trunk (Asterisk)",
        "version": "1.0.0",
        "categories": ["outbound"],
        "capabilities": ["send", "get_status"],
        "enabled": (current == "sip"),
        "configurable": True,
    })
    items.append({
        "id": "freeswitch",
        "name": "SIP trunk (FreeSWITCH)",
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
        "enabled": (snapshot.desired.values.storage_backend == "local"),
        "configurable": False,
    })
    items.append({
        "id": "s3",
        "name": "S3 / S3-compatible Storage",
        "version": "1.0.0",
        "categories": ["storage"],
        "capabilities": ["store", "retrieve", "delete"],
        "enabled": (snapshot.desired.values.storage_backend == "s3"),
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


@app.get("/plugins", responses=_PROVIDER_READ_RESPONSES, deprecated=True,
         description="Deprecated: removed in the next release with FEATURE_V3_PLUGINS; read providers from /admin/settings.")
def list_plugins(request: Request, identity=Depends(require_identity)):
    snapshot = access_runtime(request).configuration_access.providers(identity.actor)
    if not snapshot.active.values.feature_v3_plugins:
        return _plugins_disabled_response()
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
            '_meta': configuration_write_receipt(snapshot, snapshot)['_meta']}


@app.get("/plugins/{plugin_id}/config", responses={**_PROVIDER_READ_RESPONSES, **_CONFIGURATION_VALIDATION_RESPONSES},
         deprecated=True, description="Deprecated: removed in the next release; read provider settings from /admin/settings.")
def get_plugin_config(plugin_id: str, request: Request, role: str | None = None, identity=Depends(require_identity)):
    snapshot = access_runtime(request).configuration_access.providers(identity.actor)
    if not snapshot.active.values.feature_v3_plugins:
        return _plugins_disabled_response()
    return _plugin_view(snapshot, plugin_id.lower(), role)


class UpdatePluginConfigIn(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, json_schema_extra={
        'description': 'Submit only changed provider fields and the loaded expected_revision_id. An empty object submits no field edits.',
        'examples': [{}],
    })
    enabled: Optional[bool] = None
    settings: Optional[dict[str, Any]] = None
    role: str | None = None
    expected_revision_id: str | None = None


@app.put("/plugins/{plugin_id}/config", response_model=ConfigurationWriteResponse, responses=_CONFIGURATION_WRITE_RESPONSES,
         deprecated=True, description="Deprecated: removed in the next release; change provider settings with PUT /admin/settings.")
def update_plugin_config(plugin_id: str, payload: UpdatePluginConfigIn, request: Request,
                         identity=Depends(require_identity)):
    expected = request.scope['faxbot.configuration']
    access = access_runtime(request)
    access.configuration_access.prepare_provider_write(identity.actor, expected, payload.expected_revision_id)
    if isinstance(payload.settings, dict):
        from .config_plugin_fields import PLUGIN_FIELDS
        mapping = PLUGIN_FIELDS.get(plugin_id.lower(), {})
        if payload.settings:
            changes = {mapping[key]: value for key, value in payload.settings.items() if key in mapping}
        else:
            # Empty settings reset every provider field to its default (see _patch_plugin_values).
            defaults = ConfigurationValues.from_environment({})
            changes = {name: getattr(defaults, name) for name in mapping.values()}
        _refuse_environment_managed(expected, changes)
    snapshot = _configuration_manager().patch_plugin_authorized(expected, plugin_id.lower(),
        settings=payload.settings, enabled=payload.enabled, role=payload.role,
        principal=identity.actor, control=access.control)
    return configuration_write_receipt(expected, snapshot)


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


@app.post("/_internal/freeswitch/outbound_result", deprecated=True,
          description="Deprecated: FreeSWITCH is removed in the next release.")
def freeswitch_outbound_result(payload: FSOutboundResultIn, x_internal_secret: Optional[str] = Header(default=None)):
    status = str(payload.fax_status or '').lower()
    status = {'true': 'success', 'false': 'failed', 'ok': 'success', 'fail': 'failed'}.get(status, status)
    try:
        applied = _observe_native(payload.job_id, payload.attempt_id, status, 'freeswitch',
            event_key='fs-result:' + status, secret=x_internal_secret)
    except (DeliveryConflict, ValueError):
        raise HTTPException(409, detail='Native result does not match a verified delivery attempt.') from None
    return {'ok': True, 'applied': applied}


# Host terminal for the console. POST /admin/terminal/ticket mints a single-use
# ticket for the caller (host:terminal and the exec gate). The WebSocket takes no
# credential in its URL: its first message must be {"type": "auth", "ticket": ...}
# within _TERMINAL_AUTH_SECONDS. A socket that arrives with a browser session
# cookie, or whose ticket a browser session minted, also needs an exact allowed
# Origin. terminal.py keeps rechecking host:terminal for the minting credential.
from .access.transport import SESSION_COOKIES, TransportError, credential_source  # noqa: E402

_TERMINAL_TICKET_TTL = timedelta(seconds=60)
_TERMINAL_AUTH_SECONDS = 5.0
_TERMINAL_UNAVAILABLE = "The terminal is not available on this server."


class TerminalTicketOut(BaseModel):
    ticket: str
    expires_at: datetime


def _terminal_available() -> bool:
    from . import terminal as terminal_module
    return _admin_exec_enabled() and not terminal_module.check_terminal_requirements()


@app.post("/admin/terminal/ticket", response_model=TerminalTicketOut, responses=_PERMISSION_RESPONSES)
async def admin_terminal_ticket(request: Request, identity=Depends(require_permission('host:terminal', audit=True))):
    """A ticket, valid once for 60 seconds, that opens one terminal WebSocket as the caller."""
    if not _terminal_available():
        raise HTTPException(404, detail=_TERMINAL_UNAVAILABLE)
    service = access_runtime(request)
    issued = await run_lifecycle_step(private_operation(lambda: service.capabilities.mint(
        'terminal', identity.actor, _TERMINAL_TICKET_TTL, permission='host:terminal')))
    return TerminalTicketOut(ticket=issued.secret, expires_at=issued.expires_at)


def _browser_socket(scope) -> bool:
    try:
        return any(credential_source(scope, name)[0] == 'session' for name in SESSION_COOKIES)
    except TransportError:
        # Duplicate or malformed credentials: hold the socket to the browser rules, which refuse it.
        return True


async def _close_terminal(websocket: WebSocket, code: int):
    try:
        await websocket.close(code=code)
    except Exception:
        pass


async def _terminal_ticket(websocket: WebSocket) -> Optional[str]:
    """The ticket from the first message, or None when it is late or malformed."""
    try:
        message = await asyncio.wait_for(websocket.receive_text(), _TERMINAL_AUTH_SECONDS)
        data = json.loads(message)
    except Exception:
        return None
    ticket = data.get('ticket') if isinstance(data, dict) and data.get('type') == 'auth' else None
    return ticket if isinstance(ticket, str) and 0 < len(ticket) <= 256 else None


@app.websocket("/admin/terminal")
async def admin_terminal_websocket(websocket: WebSocket):
    """Host terminal WebSocket; authenticated only by a ticket in the first message."""
    from . import terminal as terminal_module
    service = getattr(websocket.app.state, 'access_runtime', None)
    transport = getattr(websocket.app.state, 'credential_transport', None)
    # Credentials never travel in the URL, so any query string is refused outright.
    if websocket.url.query or service is None or transport is None or not _terminal_available():
        await _close_terminal(websocket, 1008)
        return
    await websocket.accept()
    ticket = await _terminal_ticket(websocket)
    if ticket is None:
        await _close_terminal(websocket, 1008)
        return
    try:
        record = await run_lifecycle_step(lambda: service.capabilities.consume('terminal', ticket))
    except Exception:
        await _close_terminal(websocket, 1008)
        return
    if record.session_id is not None or _browser_socket(websocket.scope):
        try:
            transport.validate(websocket.scope, public_url=settings.public_api_url, require_origin=True)
        except AccessError:
            await _close_terminal(websocket, 1008)
            return

    def still_authorized() -> bool:
        try:
            return _terminal_available() and service.control.authorize(
                record.actor, 'host:terminal', ResourceRef('installation'), now=access_utcnow()).allowed
        except Exception:
            return False

    # Every session start goes into the audit log, after the permission is checked once more;
    # a refusal is recorded too, and no shell starts.
    try:
        await run_lifecycle_step(lambda: authorize_operation(
            service, record.actor, 'host:terminal', audit={'request': 'WEBSOCKET /admin/terminal', 'session': 'started'}))
    except Exception:
        await _close_terminal(websocket, 1008)
        return
    audit_event("terminal_opened", principal_id=record.principal_id)
    try:
        await terminal_module.handle_terminal_websocket(websocket,
            authorized=lambda: run_lifecycle_step(still_authorized))
    finally:
        audit_event("terminal_closed", principal_id=record.principal_id)
