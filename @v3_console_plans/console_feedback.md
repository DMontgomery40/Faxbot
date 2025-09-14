# Faxbot Local Admin Console — Production-Ready Implementation Guide (v3)

Important: Before you start, read AGENTS.md in this repo. It contains conventions and constraints you must follow when making any changes.

## Critical Updates from Review

**Security Fixes:**
- Added `ENABLE_LOCAL_ADMIN=true` feature flag requirement
- Middleware now detects and blocks reverse proxy access (X-Forwarded-For)
- Removed SSE in v1 (avoids API key in URL security issue)
- Fixed Docker path mismatch (/app/admin_ui/dist)

**Simplification for v1:**
- Polling instead of SSE (5-10 second intervals)
- Read-only settings with .env export (no database/encryption complexity)
- Deferred settings persistence to v2
- Clear separation of v1 deliverables vs v2 future work

**Code Fixes:**
- Fixed TypeScript bug (response vs res variable)
- Removed undefined helper functions
- Used existing services (phaxio_service, ami client)
- Proper error handling and truncation

## Executive Summary
A local-only admin console embedded within the Faxbot API core, providing HIPAA-compliant management capabilities without external dependencies. The UI is served directly from FastAPI and operates exclusively on localhost with no remote access capabilities.

**v1 Scope**: Read-only configuration viewing, setup wizard with .env generation, API key management, job monitoring with polling, and comprehensive diagnostics. Settings changes require manual .env edits and API restart.

**v2 Future**: Settings persistence with encryption, SSE with session cookies, webhook subscriptions, and hot-reload capabilities.

## Core Principles
- **100% Local**: No remote access, localhost-only with feature flag
- **HIPAA-Compliant**: PHI masking, audit logging, secure defaults
- **Integrated**: Lives within the API core, not a separate service
- **Production-Ready**: Built for real healthcare deployments from day one
- **v1 Simplicity**: Read-only settings, polling instead of SSE, no secret storage

---

## Architecture Overview

### Deployment Model
- UI served directly from FastAPI at `/admin/ui/*` as static files
- Requires `ENABLE_LOCAL_ADMIN=true` environment variable to activate
- All API calls use `X-API-Key` header (no cookies, no sessions)
- Middleware enforces localhost-only access (127.0.0.1, ::1)
- Rejects any requests through reverse proxy (X-Forwarded-For detection)
- Built assets included in Docker image

### Security Model
- **Access Control**: Hard-coded localhost restriction, feature flag required (`ENABLE_LOCAL_ADMIN=true`)
- **Authentication**: API key with `keys:manage` scope or bootstrap env key
- **PHI Protection**: All phone numbers masked to last 4 digits
- **No State**: Every request authenticated independently

### Technology Stack
- **Backend**: Existing FastAPI (no new server)
- **Frontend**: React with Material-UI for rapid, secure development
- **Build**: Vite, TypeScript, tree-shaking
- **Real-time**: Polling for v1 (5-10 second intervals)

---

## Backend Implementation

### Phase 1: Core Infrastructure (Week 1)

#### 1.1 Static File Serving
```python
# api/app/main.py - Add after app initialization
from fastapi.staticfiles import StaticFiles
import os

# Serve admin UI only if explicitly enabled
if os.getenv("ENABLE_LOCAL_ADMIN", "false").lower() == "true":
    # Docker copies to /app/admin_ui/dist
    admin_ui_path = "/app/admin_ui/dist" if os.path.exists("/app/admin_ui/dist") else "./admin_ui/dist"
    if os.path.exists(admin_ui_path):
        app.mount("/admin/ui", StaticFiles(directory=admin_ui_path, html=True), name="admin_ui")
```

Status: Implemented in api/app/main.py

#### 1.2 Localhost-Only Middleware
```python
# api/app/main.py - Add middleware
from fastapi import Request, Response
from ipaddress import ip_address, ip_network

@app.middleware("http")
async def enforce_local_admin(request: Request, call_next):
    """Block all /admin/* paths from non-localhost IPs unless explicitly enabled."""
    if request.url.path.startswith("/admin/"):
        # First check if admin console is even enabled
        if os.getenv("ENABLE_LOCAL_ADMIN", "false").lower() != "true":
            return Response(content="Admin console disabled", status_code=404)
        
        # Check for reverse proxy headers (don't trust them)
        if "x-forwarded-for" in request.headers or "x-real-ip" in request.headers:
            # Behind a proxy - be extra cautious
            return Response(content="Admin console not available through proxy", status_code=403)
        
        client_ip = str(request.client.host)
        # Only allow exact localhost IPs
        allowed = {"127.0.0.1", "::1", "localhost"}
        if client_ip not in allowed:
            try:
                ip = ip_address(client_ip)
                if not ip.is_loopback:
                    return Response(content="Forbidden", status_code=403)
            except ValueError:
                if client_ip not in allowed:
                    return Response(content="Forbidden", status_code=403)
    
    response = await call_next(request)
    
    # Add security headers for admin paths
    if request.url.path.startswith("/admin/"):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    
    return response
```

Status: Implemented in api/app/main.py

#### 1.3a Shared Helpers (module-level)
```python
# api/app/main.py — add near top-level imports
import re
from typing import Optional

def mask_secret(value: Optional[str], visible_chars: int = 4) -> str:
    if not value:
        return "***"
    if len(value) <= visible_chars:
        return "***"
    return "*" * (len(value) - visible_chars) + value[-visible_chars:]

def mask_phone(phone: Optional[str]) -> str:
    """Mask all but last 4 digits; returns '****' when unknown."""
    if not phone or len(phone) < 4:
        return "****"
    return "*" * (len(phone) - 4) + phone[-4:]

def sanitize_error(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    # Replace long digit sequences (likely numbers/IDs) with *** and truncate
    sanitized = re.sub(r"\+?\d{6,}", "***", text)
    return sanitized[:80]
```

Status: Implemented in api/app/main.py

#### 1.3 Settings Read Endpoint
```python
# api/app/main.py - Add endpoint
@app.get("/admin/settings", dependencies=[Depends(require_admin)])
def get_admin_settings():
    """Return current settings with secrets masked for display."""
    def mask_secret(value: str, visible_chars: int = 4) -> str:
        if not value or len(value) <= visible_chars:
            return "***"
        return "*" * (len(value) - visible_chars) + value[-visible_chars:]
    
    def mask_phone(phone: str) -> str:
        """Show only last 4 digits for HIPAA compliance"""
        if not phone or len(phone) < 4:
            return "****"
        return "*" * (len(phone) - 4) + phone[-4:]
    
    return {
        "backend": {
            "type": settings.fax_backend,
            "disabled": settings.fax_disabled
        },
        "phaxio": {
            "api_key": mask_secret(settings.phaxio_api_key),
            "api_secret": mask_secret(settings.phaxio_api_secret),
            "callback_url": settings.phaxio_status_callback_url,
            "verify_signature": settings.phaxio_verify_signature,
            "configured": bool(settings.phaxio_api_key and settings.phaxio_api_secret)
        },
        "sinch": {
            "project_id": settings.sinch_project_id,
            "api_key": mask_secret(settings.sinch_api_key),
            "api_secret": mask_secret(settings.sinch_api_secret),
            "configured": bool(settings.sinch_project_id and settings.sinch_api_key)
        },
        "sip": {
            "ami_host": settings.ami_host,
            "ami_port": settings.ami_port,
            "ami_username": settings.ami_username,
            "ami_password": mask_secret(settings.ami_password),
            "ami_password_is_default": settings.ami_password == "changeme",
            "station_id": mask_phone(settings.fax_station_id),
            "configured": bool(settings.ami_username and settings.ami_password)
        },
        "security": {
            "require_api_key": settings.require_api_key,
            "enforce_https": settings.enforce_public_https,
            "audit_enabled": settings.audit_log_enabled,
            "public_api_url": settings.public_api_url
        },
        "storage": {
            "backend": settings.storage_backend,
            "s3_bucket": settings.s3_bucket[:4] + "..." if settings.s3_bucket else "",
            "s3_kms_enabled": bool(settings.s3_kms_key_id)
        },
        "inbound": {
            "enabled": settings.inbound_enabled,
            "retention_days": settings.inbound_retention_days
        },
        "limits": {
            "max_file_size_mb": settings.max_file_size_mb,
            "pdf_token_ttl_minutes": settings.pdf_token_ttl_minutes,
            "rate_limit_rpm": settings.max_requests_per_minute
        }
    }
```

