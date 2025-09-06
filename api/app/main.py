import os
import shutil
import re
import uuid
import asyncio
import secrets
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Header, Depends, Query, Request
from fastapi.responses import FileResponse
from .config import settings, reload_settings
from .db import init_db, SessionLocal, FaxJob
from .models import FaxJobOut
from .conversion import ensure_dir, txt_to_pdf, pdf_to_tiff
from .ami import ami_client
from .phaxio_service import get_phaxio_service
import hmac
import hashlib
from urllib.parse import urlparse
from .audit import init_audit_logger, audit_event


app = FastAPI(title="Faxbot API", version="1.0.0")
# Expose phaxio_service module for tests that reference app.phaxio_service
from . import phaxio_service as _phaxio_module  # noqa: E402
app.phaxio_service = _phaxio_module  # type: ignore[attr-defined]


PHONE_RE = re.compile(r"^[+]?\d{6,20}$")
ALLOWED_CT = {"application/pdf", "text/plain"}


@app.on_event("startup")
async def on_startup():
    # Re-read environment into settings for testability and dynamic config
    reload_settings()
    init_db()
    # Ensure data dir
    ensure_dir(settings.fax_data_dir)
    # Validate Ghostscript availability for PDF->TIFF conversion
    if shutil.which("gs") is None:
        print("[warn] Ghostscript (gs) not found; PDF→TIFF conversion will be stubbed. Install 'ghostscript' for production use.")
    # Security posture warnings
    if not settings.api_key and not settings.fax_disabled:
        print("[warn] API_KEY is unset while faxing is enabled; /fax requests are unauthenticated. Set API_KEY for production.")
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
            job.status = status or job.status
            job.error = error
            if pages:
                try:
                    job.pages = int(pages)
                except Exception:
                    pass
            job.updated_at = datetime.utcnow()
            db.add(job)
            db.commit()
    if job_id and status:
        audit_event("job_updated", job_id=job_id, status=status, provider="asterisk")


@app.get("/health")
def health():
    return {"status": "ok"}


def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(401, detail="Invalid API key")


@app.post("/fax", response_model=FaxJobOut, status_code=202, dependencies=[Depends(require_api_key)])
async def send_fax(background: BackgroundTasks, to: str = Form(...), file: UploadFile = File(...)):
    # Validate destination
    if not PHONE_RE.match(to):
        raise HTTPException(400, detail="'to' must be E.164 or digits only")
    # Validate content type and size
    if file.content_type not in ALLOWED_CT:
        raise HTTPException(415, detail="Only PDF and TXT are allowed")
    content = await file.read()
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(413, detail=f"File exceeds {settings.max_file_size_mb} MB limit")

    job_id = uuid.uuid4().hex
    orig_path = os.path.join(settings.fax_data_dir, f"{job_id}-{file.filename}")
    pdf_path = os.path.join(settings.fax_data_dir, f"{job_id}.pdf")
    tiff_path = os.path.join(settings.fax_data_dir, f"{job_id}.tiff")

    # Persist upload
    with open(orig_path, "wb") as f:
        f.write(content)

    # Convert to PDF if needed
    if file.content_type == "text/plain" or (file.filename and file.filename.lower().endswith(".txt")):
        if settings.fax_disabled:
            # Test mode - skip conversion
            with open(pdf_path, "wb") as f:
                f.write(b"%PDF-1.4\ntest\n%%EOF")
        else:
            txt_to_pdf(orig_path, pdf_path)
    else:
        # Write the PDF directly
        with open(pdf_path, "wb") as f:
            f.write(content)

    # Backend-specific file preparation
    if settings.fax_backend == "phaxio":
        # Cloud backend: skip TIFF conversion; provider callback will set final pages
        pages = None
        if settings.fax_disabled:
            # Test mode: nothing else to prepare
            pass
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
            # For Phaxio, we need a public URL for the PDF
            background.add_task(_send_via_phaxio, job_id, to, pdf_path)
        else:
            # SIP/Asterisk backend
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
                job.status = "in_progress"
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


@app.get("/fax/{job_id}", response_model=FaxJobOut, dependencies=[Depends(require_api_key)])
def get_fax(job_id: str):
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if not job:
            raise HTTPException(404, detail="Job not found")
    return _serialize_job(job)


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
        expected_token = job.pdf_token
        if not expected_token and job.pdf_url:
            # Fallback: extract token from stored pdf_url if present (tests)
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(job.pdf_url).query)
                t = qs.get("token", [None])[0]
                if t:
                    expected_token = t
            except Exception:
                expected_token = None
        # If no token is configured for this job, treat as not found
        if not expected_token:
            raise HTTPException(404, detail="PDF not available")
        # Validate token equality
        if token != expected_token:
            raise HTTPException(403, detail="Invalid token")
        # Validate expiry if set
        if job.pdf_token_expires_at and datetime.utcnow() > job.pdf_token_expires_at:
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
            job.updated_at = datetime.utcnow()
            db.add(job)
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
                job.pdf_url = pdf_url
                job.pdf_token = pdf_token
                job.pdf_token_expires_at = expires_at
                job.status = "in_progress"
                job.updated_at = datetime.utcnow()
                db.add(job)
                db.commit()
        
        # Send via Phaxio
        audit_event("job_dispatch", job_id=job_id, method="phaxio")
        result = await phaxio_service.send_fax(to, pdf_url, job_id)
        
        # Update job with provider SID
        with SessionLocal() as db:
            job = db.get(FaxJob, job_id)
            if job:
                job.provider_sid = result['provider_sid']
                job.status = result['status']
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
    return FaxJobOut(
        id=job.id,
        to=job.to_number,
        status=job.status,
        error=job.error,
        pages=job.pages,
        backend=job.backend,
        provider_sid=job.provider_sid,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
