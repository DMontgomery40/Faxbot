import os
import re
import uuid
from datetime import datetime
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Header, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import settings
from .db import init_db, SessionLocal, FaxJob
from .models import FaxRequest, FaxJobOut
from .conversion import ensure_dir, txt_to_pdf, pdf_to_tiff
from .ami import ami_client
import asyncio


app = FastAPI(title="Open Fax API", version="1.0.0")


PHONE_RE = re.compile(r"^[+]?\d{6,20}$")
ALLOWED_CT = {"application/pdf", "text/plain"}


@app.on_event("startup")
async def on_startup():
    init_db()
    # Ensure data dir
    ensure_dir(settings.fax_data_dir)
    # Start AMI listener and result handler
    if not settings.fax_disabled:
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


@app.get("/health")
def health():
    return {"status": "ok"}


def require_api_key(x_api_key: str | None = Header(default=None)):
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
    if file.content_type == "text/plain" or file.filename.lower().endswith(".txt"):
        txt_to_pdf(orig_path, pdf_path)
    else:
        # Write the PDF directly
        with open(pdf_path, "wb") as f:
            f.write(content)

    # Convert PDF to TIFF
    pages, _ = pdf_to_tiff(pdf_path, tiff_path)

    # Create job in DB
    with SessionLocal() as db:
        job = FaxJob(
            id=job_id,
            to_number=to,
            file_name=file.filename,
            tiff_path=tiff_path,
            status="queued" if not settings.fax_disabled else "disabled",
            pages=pages,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()

    # Kick off originate in background
    if not settings.fax_disabled:
        background.add_task(_originate_job, job_id, to, tiff_path)

    return _serialize_job(job)


async def _originate_job(job_id: str, to: str, tiff_path: str):
    try:
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


@app.get("/fax/{job_id}", response_model=FaxJobOut, dependencies=[Depends(require_api_key)])
def get_fax(job_id: str):
    with SessionLocal() as db:
        job = db.get(FaxJob, job_id)
        if not job:
            raise HTTPException(404, detail="Job not found")
        return _serialize_job(job)


def _serialize_job(job: FaxJob) -> FaxJobOut:
    return FaxJobOut(
        id=job.id,
        to=job.to_number,
        status=job.status,
        error=job.error,
        pages=job.pages,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