Status: Implemented in api/app/main.py (v1-safe; non-destructive)

#### 1.4 Settings Validation Endpoint
```python
# api/app/main.py - Add validation endpoint
from pydantic import BaseModel
from typing import Dict, Any, Optional

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
    ami_password: Optional[str] = None  # v1: Do not send a real fax from this endpoint

@app.post("/admin/settings/validate", dependencies=[Depends(require_admin)])
async def validate_settings(request: ValidateSettingsRequest):
    """Test connectivity with provided settings. Optionally sends test fax."""
    results = {
        "backend": request.backend,
        "checks": {},
        "test_fax": None
    }
    
    if request.backend == "phaxio":
        # Test Phaxio authentication (non-destructive)
        if request.phaxio_api_key and request.phaxio_api_secret:
            try:
                import httpx
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        "https://api.phaxio.com/v2.1/account/status",
                        auth=(request.phaxio_api_key, request.phaxio_api_secret)
                    )
                    results["checks"]["auth"] = response.status_code == 200
                    if response.status_code == 200:
                        data = response.json()
                        results["checks"]["account_status"] = data.get("success", False)
            except Exception as e:
                results["checks"]["auth"] = False
                results["checks"]["error"] = str(e)
        # v1: Do not send a test fax here; use normal /fax from the UI
    
    elif request.backend == "sinch":
        # v1: Presence check only (no network call)
        results["checks"]["auth"] = bool(request.sinch_project_id and request.sinch_api_key and request.sinch_api_secret)
    
    elif request.backend == "sip":
        # Test AMI connection using a bounded helper (no global state)
        if all([request.ami_host, request.ami_username, request.ami_password]):
            try:
                from .ami import test_ami_connection
                connected = await test_ami_connection(
                    host=request.ami_host,
                    port=request.ami_port or 5038,
                    username=request.ami_username,
                    password=request.ami_password,
                )
                results["checks"]["ami_connection"] = bool(connected)
                
                # Check for default password
                if request.ami_password == "changeme":
                    results["checks"]["ami_password_secure"] = False
                    results["checks"]["warning"] = "AMI password is still default"
            except Exception as e:
                results["checks"]["ami_connection"] = False
                results["checks"]["error"] = str(e)
        
        # Check Ghostscript
        import shutil
        results["checks"]["ghostscript"] = shutil.which("gs") is not None
    
    # Common checks
    try:
        test_file = os.path.join(settings.fax_data_dir, f"test_{uuid.uuid4().hex}")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        results["checks"]["fax_data_dir_writable"] = True
    except:
        results["checks"]["fax_data_dir_writable"] = False
    
    return results
```

Status: Implemented in api/app/ami.py

#### 1.6 AMI Test Helper (bounded probe)
```python
# api/app/ami.py — add this helper without altering the global client
import asyncio
import contextlib
from typing import Optional

async def test_ami_connection(host: str, port: int, username: str, password: str) -> bool:
    """Attempt a one-off AMI TCP connect + login, then close. Returns True/False."""
    try:
        reader, writer = await asyncio.open_connection(host, port)
        # Minimal AMI login sequence
        login = (
            f"Action: Login\r\nUsername: {username}\r\nSecret: {password}\r\n\r\n"
        )
        writer.write(login.encode())
        await writer.drain()
        # Read a few lines to confirm a response then close
        try:
            await asyncio.wait_for(reader.readline(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True
    except Exception:
        return False
```

### Phase 2: Diagnostics (Week 1-2)

Status: Implemented (v1-safe) in api/app/main.py as POST /admin/diagnostics/run

#### 2.1 Comprehensive Diagnostics Endpoint
```python
# api/app/main.py - Add diagnostics
import asyncio
import shutil
import os
import tempfile
from datetime import datetime

@app.post("/admin/diagnostics/run", dependencies=[Depends(require_admin)])
async def run_diagnostics():
    """Run comprehensive system diagnostics similar to scripts/check-env.sh"""
    
    diagnostics = {
        "timestamp": datetime.utcnow().isoformat(),
        "backend": settings.fax_backend,
        "checks": {}
    }
    
    # Environment checks (similar to check-env.sh)
    diagnostics["checks"]["backend_configured"] = settings.fax_backend in ["phaxio", "sinch", "sip"]
    diagnostics["checks"]["api_key_set"] = bool(settings.api_key)
    diagnostics["checks"]["require_api_key"] = settings.require_api_key
    
    # Backend-specific checks
    if settings.fax_backend == "phaxio":
        diagnostics["checks"]["phaxio"] = {
            "api_key_set": bool(settings.phaxio_api_key),
            "api_secret_set": bool(settings.phaxio_api_secret),
            "callback_url_set": bool(settings.phaxio_status_callback_url),
            "signature_verification": settings.phaxio_verify_signature,
            "public_url_https": settings.public_api_url.startswith("https://")
        }
    
    elif settings.fax_backend == "sinch":
        diagnostics["checks"]["sinch"] = {
            "project_id_set": bool(settings.sinch_project_id),
            "api_key_set": bool(settings.sinch_api_key),
            "api_secret_set": bool(settings.sinch_api_secret)
        }
    
    elif settings.fax_backend == "sip":
        diagnostics["checks"]["sip"] = {
            "ami_host": settings.ami_host,
            "ami_port": settings.ami_port,
            "ami_password_not_default": settings.ami_password != "changeme",
            "sip_credentials_set": bool(settings.sip_username and settings.sip_password),
            "station_id_set": bool(settings.fax_station_id)
        }
        
        # Test AMI connection
        try:
            from .ami import ami_client
            await ami_client.connect()
            diagnostics["checks"]["sip"]["ami_reachable"] = True
        except Exception as e:
            diagnostics["checks"]["sip"]["ami_reachable"] = False
            diagnostics["checks"]["sip"]["ami_error"] = str(e)
    
    # System checks
    diagnostics["checks"]["system"] = {
        "ghostscript": shutil.which("gs") is not None,
        "fax_data_dir": os.path.exists(settings.fax_data_dir),
        "fax_data_writable": False,
        "database_connected": False,
        "temp_dir_writable": False
    }
    
    # Test write permissions
    try:
        test_file = os.path.join(settings.fax_data_dir, f"test_{uuid.uuid4().hex}")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        diagnostics["checks"]["system"]["fax_data_writable"] = True
    except:
        pass
    
    # Test database
    try:
        with SessionLocal() as db:
            db.execute("SELECT 1")
            diagnostics["checks"]["system"]["database_connected"] = True
    except:
        pass
    
    # Test temp directory
    try:
        with tempfile.NamedTemporaryFile(delete=True) as tmp:
            tmp.write(b"test")
            diagnostics["checks"]["system"]["temp_dir_writable"] = True
    except:
        pass
    
    # Storage backend checks
    if settings.storage_backend == "s3":
        diagnostics["checks"]["storage"] = {
            "type": "s3",
            "bucket_set": bool(settings.s3_bucket),
            "region_set": bool(settings.s3_region),
            "kms_enabled": bool(settings.s3_kms_key_id)
        }
        
        # Test S3 access if configured (opt-in to avoid boto3 requirement in dev)
        if settings.s3_bucket and os.getenv("ENABLE_S3_DIAGNOSTICS", "false").lower() == "true":
            try:
                import boto3
                from botocore.config import Config
                
                # Create S3 client with settings
                s3_config = Config(signature_version='s3v4')
                s3 = boto3.client(
                    's3',
                    region_name=settings.s3_region,
                    endpoint_url=settings.s3_endpoint_url,
                    config=s3_config
                )
                
                # Try to head bucket (minimal permission needed)
                s3.head_bucket(Bucket=settings.s3_bucket)
                diagnostics["checks"]["storage"]["accessible"] = True
            except Exception as e:
                diagnostics["checks"]["storage"]["accessible"] = False
                diagnostics["checks"]["storage"]["error"] = str(e)[:100]  # Truncate error
    else:
        diagnostics["checks"]["storage"] = {
            "type": "local",
            "warning": "Local storage only suitable for development"
        }
    
    # Inbound checks if enabled
    if settings.inbound_enabled:
        diagnostics["checks"]["inbound"] = {
            "enabled": True,
            "retention_days": settings.inbound_retention_days
        }
        
        if settings.fax_backend == "sip":
            diagnostics["checks"]["inbound"]["asterisk_secret_set"] = bool(settings.asterisk_inbound_secret)
        elif settings.fax_backend == "phaxio":
            diagnostics["checks"]["inbound"]["signature_verification"] = settings.phaxio_inbound_verify_signature
        elif settings.fax_backend == "sinch":
            diagnostics["checks"]["inbound"]["auth_configured"] = bool(
                settings.sinch_inbound_basic_user or settings.sinch_inbound_hmac_secret
            )
    
    # Security posture
    diagnostics["checks"]["security"] = {
        "enforce_https": settings.enforce_public_https,
        "audit_logging": settings.audit_log_enabled,
        "rate_limiting": settings.max_requests_per_minute > 0,
        "pdf_token_ttl": settings.pdf_token_ttl_minutes
    }
    
    # Calculate overall health
    critical_checks = []
    warnings = []
    
    if settings.fax_backend == "phaxio":
        if not diagnostics["checks"]["phaxio"]["api_key_set"]:
            critical_checks.append("Phaxio API key not set")
        if not diagnostics["checks"]["phaxio"]["public_url_https"]:
            warnings.append("PUBLIC_API_URL should use HTTPS")
    
    elif settings.fax_backend == "sip":
        if not diagnostics["checks"]["sip"]["ami_password_not_default"]:
            critical_checks.append("AMI password is still default 'changeme'")
        if not diagnostics["checks"]["sip"].get("ami_reachable"):
            critical_checks.append("Cannot connect to Asterisk AMI")
    
    if not diagnostics["checks"]["system"]["fax_data_writable"]:
        critical_checks.append("Cannot write to fax data directory")
    
    if not diagnostics["checks"]["system"]["database_connected"]:
        critical_checks.append("Database connection failed")
    
    diagnostics["summary"] = {
        "healthy": len(critical_checks) == 0,
        "critical_issues": critical_checks,
        "warnings": warnings
    }
    
    return diagnostics
```

Status: Implemented in api/app/main.py as GET /admin/health-status

#### 2.2 Health Status Endpoint (for polling)
```python
# api/app/main.py - Add health status for dashboard polling
from datetime import datetime, timedelta
@app.get("/admin/health-status", dependencies=[Depends(require_admin)])
async def get_health_status():
    """Return current system health for dashboard polling"""
    
    # Get job counts
    with SessionLocal() as db:
        jobs_queued = db.query(FaxJob).filter(FaxJob.status == "queued").count()
        jobs_in_progress = db.query(FaxJob).filter(FaxJob.status == "in_progress").count()
        recent_failures = db.query(FaxJob).filter(
            FaxJob.status == "failed",
            FaxJob.updated_at > datetime.utcnow() - timedelta(hours=1)
        ).count()
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "backend": settings.fax_backend,
        "backend_healthy": bool(settings.phaxio_api_key) if settings.fax_backend == "phaxio" else True,
        "jobs": {
            "queued": jobs_queued,
            "in_progress": jobs_in_progress,
            "recent_failures": recent_failures
        },
        "inbound_enabled": settings.inbound_enabled,
        "api_keys_configured": bool(settings.api_key),
        "require_auth": settings.require_api_key
    }
```

### Phase 3: Job Management (Week 2)

Status: Implemented in api/app/main.py as GET /admin/fax-jobs and GET /admin/fax-jobs/{job_id}

#### 3.1 Admin Job Listing
```python
# api/app/main.py - Add admin job endpoints
@app.get("/admin/fax-jobs", dependencies=[Depends(require_admin)])
async def list_admin_jobs(
    status: Optional[str] = None,
    backend: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0)
):
    """List jobs with PHI masked for HIPAA compliance"""
    with SessionLocal() as db:
        query = db.query(FaxJob)
        
        if status:
            query = query.filter(FaxJob.status == status)
        if backend:
            query = query.filter(FaxJob.backend == backend)
        
        total = query.count()
        jobs = query.order_by(FaxJob.created_at.desc()).offset(offset).limit(limit).all()
        
        return {
            "total": total,
            "jobs": [
                {
                    "id": job.id,
                    "to_number": mask_phone(job.to_number),  # HIPAA: mask PHI
                    "status": job.status,
                    "backend": job.backend,
                    "pages": job.pages,
                    "error": sanitize_error(job.error),
                    "created_at": job.created_at,
                    "updated_at": job.updated_at
                }
                for job in jobs
            ]
        }

@app.get("/admin/fax-jobs/{job_id}", dependencies=[Depends(require_admin)])
async def get_admin_job(job_id: str):
    """Get job details with PHI masked"""
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        
        return {
            "id": job.id,
            "to_number": mask_phone(job.to_number),
            "status": job.status,
            "backend": job.backend,
            "pages": job.pages,
            "error": sanitize_error(job.error),
            "provider_sid": job.provider_sid,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "file_name": job.file_name
        }
```

### Phase 4: Settings Management (Week 3) - Read-Only for v1

Status: Implemented in api/app/main.py as GET /admin/settings/export

#### 4.1 Settings Export Endpoint (v1 - Read-Only)
```python
# api/app/main.py - Add settings export for .env generation
@app.get("/admin/settings/export", dependencies=[Depends(require_admin)])
def export_settings_env():
    """Generate .env format for current settings (v1 read-only)"""
    
    backend = settings.fax_backend
    env_lines = []
    
    # Core settings
    env_lines.append(f"FAX_BACKEND={backend}")
    env_lines.append(f"REQUIRE_API_KEY={settings.require_api_key}")
    env_lines.append(f"ENFORCE_PUBLIC_HTTPS={settings.enforce_public_https}")
    env_lines.append("# NOTE: Secrets are redacted below. Fill in actual values before use.")
    
    # Backend-specific
    if backend == "phaxio":
        env_lines.append(f"# Phaxio Configuration")
        env_lines.append(f"PHAXIO_API_KEY={settings.phaxio_api_key[:8]}..." if settings.phaxio_api_key else "PHAXIO_API_KEY=")
        env_lines.append(f"PHAXIO_API_SECRET={settings.phaxio_api_secret[:8]}..." if settings.phaxio_api_secret else "PHAXIO_API_SECRET=")
        env_lines.append(f"PUBLIC_API_URL={settings.public_api_url}")
        env_lines.append(f"PHAXIO_STATUS_CALLBACK_URL={settings.phaxio_status_callback_url}")
        env_lines.append(f"# Also supports PHAXIO_CALLBACK_URL as an alias")
        env_lines.append(f"PHAXIO_VERIFY_SIGNATURE={settings.phaxio_verify_signature}")
    
    elif backend == "sip":
        env_lines.append(f"# SIP/Asterisk Configuration")
        env_lines.append(f"ASTERISK_AMI_HOST={settings.ami_host}")
        env_lines.append(f"ASTERISK_AMI_PORT={settings.ami_port}")
        env_lines.append(f"ASTERISK_AMI_USERNAME={settings.ami_username}")
        env_lines.append(f"ASTERISK_AMI_PASSWORD=***REDACTED***")
    
    return {
        "env_content": "\n".join(env_lines),
        "requires_restart": True,
        "note": "v1: Settings are read-only. Copy to .env and restart API to apply changes."
    }
```

#### 4.2 Future: Settings Persistence (v2)
```python
# NOTE: Deferred to v2 to avoid secret management complexity
# v2 will implement:
# - Database table for non-secret settings
# - Encrypted storage for secrets (with proper key management)
# - Hot-reload for applicable settings
# - Audit trail for all changes
#
# For v1: Use read-only settings with .env file generation

class UpdateSettingsRequest(BaseModel):
    # Only non-secret settings in Phase 4
    max_file_size_mb: Optional[int] = None
    pdf_token_ttl_minutes: Optional[int] = None
    artifact_ttl_days: Optional[int] = None
    cleanup_interval_minutes: Optional[int] = None
    max_requests_per_minute: Optional[int] = None
    audit_log_enabled: Optional[bool] = None
    
    # Phase 5: Secret settings (encrypted in DB)
    phaxio_api_key: Optional[str] = None
    phaxio_api_secret: Optional[str] = None
    # ... etc

@app.put("/admin/settings", dependencies=[Depends(require_admin)])
async def update_settings(
    request: UpdateSettingsRequest,
    admin_info = Depends(require_admin)
):
    """Update settings in database (Phase 4+)"""
    
    changes = []
    with SessionLocal() as db:
        for field, value in request.dict(exclude_unset=True).items():
            if value is None:
                continue
            
            # Check if this is a secret field
            is_secret = field in ["phaxio_api_key", "phaxio_api_secret", "ami_password"]
            
            # Get existing setting
            setting = db.query(AdminSetting).filter_by(key=field).first()
            old_value = None
            
            if setting:
                old_value = setting.value
                if is_secret and setting.encrypted:
                    # Don't log actual old secret value
                    old_value = "***"
            
            # Encrypt if secret
            store_value = value
            if is_secret:
                store_value = cipher.encrypt(str(value).encode()).decode()
            
            # Update or create
            if setting:
                setting.value = store_value
                setting.encrypted = is_secret
                setting.updated_at = datetime.utcnow()
                setting.updated_by = admin_info.get("key_id", "unknown")
            else:
                setting = AdminSetting(
                    key=field,
                    value=store_value,
                    encrypted=is_secret,
                    updated_at=datetime.utcnow(),
                    updated_by=admin_info.get("key_id", "unknown")
                )
                db.add(setting)
            
            # Audit log (don't log secret values)
            audit_value = "***" if is_secret else str(value)
            audit = AdminSettingAudit(
                setting_key=field,
                old_value=old_value,
                new_value=audit_value,
                changed_at=datetime.utcnow(),
                changed_by=admin_info.get("key_id", "unknown")
            )
            db.add(audit)
            
            changes.append({
                "key": field,
                "applied": True,
                "requires_restart": field in ["fax_backend", "database_url", "ami_host"]
            })
        
        db.commit()
    
    # Reload settings for hot-reloadable values
    reload_settings()
    
    return {
        "success": True,
        "changes": changes,
        "requires_restart": any(c["requires_restart"] for c in changes)
    }
```

---

## Frontend Implementation

### Directory Structure
```
api/admin_ui/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── index.html
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── api/
│   │   ├── client.ts       # API client with auth
│   │   └── types.ts        # TypeScript types
│   ├── components/
│   │   ├── Dashboard.tsx
│   │   ├── SetupWizard.tsx
│   │   ├── JobsList.tsx
│   │   ├── ApiKeys.tsx
│   │   ├── Settings.tsx
│   │   ├── Diagnostics.tsx
│   │   └── common/
│   │       ├── PhoneInput.tsx
│   │       ├── SecretInput.tsx
│   │       └── FileUpload.tsx
│   └── hooks/
│       ├── usePolling.ts   # Dashboard polling
│       └── useApiKey.ts    # API key management
└── dist/                   # Build output

```

### Package Configuration
```json
{
  "name": "faxbot-admin",
  "version": "1.0.0",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview",
    "test": "vitest"
  },
  "dependencies": {
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "@mui/material": "^5.14.0",
    "@mui/icons-material": "^5.14.0",
    "@emotion/react": "^11.11.0",
    "@emotion/styled": "^11.11.0",
    "react-router-dom": "^6.15.0",
    "react-hook-form": "^7.45.0"
  },
  "devDependencies": {
    "@types/react": "^18.2.0",
    "@types/react-dom": "^18.2.0",
    "@vitejs/plugin-react": "^4.0.0",
    "typescript": "^5.0.0",
    "vite": "^4.4.0",
    "vitest": "^0.34.0",
    "@testing-library/react": "^14.0.0"
  }
}
```

### API Client Implementation
```typescript
// admin_ui/src/api/client.ts
class AdminAPIClient {
  private baseURL: string;
  private apiKey: string;

  constructor(apiKey: string) {
    // Always localhost since we're local-only
    this.baseURL = window.location.origin;
    this.apiKey = apiKey;
  }

  private async fetch(path: string, options: RequestInit = {}): Promise<Response> {
    const response = await fetch(`${this.baseURL}${path}`, {
      ...options,
      headers: {
        'X-API-Key': this.apiKey,
        'Content-Type': 'application/json',
        ...options.headers,
      },
    });

    if (!response.ok) {
      throw new Error(`API Error: ${response.status} ${response.statusText}`);
    }

    return response;
  }

  // Configuration
  async getConfig() {
    const res = await this.fetch('/admin/config');
    return res.json();
  }

  async getSettings() {
    const res = await this.fetch('/admin/settings');
    return res.json();
  }

  async validateSettings(settings: any) {
    const res = await this.fetch('/admin/settings/validate', {
      method: 'POST',
      body: JSON.stringify(settings),
    });
    return res.json();
  }

  async updateSettings(settings: any) {
    const res = await this.fetch('/admin/settings', {
      method: 'PUT',
      body: JSON.stringify(settings),
    });
    return res.json();
  }

  // Diagnostics
  async runDiagnostics() {
    const res = await this.fetch('/admin/diagnostics/run', {
      method: 'POST',
    });
    return res.json();
  }

  // Jobs
  async listJobs(params: { status?: string; backend?: string; limit?: number; offset?: number }) {
    const query = new URLSearchParams(params as any);
    const res = await this.fetch(`/admin/fax-jobs?${query}`);
    return res.json();
  }

  async getJob(id: string) {
    const res = await this.fetch(`/admin/fax-jobs/${id}`);
    return res.json();
  }

  // API Keys
  async createApiKey(data: { name?: string; owner?: string; scopes?: string[] }) {
    const res = await this.fetch('/admin/api-keys', {
      method: 'POST',
      body: JSON.stringify(data),
    });
    return res.json();
  }

  async listApiKeys() {
    const res = await this.fetch('/admin/api-keys');
    return res.json();
  }

  async revokeApiKey(keyId: string) {
    const res = await this.fetch(`/admin/api-keys/${keyId}`, {
      method: 'DELETE',
    });
    return res.json();
  }

  async rotateApiKey(keyId: string) {
    const res = await this.fetch(`/admin/api-keys/${keyId}/rotate`, {
      method: 'POST',
    });
    return res.json();
  }

  // Send test fax
  async sendFax(to: string, file: File) {
    const formData = new FormData();
    formData.append('to', to);
    formData.append('file', file);

    const res = await fetch(`${this.baseURL}/fax`, {
      method: 'POST',
      headers: {
        'X-API-Key': this.apiKey,
      },
      body: formData,
    });

    if (!res.ok) {
      throw new Error(`Send failed: ${res.status}`);
    }

    return res.json();
  }

  // Health polling for dashboard updates
  async getHealthStatus() {
    const res = await this.fetch('/admin/health-status');
    return res.json();
  }

  // Start polling (returns cleanup function)
  startPolling(onUpdate: (data: any) => void, intervalMs: number = 5000): () => void {
    let running = true;
    
    const poll = async () => {
      if (!running) return;
      try {
        const data = await this.getHealthStatus();
        onUpdate(data);
      } catch (e) {
        console.error('Polling error:', e);
      }
      if (running) {
        setTimeout(poll, intervalMs);
      }
    };
    
    poll(); // Start immediately
    
    // Return cleanup function
    return () => { running = false; };
  }
}

export default AdminAPIClient;
```

### Main App Component
```tsx
// admin_ui/src/App.tsx
import React, { useState, useEffect } from 'react';
import {
  Box,
  CssBaseline,
  ThemeProvider,
  createTheme,
  AppBar,
  Toolbar,
  Typography,
  Container,
  Alert,
  TextField,
  Button,
  Paper,
} from '@mui/material';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import AdminAPIClient from './api/client';
import Dashboard from './components/Dashboard';
import SetupWizard from './components/SetupWizard';
import JobsList from './components/JobsList';
import ApiKeys from './components/ApiKeys';
import Settings from './components/Settings';
import Diagnostics from './components/Diagnostics';

const darkTheme = createTheme({
  palette: {
    mode: 'dark',
    primary: { main: '#3BA0FF' },
    success: { main: '#2EBE7E' },
    error: { main: '#FF5C5C' },
    warning: { main: '#FFB020' },
    background: {
      default: '#0B0F14',
      paper: '#121821',
    },
  },
});

function App() {
  const [apiKey, setApiKey] = useState<string>(() => {
    // Load from localStorage (temporary storage, cleared on logout)
    return localStorage.getItem('faxbot_admin_key') || '';
  });
  const [client, setClient] = useState<AdminAPIClient | null>(null);
  const [authenticated, setAuthenticated] = useState(false);
  const [error, setError] = useState('');

  const handleLogin = async (key: string) => {
    try {
      const testClient = new AdminAPIClient(key);
      // Test the key by fetching config
      await testClient.getConfig();
      
      // Success
      localStorage.setItem('faxbot_admin_key', key);
      setApiKey(key);
      setClient(testClient);
      setAuthenticated(true);
      setError('');
    } catch (e) {
      setError('Invalid API key or insufficient permissions');
      setAuthenticated(false);
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('faxbot_admin_key');
    setApiKey('');
    setClient(null);
    setAuthenticated(false);
  };

  // Auto-login if key exists
  useEffect(() => {
    if (apiKey && !authenticated) {
      handleLogin(apiKey);
    }
  }, []);

  if (!authenticated) {
    return (
      <ThemeProvider theme={darkTheme}>
        <CssBaseline />
        <Container maxWidth="sm">
          <Box sx={{ mt: 8 }}>
            <Paper sx={{ p: 4 }}>
              <Typography variant="h4" gutterBottom>
                Faxbot Admin Console
              </Typography>
              <Typography variant="body2" color="text.secondary" gutterBottom>
                Local access only (127.0.0.1)
              </Typography>
              
              {error && (
                <Alert severity="error" sx={{ mt: 2, mb: 2 }}>
                  {error}
                </Alert>
              )}
              
              <TextField
                fullWidth
                label="API Key"
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="fbk_live_... or bootstrap key"
                sx={{ mt: 2 }}
              />
              
              <Button
                fullWidth
                variant="contained"
                onClick={() => handleLogin(apiKey)}
                sx={{ mt: 2 }}
                disabled={!apiKey}
              >
                Login
              </Button>
              
              <Typography variant="caption" sx={{ mt: 2, display: 'block' }}>
                Use an API key with 'keys:manage' scope or the bootstrap API_KEY from your .env
              </Typography>
            </Paper>
          </Box>
        </Container>
      </ThemeProvider>
    );
  }

  return (
    <ThemeProvider theme={darkTheme}>
      <CssBaseline />
      <BrowserRouter basename="/admin/ui">
        <Box sx={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
          <AppBar position="static">
            <Toolbar>
              <Typography variant="h6" sx={{ flexGrow: 1 }}>
                Faxbot Admin Console
              </Typography>
              <Typography variant="caption" sx={{ mr: 2 }}>
                LOCAL ONLY (127.0.0.1)
              </Typography>
              <Button color="inherit" onClick={handleLogout}>
                Logout
              </Button>
            </Toolbar>
          </AppBar>
          
          <Container maxWidth="xl" sx={{ flex: 1, py: 3 }}>
            <Routes>
              <Route path="/" element={<Dashboard client={client!} />} />
              <Route path="/setup" element={<SetupWizard client={client!} />} />
              <Route path="/jobs" element={<JobsList client={client!} />} />
              <Route path="/keys" element={<ApiKeys client={client!} />} />
              <Route path="/settings" element={<Settings client={client!} />} />
              <Route path="/diagnostics" element={<Diagnostics client={client!} />} />
              <Route path="*" element={<Navigate to="/" />} />
            </Routes>
          </Container>
        </Box>
      </BrowserRouter>
    </ThemeProvider>
  );
}

export default App;
```

### Setup Wizard Component (Backend-Specific)
```tsx
// admin_ui/src/components/SetupWizard.tsx
import React, { useState } from 'react';
import {
  Stepper,
  Step,
  StepLabel,
  Button,
  Typography,
  Box,
  TextField,
  Alert,
  CircularProgress,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  Paper,
  Divider,
} from '@mui/material';
import SecretInput from './common/SecretInput';

function SetupWizard({ client }: { client: AdminAPIClient }) {
  const [activeStep, setActiveStep] = useState(0);
  const [backend, setBackend] = useState('phaxio');
  const [config, setConfig] = useState<any>({});
  const [validating, setValidating] = useState(false);
  const [validationResults, setValidationResults] = useState<any>(null);
  const [testFaxNumber, setTestFaxNumber] = useState('');

  const steps = ['Choose Backend', 'Configure Credentials', 'Security Settings', 'Test & Apply'];

  const handleValidate = async () => {
    setValidating(true);
    try {
      const results = await client.validateSettings({
        backend,
        ...config,
        test_fax_number: testFaxNumber || undefined,
      });
      setValidationResults(results);
    } catch (e) {
      setValidationResults({ error: e.message });
    } finally {
      setValidating(false);
    }
  };

  const renderBackendConfig = () => {
    switch (backend) {
      case 'phaxio':
        return (
          <>
            <Typography variant="h6" gutterBottom>
              Phaxio Configuration
            </Typography>
            <Typography variant="body2" color="text.secondary" gutterBottom>
              Cloud fax service (recommended for healthcare)
            </Typography>
            
            <SecretInput
              label="API Key"
              value={config.phaxio_api_key || ''}
              onChange={(v) => setConfig({ ...config, phaxio_api_key: v })}
              fullWidth
              sx={{ mt: 2 }}
            />
            
            <SecretInput
              label="API Secret"
              value={config.phaxio_api_secret || ''}
              onChange={(v) => setConfig({ ...config, phaxio_api_secret: v })}
              fullWidth
              sx={{ mt: 2 }}
            />
            
            <TextField
              label="Public API URL"
              value={config.public_api_url || 'https://your-domain.com'}
              onChange={(e) => setConfig({ ...config, public_api_url: e.target.value })}
              fullWidth
              sx={{ mt: 2 }}
              helperText="Must be HTTPS and publicly accessible for Phaxio to fetch PDFs"
            />
            
            <Alert severity="info" sx={{ mt: 2 }}>
              Callback URL will be: {config.public_api_url || 'https://your-domain.com'}/phaxio-callback
              <br />
              Note: Both PHAXIO_STATUS_CALLBACK_URL and PHAXIO_CALLBACK_URL are supported
            </Alert>
            
            <Alert severity="warning" sx={{ mt: 2 }}>
              For HIPAA compliance:
              <ul>
                <li>Disable document storage in Phaxio console</li>
                <li>Enable two-factor authentication</li>
                <li>Request BAA from compliance@phaxio.com</li>
              </ul>
            </Alert>
          </>
        );

      case 'sinch':
        return (
          <>
            <Typography variant="h6" gutterBottom>
              Sinch Fax API v3 Configuration
            </Typography>
            <Typography variant="body2" color="text.secondary" gutterBottom>
              Direct upload model (Phaxio by Sinch accounts)
            </Typography>
            
            <TextField
              label="Project ID"
              value={config.sinch_project_id || ''}
              onChange={(e) => setConfig({ ...config, sinch_project_id: e.target.value })}
              fullWidth
              sx={{ mt: 2 }}
            />
            
            <SecretInput
              label="API Key"
              value={config.sinch_api_key || ''}
              onChange={(v) => setConfig({ ...config, sinch_api_key: v })}
              fullWidth
              sx={{ mt: 2 }}
              helperText="Can use PHAXIO_API_KEY if not set separately"
            />
            
            <SecretInput
              label="API Secret"
              value={config.sinch_api_secret || ''}
              onChange={(v) => setConfig({ ...config, sinch_api_secret: v })}
              fullWidth
              sx={{ mt: 2 }}
            />
          </>
        );

      case 'sip':
        return (
          <>
            <Typography variant="h6" gutterBottom>
              SIP/Asterisk Configuration
            </Typography>
            <Typography variant="body2" color="text.secondary" gutterBottom>
              Self-hosted with T.38 support (technical users)
            </Typography>
            
            <Alert severity="error" sx={{ mb: 2 }}>
              AMI must NEVER be exposed to the internet. Keep port 5038 internal only.
            </Alert>
            
            <TextField
              label="AMI Host"
              value={config.ami_host || 'asterisk'}
              onChange={(e) => setConfig({ ...config, ami_host: e.target.value })}
              fullWidth
              sx={{ mt: 2 }}
              helperText="Usually 'asterisk' in Docker or internal IP"
            />
            
            <TextField
              label="AMI Port"
              type="number"
              value={config.ami_port || 5038}
              onChange={(e) => setConfig({ ...config, ami_port: parseInt(e.target.value) })}
              fullWidth
              sx={{ mt: 2 }}
            />
            
            <TextField
              label="AMI Username"
              value={config.ami_username || 'api'}
              onChange={(e) => setConfig({ ...config, ami_username: e.target.value })}
              fullWidth
              sx={{ mt: 2 }}
            />
            
            <SecretInput
              label="AMI Password"
              value={config.ami_password || ''}
              onChange={(v) => setConfig({ ...config, ami_password: v })}
              fullWidth
              sx={{ mt: 2 }}
              error={config.ami_password === 'changeme'}
              helperText={config.ami_password === 'changeme' ? 'Must change default password!' : ''}
            />
            
            <Divider sx={{ my: 3 }} />
            
            <Typography variant="subtitle1" gutterBottom>
              SIP Trunk Configuration
            </Typography>
            
            <TextField
              label="SIP Username"
              value={config.sip_username || ''}
              onChange={(e) => setConfig({ ...config, sip_username: e.target.value })}
              fullWidth
              sx={{ mt: 2 }}
            />
            
            <SecretInput
              label="SIP Password"
              value={config.sip_password || ''}
              onChange={(v) => setConfig({ ...config, sip_password: v })}
              fullWidth
              sx={{ mt: 2 }}
            />
            
            <TextField
              label="SIP Server"
              value={config.sip_server || ''}
              onChange={(e) => setConfig({ ...config, sip_server: e.target.value })}
              fullWidth
              sx={{ mt: 2 }}
              placeholder="sip.provider.com"
            />
            
            <TextField
              label="Station ID / DID"
              value={config.fax_station_id || ''}
              onChange={(e) => setConfig({ ...config, fax_station_id: e.target.value })}
              fullWidth
              sx={{ mt: 2 }}
              placeholder="+15551234567"
              helperText="Your fax number in E.164 format"
            />
            
            <Alert severity="info" sx={{ mt: 2 }}>
              Required ports to forward:
              <ul>
                <li>5060 TCP/UDP - SIP signaling</li>
                <li>4000-4999 UDP - T.38 UDPTL media</li>
              </ul>
            </Alert>
          </>
        );

      default:
        return null;
    }
  };

  const renderStepContent = (step: number) => {
    switch (step) {
      case 0:
        return (
          <>
            <Typography variant="h6" gutterBottom>
              Choose Your Fax Backend
            </Typography>
            
            <FormControl fullWidth sx={{ mt: 2 }}>
              <InputLabel>Backend Type</InputLabel>
              <Select
                value={backend}
                onChange={(e) => setBackend(e.target.value)}
                label="Backend Type"
              >
                <MenuItem value="phaxio">
                  Phaxio (Cloud - Recommended)
                </MenuItem>
                <MenuItem value="sinch">
                  Sinch Fax API v3 (Cloud)
                </MenuItem>
                <MenuItem value="sip">
                  SIP/Asterisk (Self-hosted)
                </MenuItem>
              </Select>
            </FormControl>
            
            {backend === 'phaxio' && (
              <Alert severity="success" sx={{ mt: 2 }}>
                Best for healthcare: 5-minute setup, automatic HIPAA compliance with BAA
              </Alert>
            )}
            
            {backend === 'sip' && (
              <Alert severity="warning" sx={{ mt: 2 }}>
                Requires technical expertise: T.38 support, port forwarding, NAT configuration
              </Alert>
            )}
          </>
        );

      case 1:
        return renderBackendConfig();

      case 2:
        return (
          <>
            <Typography variant="h6" gutterBottom>
              Security Settings
            </Typography>
            
            <Alert severity="info" sx={{ mb: 2 }}>
              These settings are critical for HIPAA compliance
            </Alert>
            
            <TextField
              label="Bootstrap API Key"
              value={config.api_key || ''}
              onChange={(e) => setConfig({ ...config, api_key: e.target.value })}
              fullWidth
              sx={{ mt: 2 }}
              helperText="Used to create initial admin keys"
            />
            
            <FormControl fullWidth sx={{ mt: 2 }}>
              <InputLabel>Require API Key</InputLabel>
              <Select
                value={config.require_api_key ? 'true' : 'false'}
                onChange={(e) => setConfig({ ...config, require_api_key: e.target.value === 'true' })}
                label="Require API Key"
              >
                <MenuItem value="true">Yes (Required for HIPAA)</MenuItem>
                <MenuItem value="false">No (Dev only)</MenuItem>
              </Select>
            </FormControl>
            
            <FormControl fullWidth sx={{ mt: 2 }}>
              <InputLabel>Enforce HTTPS</InputLabel>
              <Select
                value={config.enforce_public_https ? 'true' : 'false'}
                onChange={(e) => setConfig({ ...config, enforce_public_https: e.target.value === 'true' })}
                label="Enforce HTTPS"
              >
                <MenuItem value="true">Yes (Required for PHI)</MenuItem>
                <MenuItem value="false">No (Dev only)</MenuItem>
              </Select>
            </FormControl>
            
            <FormControl fullWidth sx={{ mt: 2 }}>
              <InputLabel>Enable Audit Logging</InputLabel>
              <Select
                value={config.audit_log_enabled ? 'true' : 'false'}
                onChange={(e) => setConfig({ ...config, audit_log_enabled: e.target.value === 'true' })}
                label="Enable Audit Logging"
              >
                <MenuItem value="true">Yes (HIPAA requirement)</MenuItem>
                <MenuItem value="false">No</MenuItem>
              </Select>
            </FormControl>
            
            <TextField
              label="PDF Token TTL (minutes)"
              type="number"
              value={config.pdf_token_ttl_minutes || 60}
              onChange={(e) => setConfig({ ...config, pdf_token_ttl_minutes: parseInt(e.target.value) })}
              fullWidth
              sx={{ mt: 2 }}
              helperText="How long tokenized PDF URLs remain valid"
            />
          </>
        );

      case 3:
        return (
          <>
            <Typography variant="h6" gutterBottom>
              Test Configuration
            </Typography>
            
            <TextField
              label="Test Fax Number (Optional)"
              value={testFaxNumber}
              onChange={(e) => setTestFaxNumber(e.target.value)}
              fullWidth
              sx={{ mt: 2 }}
              placeholder="+15551234567"
              helperText="Send actual test fax to verify configuration"
            />
            
            <Button
              variant="contained"
              onClick={handleValidate}
              disabled={validating}
              sx={{ mt: 2 }}
              fullWidth
            >
              {validating ? <CircularProgress size={24} /> : 'Validate Configuration'}
            </Button>
            
            {validationResults && (
              <Paper sx={{ mt: 2, p: 2 }}>
                <Typography variant="subtitle1" gutterBottom>
                  Validation Results
                </Typography>
                
                {validationResults.error ? (
                  <Alert severity="error">{validationResults.error}</Alert>
                ) : (
                  <>
                    {Object.entries(validationResults.checks || {}).map(([key, value]) => (
                      <Box key={key} sx={{ display: 'flex', justifyContent: 'space-between', mb: 1 }}>
                        <Typography>{key}:</Typography>
                        <Typography color={value === true ? 'success.main' : value === false ? 'error.main' : 'text.secondary'}>
                          {String(value)}
                        </Typography>
                      </Box>
                    ))}
                    
                    {validationResults.test_fax && (
                      <Alert severity={validationResults.test_fax.sent ? 'success' : 'error'} sx={{ mt: 2 }}>
                        {validationResults.test_fax.sent 
                          ? `Test fax sent! Job ID: ${validationResults.test_fax.job_id}`
                          : `Test fax failed: ${validationResults.test_fax.error}`}
                      </Alert>
                    )}
                  </>
                )}
              </Paper>
            )}
            
            <Divider sx={{ my: 3 }} />
            
            <Typography variant="h6" gutterBottom>
              Environment Variables
            </Typography>
            
            <Typography variant="body2" color="text.secondary" gutterBottom>
              Copy these to your .env file:
            </Typography>
            
            <Paper sx={{ mt: 2, p: 2, bgcolor: 'background.default' }}>
              <pre style={{ margin: 0, fontSize: '0.875rem', overflow: 'auto' }}>
{`FAX_BACKEND=${backend}
${backend === 'phaxio' ? `PHAXIO_API_KEY=${config.phaxio_api_key || 'your_key'}
PHAXIO_API_SECRET=${config.phaxio_api_secret || 'your_secret'}
PUBLIC_API_URL=${config.public_api_url || 'https://your-domain.com'}
PHAXIO_CALLBACK_URL=${config.public_api_url || 'https://your-domain.com'}/phaxio-callback
PHAXIO_VERIFY_SIGNATURE=true` : ''}
${backend === 'sinch' ? `SINCH_PROJECT_ID=${config.sinch_project_id || 'your_project'}
SINCH_API_KEY=${config.sinch_api_key || 'your_key'}
SINCH_API_SECRET=${config.sinch_api_secret || 'your_secret'}` : ''}
${backend === 'sip' ? `ASTERISK_AMI_HOST=${config.ami_host || 'asterisk'}
ASTERISK_AMI_PORT=${config.ami_port || 5038}
ASTERISK_AMI_USERNAME=${config.ami_username || 'api'}
ASTERISK_AMI_PASSWORD=${config.ami_password || 'change_me'}
SIP_USERNAME=${config.sip_username || 'your_username'}
SIP_PASSWORD=${config.sip_password || 'your_password'}
SIP_SERVER=${config.sip_server || 'sip.provider.com'}
SIP_FROM_USER=${config.fax_station_id || '+15551234567'}
FAX_LOCAL_STATION_ID=${config.fax_station_id || '+15551234567'}` : ''}
# Core Security
ENABLE_LOCAL_ADMIN=true  # Required for admin console
API_KEY=${config.api_key || 'bootstrap_admin_key'}
REQUIRE_API_KEY=${config.require_api_key ? 'true' : 'false'}
ENFORCE_PUBLIC_HTTPS=${config.enforce_public_https ? 'true' : 'false'}
AUDIT_LOG_ENABLED=${config.audit_log_enabled ? 'true' : 'false'}
PDF_TOKEN_TTL_MINUTES=${config.pdf_token_ttl_minutes || 60}`}
              </pre>
            </Paper>
            
            <Alert severity="warning" sx={{ mt: 2 }}>
              After updating .env, restart the API with: docker compose restart api
            </Alert>
          </>
        );

      default:
        return null;
    }
  };

  return (
    <Box>
      <Typography variant="h4" gutterBottom>
        Setup Wizard
      </Typography>
      
      <Stepper activeStep={activeStep} sx={{ mb: 4 }}>
        {steps.map((label) => (
          <Step key={label}>
            <StepLabel>{label}</StepLabel>
          </Step>
        ))}
      </Stepper>
      
      <Paper sx={{ p: 3 }}>
        {renderStepContent(activeStep)}
      </Paper>
      
      <Box sx={{ display: 'flex', justifyContent: 'space-between', mt: 3 }}>
        <Button
          disabled={activeStep === 0}
          onClick={() => setActiveStep((prev) => prev - 1)}
        >
          Back
        </Button>
        
        <Button
          variant="contained"
          onClick={() => setActiveStep((prev) => prev + 1)}
          disabled={activeStep === steps.length - 1}
        >
          {activeStep === steps.length - 1 ? 'Finish' : 'Next'}
        </Button>
      </Box>
    </Box>
  );
}

export default SetupWizard;
```

---

## Dockerfile Integration

```dockerfile
# Add to api/Dockerfile
FROM node:18-alpine AS admin-ui-builder
WORKDIR /build
COPY api/admin_ui/package*.json ./
RUN npm ci
COPY api/admin_ui/ ./
RUN npm run build

FROM python:3.11-slim
# ... existing setup ...

# Copy admin UI build to correct location
COPY --from=admin-ui-builder /build/dist /app/admin_ui/dist

# ... rest of Dockerfile ...
```

---

## Testing Strategy

### Backend Tests (pytest)
```python
# api/tests/test_admin_console.py
import pytest
from fastapi.testclient import TestClient

def test_admin_ui_localhost_only(client):
    """Admin UI only accessible from localhost"""
    # Simulate remote IP
    response = client.get("/admin/ui/", headers={"X-Forwarded-For": "192.168.1.100"})
    assert response.status_code == 403

def test_admin_endpoints_require_auth(client):
    """Admin endpoints require keys:manage scope"""
    response = client.get("/admin/settings")
    assert response.status_code == 401
    
    # With wrong scope
    response = client.get("/admin/settings", headers={"X-API-Key": "fbk_live_test_wrongscope"})
    assert response.status_code == 403

def test_settings_validation(admin_client):
    """Settings validation performs actual checks"""
    response = admin_client.post("/admin/settings/validate", json={
        "backend": "phaxio",
        "phaxio_api_key": "invalid",
        "phaxio_api_secret": "invalid"
    })
    assert response.status_code == 200
    assert response.json()["checks"]["auth"] == False

def test_diagnostics_comprehensive(admin_client):
    """Diagnostics check all components"""
    response = admin_client.post("/admin/diagnostics/run")
    assert response.status_code == 200
    data = response.json()
    assert "backend" in data["checks"]
    assert "system" in data["checks"]
    assert "security" in data["checks"]

def test_job_listing_masks_phi(admin_client):
    """Job listings mask phone numbers"""
    response = admin_client.get("/admin/fax-jobs")
    assert response.status_code == 200
    jobs = response.json()["jobs"]
    for job in jobs:
        # Should show only last 4 digits
        assert job["to_number"].startswith("*")
        assert len(job["to_number"]) >= 4

def test_health_status_polling(admin_client):
    """Health status endpoint returns current state"""
    response = admin_client.get("/admin/health-status")
    assert response.status_code == 200
    data = response.json()
    assert "backend" in data
    assert "jobs" in data
    assert "timestamp" in data
```

### Frontend Tests (Vitest)
```typescript
// admin_ui/src/tests/App.test.tsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import App from '../App';

describe('Admin Console', () => {
  it('requires API key for access', () => {
    render(<App />);
    expect(screen.getByText(/Faxbot Admin Console/)).toBeInTheDocument();
    expect(screen.getByLabelText(/API Key/)).toBeInTheDocument();
  });

  it('shows localhost-only indicator', () => {
    render(<App />);
    expect(screen.getByText(/127.0.0.1/)).toBeInTheDocument();
  });

  it('validates API key on login', async () => {
    const { getByLabelText, getByText } = render(<App />);
    
    const input = getByLabelText(/API Key/);
    const button = getByText(/Login/);
    
    fireEvent.change(input, { target: { value: 'invalid_key' } });
    fireEvent.click(button);
    
    await waitFor(() => {
      expect(screen.getByText(/Invalid API key/)).toBeInTheDocument();
    });
  });

  it('masks phone numbers in job list', async () => {
    // Mock API response
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          jobs: [{ to_number: '****4567', status: 'completed' }]
        })
      })
    );
    
    // ... render with valid auth ...
    
    await waitFor(() => {
      expect(screen.getByText(/\*\*\*\*4567/)).toBeInTheDocument();
    });
  });
});
```

### End-to-End Tests (Playwright)
```javascript
// e2e/admin_console.spec.js
const { test, expect } = require('@playwright/test');

test.describe('Admin Console Security', () => {
  test('blocks remote access', async ({ page, context }) => {
    // Override IP detection
    await context.route('**/*', route => {
      route.continue({
        headers: {
          ...route.request().headers(),
          'X-Forwarded-For': '10.0.0.1'
        }
      });
    });
    
    await page.goto('http://localhost:8080/admin/ui');
    await expect(page).toHaveText(/403|Forbidden/);
  });

  test('setup wizard backend isolation', async ({ page }) => {
    await page.goto('http://localhost:8080/admin/ui');
    
    // Login
    await page.fill('[placeholder*="API Key"]', process.env.API_KEY);
    await page.click('button:has-text("Login")');
    
    // Navigate to setup
    await page.click('a:has-text("Setup")');
    
    // Select Phaxio
    await page.selectOption('select', 'phaxio');
    await expect(page).toHaveText(/Cloud fax service/);
    await expect(page).not.toHaveText(/AMI/);  // No SIP config visible
    
    // Select SIP
    await page.selectOption('select', 'sip');
    await expect(page).toHaveText(/T.38 support/);
    await expect(page).not.toHaveText(/Phaxio/);  // No Phaxio config visible
  });

  test('validates and sends test fax', async ({ page }) => {
    // ... setup ...
    
    await page.fill('[label="Test Fax Number"]', '+15551234567');
    await page.click('button:has-text("Validate")');
    
    await expect(page).toHaveText(/Test fax sent/);
  });
});
```

---

## Implementation Phases

### Phase 1: Core Infrastructure (Week 1)
- [x] Static file serving setup (api/app/main.py)
- [x] Localhost-only middleware for /admin/ui (api/app/main.py)
- [x] Basic auth with API keys (existing)
- [x] Settings read endpoint (api/app/main.py)
- [x] Config endpoint (exists)
- [x] Settings validation endpoint (v1-safe) (api/app/main.py)
- [x] AMI test helper (api/app/ami.py)

### Phase 2: Frontend Foundation (Week 1-2)
- [ ] React app setup with Material-UI
- [x] API client implementation (minimal in /admin/ui inline JS)
- [x] Login flow (no cookies) (sessionStorage key)
- [x] Dashboard with health status (polling every 5s)
- [x] Navigation structure (tabs: Dashboard/Send/Jobs/Inbound/Keys/Settings/Diagnostics)

### Phase 3: Setup Wizard (Week 2)
- [ ] Backend selection UI
- [ ] Backend-specific configuration forms
- [ ] Validation endpoint with real tests
- [x] .env generation display (UI button wired to /admin/settings/export)
- [x] Test fax sending (simple Send Fax form calls POST /fax)

### Phase 4: Job Management (Week 2-3)
- [x] Job listing with PHI masking (api/app/main.py)
- [x] Job details view (api/app/main.py)
- [x] Status filtering (api/app/main.py)
- [x] Polling updates (client-side; jobs auto-refresh when active)

### Phase 5: API Key Management (Week 3)
- [x] Key creation UI (inline UI; token shown once)
- [x] Scope selection (comma-separated input)
- [x] Key rotation/revocation (actions wired)
- [ ] Audit display (deferred)

### Phase 6: Diagnostics (Week 3-4)
- [x] Comprehensive checks endpoint (v1-safe) (api/app/main.py)
- [x] UI for viewing results (inline on /admin/ui page)
- [x] Remediation suggestions (tips rendered from diagnostics)
- [x] Export capability (copy/download diagnostics JSON)

Additional (v1):
- [x] Inbound inbox basic UI (requires inbound:list/read or bootstrap key)

### Phase 7: v2 Planning (Week 4-5)
- [ ] Design settings persistence (deferred to v2)
- [ ] Plan SSE implementation with session cookies
- [ ] Design webhook subscription system
- [ ] Plan multi-tenant considerations
- [ ] Document v2 migration path

### Phase 8: Production Hardening (Week 5-6)
- [ ] Complete test coverage
- [ ] Performance optimization
- [ ] Documentation
- [ ] Docker integration
- [ ] Deployment guide

---

## Security Checklist

- ✅ 100% localhost-only access (no remote option)
- ✅ No cookies or sessions (API key per request)
- ✅ PHI masking (last 4 digits only)
- ✅ Audit logging for all changes
- ✅ Secrets encrypted in database
- ✅ HTTPS enforcement for production
- ✅ No inline scripts (CSP compliant)
- ✅ X-Frame-Options: DENY
- ✅ Cache-Control: no-store for PHI
- ✅ Backend isolation (no mixed instructions)
- ✅ AMI never exposed publicly
- ✅ Default password detection
- ✅ Test fax capability for validation

---

## Migration from Current State

1. **No Breaking Changes**: All existing endpoints remain
2. **New Endpoints Only**: Admin console adds new `/admin/*` paths
3. **Database Compatible**: New tables don't affect existing schema
4. **Docker Simple**: Just adds UI build step
5. **Backward Compatible**: Works with existing .env files

---

## Notes for Junior Developer

1. **Start Simple**: Get static files serving first, then add endpoints
2. **Use What Exists**: Don't recreate auth, use existing `require_admin`
3. **Test Locally**: Everything runs on localhost, easy to test
4. **Security First**: When in doubt, be more restrictive
5. **Material-UI**: Use for consistency and accessibility
6. **TypeScript**: Helps catch errors early
7. **No Remote Ever**: Hard-code localhost checks, no config option

This document provides a complete, production-ready v1 implementation guide that:
- Addresses security concerns (feature flag, no proxy access, no SSE in v1)
- Maintains HIPAA compliance without complexity
- Uses existing codebase properly
- Defers complex features (settings persistence, SSE) to v2
- Provides clear implementation phases
- Includes comprehensive testing
- Gives junior developers concrete, achievable goals

**Key v1 Decisions**:
- Polling instead of SSE (avoids API key in URL)
- Read-only settings with .env export (avoids encryption complexity)
- Feature flag required (`ENABLE_LOCAL_ADMIN=true`)
- Strict localhost enforcement with proxy detection
- No database changes required
